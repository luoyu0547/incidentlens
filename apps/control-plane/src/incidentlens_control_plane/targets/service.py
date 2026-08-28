"""Product facade over the authoritative ProjectRegistry.

The Target facade keeps the registry authoritative: ``host`` / ``ssh_user`` /
``ssh_port`` and all service/scope data stay in ``projects.record_json`` while
``target_facade_bindings`` carries only product identity, the server-side
``authentication_ref`` and host-key policy metadata.

Bindings for pre-existing registry targets are lazily materialized on first
access and are stable thereafter: a registry target ID is retained as the
facade ID when it is globally unique across all projects, otherwise the facade
ID is ``tgt_`` plus the first 24 hex characters of
``sha256(project_id + "\\0" + registry_target_id)`` so duplicate internal target
IDs in different projects never alias to the same facade target.
"""

from __future__ import annotations

import hashlib
import uuid
from collections import Counter
from datetime import UTC, datetime

from incidentlens_control_plane.investigation.store import InvestigationStore
from incidentlens_control_plane.project_registry.store import (
    ProjectNotFound,
    ProjectRegistryStore,
    RegistryUpdateConflict,
)
from incidentlens_control_plane.project_registry.types import (
    ProjectRegistration,
    TargetRegistration,
)
from incidentlens_control_plane.targets.store import (
    TargetNotFound,
    TargetStore,
    TargetVersionConflict,
)
from incidentlens_control_plane.targets.types import (
    TargetBinding,
    TargetCreate,
    TargetPatch,
    TargetServiceView,
    TargetView,
)

#: Project IDs the facade owns (one internal registry project per product target).
_FACADE_PROJECT_PREFIX = "prd_"

#: Internal registry target ID inside every facade-owned project.
_INTERNAL_TARGET_ID = "default"


class TargetDeleteBlocked(Exception):
    """Raised when a delete is blocked by an active (non-terminal) investigation."""


class TargetService:
    """Boundary service implementing the Target product facade."""

    def __init__(
        self,
        *,
        projects: ProjectRegistryStore,
        target_store: TargetStore,
        investigations: InvestigationStore,
    ) -> None:
        self._projects = projects
        self._target_store = target_store
        self._investigations = investigations

    # -- product read surface --------------------------------------------------

    def list_targets(self, *, now: datetime | None = None) -> list[TargetView]:
        """Return every product target, binding any pre-existing targets first."""
        now = now if now is not None else datetime.now(UTC)
        all_targets = self._projects.list_registry_targets()
        counts = Counter(registry_target_id for _, registry_target_id, _ in all_targets)
        bound: set[tuple[str, str]] = {
            (binding.project_id, binding.registry_target_id)
            for binding in self._target_store.list()
        }
        for project_id, registry_target_id, display_name in all_targets:
            if (project_id, registry_target_id) not in bound:
                binding = self._build_existing_binding(
                    project_id,
                    registry_target_id,
                    display_name,
                    counts=counts,
                    now=now,
                )
                self._target_store.create(binding)
                bound.add((project_id, registry_target_id))

        views: list[TargetView] = []
        for binding in self._target_store.list():
            try:
                views.append(self._to_view(binding))
            except TargetNotFound:
                # The authoritative project (or its target) disappeared beneath
                # a facade binding; skip it instead of failing the whole list.
                continue
        return views

    def get_target(self, target_id: str, *, now: datetime | None = None) -> TargetView:
        """Return one product target, lazily binding pre-existing targets."""
        now = now if now is not None else datetime.now(UTC)
        binding = self._binding_for_access(target_id, now=now)
        return self._to_view(binding)

    def services_for_target(
        self, target_id: str, *, now: datetime | None = None
    ) -> list[TargetServiceView]:
        """Return the services of a target, resolved through ProjectRegistry."""
        now = now if now is not None else datetime.now(UTC)
        binding = self._binding_for_access(target_id, now=now)
        try:
            record = self._projects.get(binding.project_id)
        except ProjectNotFound as exc:
            raise TargetNotFound(
                f"target '{target_id}' no longer exists"
            ) from exc
        return [
            TargetServiceView(
                service=svc.compose_service,
                container_names=svc.container_names,
                allowed_host_paths=svc.allowed_host_paths,
                protected_remote_paths=svc.protected_remote_paths,
            )
            for svc in record.services
        ]

    # -- product write surface ------------------------------------------------

    def create_target(self, create: TargetCreate, *, now: datetime) -> TargetView:
        """Create a new product target backed by a fresh internal project."""
        project_id = f"{_FACADE_PROJECT_PREFIX}{uuid.uuid4().hex[:24]}"
        registration = ProjectRegistration(
            project_id=project_id,
            display_name=create.name,
            local_source_paths=(
                (create.optional_source_path,)
                if create.optional_source_path is not None
                else ()
            ),
            targets=(
                TargetRegistration(
                    target_id=_INTERNAL_TARGET_ID,
                    host=create.host,
                    ssh_user=create.ssh_user,
                    port=create.ssh_port,
                ),
            ),
            services=(),
        )
        record = self._projects.create(registration, now=now)
        binding = TargetBinding(
            target_id=f"tgt_{uuid.uuid4().hex[:24]}",
            project_id=record.project_id,
            registry_target_id=_INTERNAL_TARGET_ID,
            name=create.name,
            authentication_ref=create.authentication_ref,
            host_key_policy=create.host_key_policy,
            pinned_host_key_sha256=create.pinned_host_key_sha256,
            version=1,
            created_at=now,
            updated_at=now,
        )
        self._target_store.create(binding)
        return self._to_view(binding)

    def patch_target(
        self, target_id: str, patch: TargetPatch, *, now: datetime
    ) -> TargetView:
        """Apply a partial update under optimistic concurrency.

        ``expected_version`` must match the current facade version.  Registry
        host/user/port and the optional local-source metadata are written back
        through ProjectRegistry, which preserves services and scope untouched.
        """
        binding = self._binding_for_access(target_id, now=now)
        if binding.version != patch.expected_version:
            raise TargetVersionConflict(
                f"target '{target_id}' expected version "
                f"{patch.expected_version}, current {binding.version}"
            )
        try:
            record = self._projects.get(binding.project_id)
        except ProjectNotFound as exc:
            raise TargetNotFound(f"target '{target_id}' no longer exists") from exc

        target_reg = next(
            (t for t in record.targets if t.target_id == binding.registry_target_id),
            None,
        )
        if target_reg is None:
            raise TargetNotFound(f"target '{target_id}' is no longer registered")

        updated_target = target_reg.model_copy(
            update={
                "host": patch.host if patch.host is not None else target_reg.host,
                "ssh_user": (
                    patch.ssh_user if patch.ssh_user is not None else target_reg.ssh_user
                ),
                "port": patch.ssh_port if patch.ssh_port is not None else target_reg.port,
            }
        )
        new_targets = tuple(
            updated_target if t.target_id == binding.registry_target_id else t
            for t in record.targets
        )
        local_source_paths = record.local_source_paths
        if "optional_source_path" in patch.model_fields_set:
            local_source_paths = (
                (patch.optional_source_path,)
                if patch.optional_source_path is not None
                else ()
            )
        registration = ProjectRegistration(
            project_id=record.project_id,
            display_name=record.display_name,
            local_source_paths=local_source_paths,
            targets=new_targets,
            services=record.services,
        )
        try:
            self._projects.replace(
                registration, now=now, expected_updated_at=record.updated_at
            )
        except RegistryUpdateConflict as exc:
            raise TargetVersionConflict(
                f"target '{target_id}' was modified concurrently"
            ) from exc
        except ProjectNotFound as exc:
            raise TargetNotFound(f"target '{target_id}' no longer exists") from exc

        updated_binding = binding.model_copy(
            update={
                "name": patch.name if patch.name is not None else binding.name,
                "authentication_ref": (
                    patch.authentication_ref
                    if patch.authentication_ref is not None
                    else binding.authentication_ref
                ),
                "host_key_policy": (
                    patch.host_key_policy
                    if patch.host_key_policy is not None
                    else binding.host_key_policy
                ),
                "pinned_host_key_sha256": (
                    patch.pinned_host_key_sha256
                    if patch.pinned_host_key_sha256 is not None
                    else binding.pinned_host_key_sha256
                ),
                "version": binding.version + 1,
                "updated_at": now,
            }
        )
        stored_binding = self._target_store.update(
            updated_binding, expected_version=binding.version
        )
        return self._to_view(stored_binding)

    def delete_target(self, target_id: str, *, now: datetime) -> None:
        """Delete a product target, blocking while investigations reference it.

        A facade-owned project (created for this target) is removed entirely; a
        pre-existing registry target is removed from its project so the facade
        binding cannot silently resurrect on the next access.
        """
        binding = self._binding_for_access(target_id, now=now)
        for investigation in self._investigations.list_non_terminal_investigations():
            if (
                investigation.project_id == binding.project_id
                and investigation.target_id == binding.registry_target_id
            ):
                raise TargetDeleteBlocked(
                    f"target '{target_id}' is referenced by an active investigation"
                )

        self._target_store.delete(target_id)
        if binding.project_id.startswith(_FACADE_PROJECT_PREFIX):
            try:
                self._projects.delete(binding.project_id)
            except ProjectNotFound:
                # The internal project was already gone; the binding is removed.
                pass
            return

        try:
            record = self._projects.get(binding.project_id)
        except ProjectNotFound as exc:
            raise TargetNotFound(f"target '{target_id}' no longer exists") from exc
        new_targets = tuple(
            t for t in record.targets if t.target_id != binding.registry_target_id
        )
        registration = ProjectRegistration(
            project_id=record.project_id,
            display_name=record.display_name,
            local_source_paths=record.local_source_paths,
            targets=new_targets,
            services=record.services,
        )
        try:
            self._projects.replace(
                registration, now=now, expected_updated_at=record.updated_at
            )
        except RegistryUpdateConflict as exc:
            raise TargetVersionConflict(
                f"target '{target_id}' was modified concurrently"
            ) from exc
        except ProjectNotFound as exc:
            raise TargetNotFound(f"target '{target_id}' no longer exists") from exc

    # -- internals -------------------------------------------------------------

    def _binding_for_access(self, target_id: str, *, now: datetime) -> TargetBinding:
        """Resolve an existing binding or lazily bind a pre-existing target."""
        try:
            return self._target_store.get(target_id)
        except TargetNotFound:
            pass
        all_targets = self._projects.list_registry_targets()
        counts = Counter(
            registry_target_id for _, registry_target_id, _ in all_targets
        )
        for project_id, registry_target_id, display_name in all_targets:
            if (
                self._derive_facade_id(
                    project_id,
                    registry_target_id,
                    globally_unique=counts[registry_target_id] == 1,
                )
                == target_id
            ):
                binding = self._build_existing_binding(
                    project_id,
                    registry_target_id,
                    display_name,
                    counts=counts,
                    now=now,
                )
                return self._target_store.create(binding)
        raise TargetNotFound(f"target '{target_id}' not found")

    def _build_existing_binding(
        self,
        project_id: str,
        registry_target_id: str,
        display_name: str,
        *,
        counts: Counter[str],
        now: datetime,
    ) -> TargetBinding:
        """Build a binding for a pre-existing registry target (not yet written)."""
        return TargetBinding(
            target_id=self._derive_facade_id(
                project_id,
                registry_target_id,
                globally_unique=counts[registry_target_id] == 1,
            ),
            project_id=project_id,
            registry_target_id=registry_target_id,
            name=display_name,
            authentication_ref="",
            host_key_policy="strict",
            pinned_host_key_sha256=None,
            version=1,
            created_at=now,
            updated_at=now,
        )

    def _to_view(self, binding: TargetBinding) -> TargetView:
        """Render a facade binding against the authoritative registry record."""
        try:
            record = self._projects.get(binding.project_id)
        except ProjectNotFound as exc:
            raise TargetNotFound(
                f"target '{binding.target_id}' no longer has a registered project"
            ) from exc
        target_reg = next(
            (t for t in record.targets if t.target_id == binding.registry_target_id),
            None,
        )
        if target_reg is None:
            raise TargetNotFound(
                f"target '{binding.target_id}' is no longer registered"
            )
        return TargetView(
            target_id=binding.target_id,
            name=binding.name,
            host=target_reg.host,
            ssh_user=target_reg.ssh_user,
            ssh_port=target_reg.port or 22,
            authentication_configured=bool(binding.authentication_ref),
            authentication_hint=self._authentication_hint(binding.authentication_ref),
            host_key_policy=binding.host_key_policy,
            pinned_host_key_sha256=binding.pinned_host_key_sha256,
            optional_source_path=(
                record.local_source_paths[0] if record.local_source_paths else None
            ),
            version=binding.version,
            created_at=binding.created_at,
            updated_at=binding.updated_at,
        )

    @staticmethod
    def _derive_facade_id(
        project_id: str, registry_target_id: str, *, globally_unique: bool
    ) -> str:
        """Derive the stable facade target ID for an internal registry target."""
        if globally_unique:
            return registry_target_id
        material = f"{project_id}\0{registry_target_id}".encode("utf-8")
        return f"tgt_{hashlib.sha256(material).hexdigest()[:24]}"

    @staticmethod
    def _authentication_hint(authentication_ref: str) -> str:
        """Return a short, non-secret hint for a stored SSH identity reference."""
        if not authentication_ref:
            return ""
        if ":" in authentication_ref:
            return authentication_ref.split(":", 1)[0]
        return authentication_ref[:16]

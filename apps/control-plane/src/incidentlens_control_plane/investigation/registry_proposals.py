"""Approval-gated registry updates backed by discovery evidence.

``RegistryProposalService`` turns an unregistered container/path candidate
(discovered by ``SourceDiscoveryService`` and exposed only through authorized
output) into an evidence-backed ``RegistryUpdateProposal``, requests an exact,
single-use approval, and — only after the approval is granted — re-validates the
current registry, canonicalizes paths / verifies the container identity, and
atomically writes the widened scope back through ``ProjectRegistryStore.replace``
with an audit event.  An agent/model never modifies the registry directly; a
rejected or stale decision is returned as evidence so the parent can continue
within its original permissions or stop with a limitation.

Approval intents are canonicalized exactly like every other approval: the same
deterministic intent is used to request and later to consume, so a proposal can
never be replayed against a different shape of change.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath

from incidentlens_control_plane.approvals.service import (
    ApprovalMismatch,
    ApprovalService,
    ApprovalUnavailable,
)
from incidentlens_control_plane.approvals.store import (
    ApprovalNotFound,
    intent_sha256,
)
from incidentlens_control_plane.approvals.types import ApprovalRecord, ApprovalStatus
from incidentlens_control_plane.events.broker import RuntimeEventBroker
from incidentlens_control_plane.events.store import RuntimeEventStore
from incidentlens_control_plane.events.types import RuntimeEvent, RuntimeEventType
from incidentlens_control_plane.evidence.service import EvidenceService
from incidentlens_control_plane.evidence.types import EvidenceRef
from incidentlens_control_plane.investigation.events import InvestigationEventPublisher
from incidentlens_control_plane.investigation.store import (
    InvestigationNotFound,
    InvestigationStore,
)
from incidentlens_control_plane.investigation.types import (
    AgentRun,
    EvidenceReference,
    RegistryProposalStatus,
    RegistryUpdateKind,
    RegistryUpdateProposal,
)
from incidentlens_control_plane.project_registry.store import (
    ProjectRegistryStore,
    RegistryUpdateConflict,
)
from incidentlens_control_plane.project_registry.types import (
    ProjectRecord,
    ProjectRegistration,
    TargetRegistration,
)
from incidentlens_control_plane.remote_ops.gateway import RemoteToolGateway
from incidentlens_control_plane.remote_ops.sessions import SessionManager
from incidentlens_control_plane.remote_ops.transport import RemotePathError

_IDENTITY_TIMEOUT = 30.0
_CONTAINER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class RegistryProposalError(Exception):
    """A deterministic proposal/decision failure safe to report to the model."""


@dataclass(frozen=True)
class ProposalOutcome:
    """A persisted, approval-pending registry update proposal."""

    proposal: RegistryUpdateProposal
    approval: ApprovalRecord
    evidence: tuple[EvidenceReference, ...]


@dataclass(frozen=True)
class ProposalDecisionOutcome:
    """The result of resolving an approval against a registry update proposal.

    ``applied`` is True only when the widened scope was written back to the
    registry.  A rejection or stale/void decision sets ``applied=False`` and
    returns the decision evidence so the parent can continue within its
    original permissions or stop with a limitation.
    """

    proposal: RegistryUpdateProposal
    applied: bool
    record: ProjectRecord | None = None
    evidence: tuple[EvidenceReference, ...] = ()
    decision: str | None = None
    reason: str | None = None


def registry_update_intent(proposal: RegistryUpdateProposal) -> dict[str, object]:
    """The canonical, deterministic approval intent for *proposal*."""
    if proposal.kind is RegistryUpdateKind.CONTAINER_REGISTRATION:
        return {
            "kind": "registry_update",
            "update_kind": "container_registration",
            "project_id": proposal.proposed_project_id,
            "target_id": proposal.proposed_target_id,
            "service": proposal.proposed_service_name,
            "container": proposal.proposed_container_name,
        }
    return {
        "kind": "registry_update",
        "update_kind": "path_extension",
        "project_id": proposal.proposed_project_id,
        "target_id": proposal.proposed_target_id,
        "service": proposal.proposed_service_name,
        "paths": [str(path) for path in proposal.proposed_paths],
    }


class RegistryProposalService:
    """Create, approve, apply and reject evidence-backed registry updates."""

    def __init__(
        self,
        *,
        projects: ProjectRegistryStore,
        investigations: InvestigationStore,
        approvals: ApprovalService,
        evidence: EvidenceService,
        events: RuntimeEventStore,
        broker: RuntimeEventBroker,
        gateway: RemoteToolGateway,
        sessions: SessionManager,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._projects = projects
        self._investigations = investigations
        self._approvals = approvals
        self._evidence = evidence
        self._events = events
        self._broker = broker
        self._gateway = gateway
        self._sessions = sessions
        self._events_pub = InvestigationEventPublisher(events, broker)
        self._now = now or (lambda: datetime.now(UTC))
        self._decision_locks: dict[str, asyncio.Lock] = {}

    # -- proposal creation ----------------------------------------------------

    async def propose(
        self,
        run: AgentRun,
        *,
        discovery_evidence_id: str,
        kind: RegistryUpdateKind,
        service_name: str,
        container: str | None = None,
        paths: tuple[PurePosixPath, ...] = (),
        now: datetime | None = None,
    ) -> ProposalOutcome:
        """Persist a proposal and request its exact, single-use approval.

        The proposed container name is a required domain field; for a
        ``PATH_EXTENSION`` it identifies the service's primary container as a
        label (it is not part of the approval intent).
        """
        now = now or self._now()
        if kind is RegistryUpdateKind.CONTAINER_REGISTRATION:
            if not container:
                raise RegistryProposalError(
                    "container_registration requires a candidate container"
                )
            if not _CONTAINER_NAME_RE.match(container):
                raise RegistryProposalError(
                    f"invalid container name: {container!r}"
                )
        else:
            if not paths:
                raise RegistryProposalError(
                    "path_extension requires at least one candidate path"
                )
        proposal_container = container or self._default_container_label(
            run, service_name
        )

        proposal = RegistryUpdateProposal(
            proposal_id=f"prop-{uuid.uuid4().hex[:16]}",
            investigation_id=run.investigation_id,
            agent_run_id=run.agent_run_id,
            kind=kind,
            discovery_evidence_id=discovery_evidence_id,
            proposed_project_id=run.scope.project_id,
            proposed_target_id=run.scope.target_id,
            proposed_service_name=service_name,
            proposed_container_name=proposal_container,
            proposed_paths=paths,
            status=RegistryProposalStatus.PENDING,
            created_at=now,
        )
        intent = registry_update_intent(proposal)
        approval = await self._approvals.request(
            intent,
            project_id=run.scope.project_id,
            target_id=run.scope.target_id,
            service=service_name,
            investigation_id=run.investigation_id,
            agent_run_id=run.agent_run_id,
            proposal_id=proposal.proposal_id,
            risk="approval_required",
            preview={
                "preview": "Registry update requires operator approval.",
                "impact": (
                    f"Would widen registry scope for service {service_name} "
                    f"on target {run.scope.target_id}."
                ),
            },
        )
        proposal = proposal.model_copy(
            update={"approval_intent_sha256": approval.intent_sha256}
        )
        self._investigations.create_proposal(proposal)
        self._events_pub.registry_proposal_created(proposal, occurred_at=now)

        description = (
            f"proposal {proposal.proposal_id} "
            f"{proposal.kind.value} service={service_name} "
            f"container={proposal.proposed_container_name} "
            f"paths={[str(p) for p in paths]} approval={approval.approval_id}"
        )
        ref = self._evidence.record_registry_discovery(
            **self._evidence_kwargs(
                run, service_name, f"proposal:{proposal.proposal_id}", now
            ),
            discovery_kind="registry_proposal",
            description=description,
        )
        summary = (
            f"proposal {proposal.proposal_id} {proposal.kind.value} pending "
            f"approval {approval.approval_id}"
        )
        return ProposalOutcome(
            proposal=proposal,
            approval=approval,
            evidence=(self._evidence_ref(ref, summary),),
        )

    # -- decision resolution --------------------------------------------------

    async def handle_approval_decision(
        self,
        proposal: RegistryUpdateProposal,
        approval: ApprovalRecord,
        *,
        now: datetime | None = None,
    ) -> ProposalDecisionOutcome:
        """Resolve an approved or rejected approval against *proposal*.

        Decisions are serialized per proposal (an in-process ``asyncio.Lock``)
        so two concurrent calls cannot double-apply a write or double-consume an
        approval: the second caller re-reads the proposal status and becomes a
        no-op.  Only a PENDING proposal is decided.  An approved proposal is
        applied as verify identity -> canonicalize -> ``replace()`` (guarded by
        the project's ``updated_at`` so a concurrent registry write is caught)
        -> audit event -> consume approval.  The approval is re-read from the
        store before any mutation, so a forged or expired caller-supplied record
        can never authorize a registry write; consumption is terminal accounting
        and a failure there does not roll back an already-completed write.
        A rejected or stale decision is returned as evidence without touching
        the registry.
        """
        now = now or self._now()
        async with self._decision_lock(proposal.proposal_id):
            return await self._decide(proposal, approval, now)

    async def _decide(
        self,
        proposal: RegistryUpdateProposal,
        approval: ApprovalRecord,
        now: datetime,
    ) -> ProposalDecisionOutcome:
        current = self._investigations.get_proposal(proposal.proposal_id)
        if current.status is not RegistryProposalStatus.PENDING:
            return ProposalDecisionOutcome(
                proposal=current,
                applied=False,
                decision=current.status.value,
                reason=f"proposal already decided as {current.status.value}",
            )

        # Never trust the caller-supplied record: re-read the persisted approval
        # so a forged/expired record cannot authorize a mutation (I1).
        stored = self._approvals.get(approval.approval_id)
        if stored is None:
            raise ApprovalNotFound(f"Approval '{approval.approval_id}' not found")
        if stored.status is ApprovalStatus.REJECTED:
            return await self._reject(proposal, stored, now)
        if stored.status is not ApprovalStatus.APPROVED:
            raise ApprovalUnavailable(
                f"approval {approval.approval_id} is {stored.status.value}, "
                "not approved"
            )
        if now > stored.expires_at:
            raise ApprovalUnavailable(
                f"approval {approval.approval_id} expired at "
                f"{stored.expires_at.isoformat()}"
            )

        intent = registry_update_intent(proposal)
        if intent_sha256(intent) != stored.intent_sha256:
            raise ApprovalMismatch(
                "approval intent does not match the proposal's registry update"
            )

        # Re-validate against the live registry before applying.
        try:
            project = self._projects.get(proposal.proposed_project_id)
        except Exception as exc:  # noqa: BLE001 - ProjectNotFound maps to stale
            return await self._stale(
                proposal, stored, f"project no longer registered: {exc}", now
            )

        canonical_paths: tuple[PurePosixPath, ...] = proposal.proposed_paths
        if proposal.kind is RegistryUpdateKind.CONTAINER_REGISTRATION:
            if not await self._verify_container_identity(project, proposal, now):
                return await self._stale(
                    proposal,
                    stored,
                    f"container {proposal.proposed_container_name!r} is not running",
                    now,
                )
        else:
            resolved = await self._canonicalize_host_paths(project, proposal, now)
            if resolved is None:
                return await self._stale(
                    proposal, stored, "a proposed path no longer exists", now
                )
            canonical_paths = resolved

        try:
            derived = self._derive_registration(project, proposal, canonical_paths)
        except RegistryUpdateConflict as exc:
            return await self._stale(proposal, stored, str(exc), now)
        except Exception as exc:  # noqa: BLE001 - service/target gone maps to stale
            return await self._stale(proposal, stored, str(exc), now)

        # Optimistic writeback: conditional on the project's updated_at as read
        # above, so a concurrent registry update is a conflict, never a lost
        # update (I2).
        try:
            updated = self._projects.replace(
                derived, now=now, expected_updated_at=project.updated_at
            )
        except RegistryUpdateConflict as exc:
            return await self._stale(proposal, stored, str(exc), now)
        await self._emit_registry_updated(updated, proposal, now)
        # The writeback succeeded; consume the exact single-use approval as
        # terminal accounting.  A failure here never rolls back the completed
        # write (I1).
        try:
            await self._approvals.consume(approval.approval_id, intent)
        except (ApprovalNotFound, ApprovalUnavailable, ApprovalMismatch):
            pass
        decided = self._investigations.transition_proposal_status(
            proposal.proposal_id, RegistryProposalStatus.APPROVED, now=now
        )
        self._events_pub.registry_proposal_decided(decided, decision="approved", occurred_at=now)
        ref = self._record_approval_decision(
            proposal, stored, "approved", now, source_ref=str(updated.project_id)
        )
        return ProposalDecisionOutcome(
            proposal=decided,
            applied=True,
            record=updated,
            evidence=(ref,),
            decision="approved",
            reason="registry scope widened",
        )

    def _decision_lock(self, proposal_id: str) -> asyncio.Lock:
        lock = self._decision_locks.get(proposal_id)
        if lock is None:
            lock = asyncio.Lock()
            self._decision_locks[proposal_id] = lock
        return lock

    # -- rejection / stale ----------------------------------------------------

    async def _reject(
        self,
        proposal: RegistryUpdateProposal,
        approval: ApprovalRecord,
        now: datetime,
    ) -> ProposalDecisionOutcome:
        ref = self._record_approval_decision(
            proposal, approval, "rejected", now
        )
        decided = self._investigations.transition_proposal_status(
            proposal.proposal_id, RegistryProposalStatus.REJECTED, now=now
        )
        self._events_pub.registry_proposal_decided(decided, decision="rejected", occurred_at=now)
        return ProposalDecisionOutcome(
            proposal=decided,
            applied=False,
            evidence=(ref,),
            decision="rejected",
            reason=approval.intent_summary,
        )

    async def _stale(
        self,
        proposal: RegistryUpdateProposal,
        approval: ApprovalRecord,
        reason: str,
        now: datetime,
    ) -> ProposalDecisionOutcome:
        """Mark a proposal stale (no longer applicable) and consume the approval."""
        ref = self._record_approval_decision(proposal, approval, "stale", now)
        decided = self._investigations.transition_proposal_status(
            proposal.proposal_id, RegistryProposalStatus.STALE, now=now
        )
        self._events_pub.registry_proposal_decided(decided, decision="stale", occurred_at=now)
        try:
            await self._approvals.consume(approval.approval_id, registry_update_intent(proposal))
        except (ApprovalUnavailable, ApprovalNotFound):
            pass
        return ProposalDecisionOutcome(
            proposal=decided,
            applied=False,
            evidence=(ref,),
            decision="stale",
            reason=reason,
        )

    # -- identity verification / canonicalization ------------------------------

    async def _verify_container_identity(
        self,
        project: ProjectRecord,
        proposal: RegistryUpdateProposal,
        now: datetime,
    ) -> bool:
        target = self._find_target(project, proposal.proposed_target_id)
        if target is None:
            return False
        try:
            session = await self._sessions.connect(target)
            result = await session.transport.run_argv(
                (
                    "docker",
                    "inspect",
                    "--format",
                    "{{.Id}}",
                    proposal.proposed_container_name,
                ),
                timeout=_IDENTITY_TIMEOUT,
            )
        except (RemotePathError, OSError):
            return False
        return result.exit_status == 0 and bool(result.stdout.strip())

    async def _canonicalize_host_paths(
        self,
        project: ProjectRecord,
        proposal: RegistryUpdateProposal,
        now: datetime,
    ) -> tuple[PurePosixPath, ...] | None:
        """Resolve proposed host paths through the live transport.

        Returns ``None`` (stale) when a proposed path is gone or resolves
        outside an absolute, traversal-free location.
        """
        target = self._find_target(project, proposal.proposed_target_id)
        if target is None:
            return None
        try:
            session = await self._sessions.connect(target)
        except Exception:  # noqa: BLE001 - a lost connection cannot canonicalize
            return None
        canonical: list[PurePosixPath] = []
        for raw in proposal.proposed_paths:
            try:
                resolved = await session.transport.realpath(raw)
            except RemotePathError:
                return None
            resolved_path = PurePosixPath(str(resolved))
            if not resolved_path.is_absolute() or ".." in resolved_path.parts:
                return None
            if resolved_path not in canonical:
                canonical.append(resolved_path)
        return tuple(canonical)

    def _derive_registration(
        self,
        project: ProjectRecord,
        proposal: RegistryUpdateProposal,
        canonical_paths: tuple[PurePosixPath, ...],
    ) -> ProjectRegistration:
        if proposal.kind is RegistryUpdateKind.CONTAINER_REGISTRATION:
            return self._projects.derive_registration_with_updates(
                project,
                service_name=proposal.proposed_service_name,
                container=proposal.proposed_container_name,
            )
        return self._projects.derive_registration_with_updates(
            project,
            service_name=proposal.proposed_service_name,
            host_paths=canonical_paths,
        )

    @staticmethod
    def _find_target(
        project: ProjectRecord, target_id: str
    ) -> TargetRegistration | None:
        for target in project.targets:
            if target.target_id == target_id:
                return target
        return None

    # -- audit / evidence -----------------------------------------------------

    async def _emit_registry_updated(
        self,
        project: ProjectRecord,
        proposal: RegistryUpdateProposal,
        now: datetime,
    ) -> None:
        event = RuntimeEvent(
            event_id=f"evt-{uuid.uuid4().hex[:12]}",
            sequence=0,
            event_type=RuntimeEventType.PROJECT_UPDATED,
            occurred_at=now.astimezone(UTC),
            payload={
                "project_id": project.project_id,
                "proposal_id": proposal.proposal_id,
                "update_kind": proposal.kind.value,
                "service": proposal.proposed_service_name,
                "container": proposal.proposed_container_name,
                "paths": [str(path) for path in proposal.proposed_paths],
            },
        )
        stored = self._events.append(event)
        await self._broker.publish(stored)

    def _record_approval_decision(
        self,
        proposal: RegistryUpdateProposal,
        approval: ApprovalRecord,
        decision: str,
        now: datetime,
        *,
        source_ref: str | None = None,
    ) -> EvidenceReference:
        ref = self._evidence.record_approval_decision(
            agent_run_id=proposal.agent_run_id,
            incident_id=self._incident_id(proposal),
            project_id=proposal.proposed_project_id,
            target_id=proposal.proposed_target_id,
            service_name=proposal.proposed_service_name,
            approval_id=approval.approval_id,
            decision=decision,
            intent_summary=approval.intent_summary,
            source_ref=source_ref or f"proposal:{proposal.proposal_id}",
            created_by="service",
            now=now,
        )
        summary = (
            f"registry proposal {proposal.proposal_id} {decision}; "
            f"approval {approval.approval_id}"
        )
        return EvidenceReference(
            evidence_id=ref.evidence_ref_id,
            operation_id=f"proposal:{proposal.proposal_id}",
            summary=summary,
        )

    # -- helpers --------------------------------------------------------------

    def _default_container_label(self, run: AgentRun, service_name: str) -> str:
        if run.scope.container_name:
            return run.scope.container_name
        svc = self._gateway.resolve_service(
            run.scope.project_id, run.scope.target_id, service_name
        )
        if svc.container_names:
            return svc.container_names[0]
        return service_name

    def _evidence_kwargs(
        self, run: AgentRun, service_name: str, source_ref: str, now: datetime
    ) -> dict[str, object]:
        return {
            "agent_run_id": run.agent_run_id,
            "incident_id": self._incident_id_for_run(run),
            "project_id": run.scope.project_id,
            "target_id": run.scope.target_id,
            "service_name": service_name,
            "source_ref": source_ref,
            "created_by": "agent",
            "now": now,
        }

    def _incident_id_for_run(self, run: AgentRun) -> str:
        try:
            investigation = self._investigations.get_investigation(run.investigation_id)
        except InvestigationNotFound as exc:
            raise RegistryProposalError(
                f"investigation {run.investigation_id!r} not found"
            ) from exc
        return investigation.incident_id

    def _incident_id(self, proposal: RegistryUpdateProposal) -> str:
        try:
            investigation = self._investigations.get_investigation(
                proposal.investigation_id
            )
        except InvestigationNotFound as exc:
            raise RegistryProposalError(
                f"investigation {proposal.investigation_id!r} not found"
            ) from exc
        return investigation.incident_id

    @staticmethod
    def _evidence_ref(ref: EvidenceRef, summary: str) -> EvidenceReference:
        return EvidenceReference(
            evidence_id=ref.evidence_ref_id,
            operation_id=ref.source_ref or "",
            summary=summary,
        )


__all__ = [
    "ProposalDecisionOutcome",
    "ProposalOutcome",
    "RegistryProposalError",
    "RegistryProposalService",
    "registry_update_intent",
]

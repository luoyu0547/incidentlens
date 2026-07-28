"""Skill discovery, validation, and read-only access for investigation skills.

Manages the ``skills/`` tree, validates every skill at startup, wraps
DeepAgents for read-only file access, and exposes machine-readable
evidence policies.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict

from deepagents.backends.composite import CompositeBackend
from deepagents.backends.filesystem import FilesystemBackend
from deepagents.backends.state import StateBackend
from deepagents.middleware.filesystem import (
    FilesystemMiddleware,
    FilesystemPermission,
)
from deepagents.middleware.skills import SkillsMiddleware

# ---------------------------------------------------------------------------
# Known read-only observability tools.  The validator rejects any skill that
# lists a tool not in this set.
# ---------------------------------------------------------------------------

KNOWN_TOOLS: set[str] = {
    "read_file",
    "search_logs",
    "get_service_dependencies",
    "get_slow_traces",
    "get_trace",
    "query_metrics",
    "get_runbook",
    "list_recent_deployments",
}

# ---------------------------------------------------------------------------
# Evidence policy Pydantic model
# ---------------------------------------------------------------------------


class EvidencePolicy(BaseModel):
    """Machine-readable evidence policy for a single investigation skill."""

    model_config = ConfigDict(extra="forbid")

    skill_name: str
    cause_code: str
    required_evidence_types: list[str]
    minimum_independent_evidence: int
    direct_contradictions: list[str]


# ---------------------------------------------------------------------------
# Skill definition (parsed from SKILL.md + evidence-policy.yaml)
# ---------------------------------------------------------------------------


@dataclass
class SkillDefinition:
    """Parsed representation of one investigation skill."""

    name: str
    description: str
    allowed_tools: list[str]
    reference_paths: list[str]
    policy: EvidencePolicy
    body: str = ""


# ---------------------------------------------------------------------------
# Simple result types for read_file / write_file
# ---------------------------------------------------------------------------


@dataclass
class ReadFileResult:
    """Result of a read_file operation on the skills backend."""

    ok: bool
    content: str = ""
    error: str = ""


@dataclass
class WriteFileResult:
    """Result of a write_file operation on the skills backend."""

    ok: bool
    error: str = ""


# ---------------------------------------------------------------------------
# SkillRuntime
# ---------------------------------------------------------------------------


class SkillRuntime:
    """Discover, validate, and provide read-only access to investigation skills.

    Parameters
    ----------
    skills_root:
        Absolute or relative path to the ``skills/`` directory.
    audit_store:
        An :class:`InvestigationAuditStore` used to record skill-scan and
        skill-read audit entries.
    """

    def __init__(self, skills_root: Path, audit_store: Any) -> None:
        self._skills_root = skills_root.resolve()
        self._audit_store = audit_store
        self._definitions: tuple[SkillDefinition, ...] | None = None
        self._cause_code_map: dict[str, EvidencePolicy] = {}

        # Build the DeepAgents backend stack
        self._skills_backend = FilesystemBackend(
            root_dir=self._skills_root, virtual_mode=True
        )
        self._backend = CompositeBackend(
            default=StateBackend(),
            routes={"/skills/": self._skills_backend},
        )
        self._permissions = [
            FilesystemPermission(
                operations=["read"], paths=["/skills/**"], mode="allow"
            ),
            FilesystemPermission(
                operations=["read"], paths=["/**"], mode="deny"
            ),
            FilesystemPermission(
                operations=["write"], paths=["/**"], mode="deny"
            ),
        ]
        self._filesystem = FilesystemMiddleware(
            backend=self._backend, _permissions=self._permissions
        )
        self._skills = SkillsMiddleware(
            backend=self._backend,
            sources=[("/skills/", "IncidentLens")],
        )

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def validate(self) -> tuple[SkillDefinition, ...]:
        """Scan the skills tree, parse every skill, and validate invariants.

        Raises :class:`ValueError` on any structural problem.

        Returns
        -------
        Tuple of validated :class:`SkillDefinition` objects.
        """
        definitions: list[SkillDefinition] = []
        seen_names: set[str] = set()
        seen_cause_codes: set[str] = set()

        if not self._skills_root.is_dir():
            msg = f"skills root does not exist: {self._skills_root}"
            raise ValueError(msg)

        for child in sorted(self._skills_root.iterdir()):
            if not child.is_dir():
                continue
            skill_dir = child
            skill_md = skill_dir / "SKILL.md"
            policy_yaml = skill_dir / "evidence-policy.yaml"
            refs_dir = skill_dir / "references"

            # --- Required files ---
            if not skill_md.exists():
                msg = f"missing SKILL.md in {skill_dir.name}"
                raise ValueError(msg)
            if not policy_yaml.exists():
                msg = f"missing evidence-policy.yaml in {skill_dir.name}"
                raise ValueError(msg)

            # --- Parse SKILL.md frontmatter ---
            raw = skill_md.read_text(encoding="utf-8")
            frontmatter, body = self._parse_frontmatter(raw, skill_md)

            name = frontmatter.get("name", "")
            description = frontmatter.get("description", "")
            allowed_tools_raw = frontmatter.get("allowed-tools", "")

            if not name:
                msg = f"missing 'name' in frontmatter of {skill_md}"
                raise ValueError(msg)

            # --- Duplicate name check (before mismatch) ---
            if name in seen_names:
                msg = f"duplicate skill name: {name}"
                raise ValueError(msg)
            seen_names.add(name)

            # --- Name / directory mismatch ---
            if name != skill_dir.name:
                msg = (
                    f"skill name '{name}' does not match directory "
                    f"'{skill_dir.name}'"
                )
                raise ValueError(msg)

            # --- Unknown tool check ---
            if isinstance(allowed_tools_raw, str):
                tools = [
                    t.strip()
                    for t in allowed_tools_raw.split()
                    if t.strip()
                ]
            else:
                tools = list(allowed_tools_raw) if allowed_tools_raw else []

            unknown = set(tools) - KNOWN_TOOLS
            if unknown:
                msg = f"unknown allowed tool(s) in '{name}': {', '.join(sorted(unknown))}"
                raise ValueError(msg)

            # --- Description length ---
            if len(description) > 1024:
                msg = f"description exceeds 1024 characters in '{name}'"
                raise ValueError(msg)

            # --- References ---
            reference_paths: list[str] = []
            if refs_dir.is_dir():
                for ref in sorted(refs_dir.iterdir()):
                    if ref.is_file():
                        # Check for path traversal via symlinks
                        resolved = ref.resolve()
                        if not resolved.is_relative_to(self._skills_root):
                            msg = f"path traversal detected in references of '{name}'"
                            raise ValueError(msg)
                        rel = ref.relative_to(self._skills_root)
                        reference_paths.append(f"/skills/{rel.as_posix()}")

            # --- Parse evidence-policy.yaml ---
            policy_raw = yaml.safe_load(policy_yaml.read_text(encoding="utf-8"))
            policy = EvidencePolicy.model_validate(policy_raw)

            if policy.skill_name != name:
                msg = (
                    f"evidence-policy skill_name '{policy.skill_name}' "
                    f"does not match skill name '{name}'"
                )
                raise ValueError(msg)

            # --- Unique cause codes ---
            if policy.cause_code in seen_cause_codes:
                msg = f"duplicate cause_code: {policy.cause_code}"
                raise ValueError(msg)
            seen_cause_codes.add(policy.cause_code)

            definitions.append(
                SkillDefinition(
                    name=name,
                    description=description,
                    allowed_tools=tools,
                    reference_paths=reference_paths,
                    policy=policy,
                    body=body,
                )
            )

        self._definitions = tuple(definitions)
        self._cause_code_map = {d.policy.cause_code: d.policy for d in definitions}

        # Audit: record skill_scan with names and paths (not full bodies)
        self._audit_store.record(
            incident_id="startup",
            action="skill_scan",
            details={
                "names": [d.name for d in definitions],
                "paths": [
                    f"/skills/{d.name}/SKILL.md" for d in definitions
                ],
            },
        )

        return self._definitions

    # ------------------------------------------------------------------
    # Progressive disclosure: metadata-only prompt
    # ------------------------------------------------------------------

    def metadata_prompt(self) -> str:
        """Return a prompt listing skill names and descriptions only.

        This is the initial prompt injected at session start so the agent
        knows which skills exist without loading their full bodies.
        """
        if self._definitions is None:
            self.validate()

        lines: list[str] = []
        for d in self._definitions:
            lines.append(f"- **{d.name}**: {d.description}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Middleware access
    # ------------------------------------------------------------------

    def middleware(
        self,
    ) -> tuple[FilesystemMiddleware, SkillsMiddleware, _SkillReadAudit]:
        """Return the three middlewares for agent integration.

        Returns
        -------
        A tuple of ``(filesystem, skills, skill_read_audit)``.
        """
        audit = _SkillReadAudit(self._audit_store)
        return self._filesystem, self._skills, audit

    # ------------------------------------------------------------------
    # Evidence policy lookup
    # ------------------------------------------------------------------

    def policy_for(self, cause_code: str) -> EvidencePolicy:
        """Return the evidence policy for a given cause code.

        Raises :class:`KeyError` if no skill claims this cause code.
        """
        if self._definitions is None:
            self.validate()
        return self._cause_code_map[cause_code]

    # ------------------------------------------------------------------
    # Read / Write with permission enforcement
    # ------------------------------------------------------------------

    async def read_file(self, path: str) -> ReadFileResult:
        """Read a file through the skills backend, enforcing permissions.

        Only paths under ``/skills/`` are allowed.  Path traversal is
        rejected.
        """
        normalised = self._normalise_path(path)
        if not normalised.startswith("/skills/"):
            return ReadFileResult(ok=False, error="access denied: path outside /skills/")
        if self._has_traversal(normalised):
            return ReadFileResult(ok=False, error="access denied: path traversal")

        try:
            result = await self._backend.aread(normalised)
        except Exception as exc:
            return ReadFileResult(ok=False, error=str(exc))

        if result.error:
            return ReadFileResult(ok=False, error=result.error)

        content = result.file_data["content"] if result.file_data else ""
        return ReadFileResult(ok=True, content=content)

    async def write_file(self, path: str, content: str) -> WriteFileResult:
        """Attempt to write a file — always denied by the permission model."""
        return WriteFileResult(ok=False, error="writes are not permitted")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_frontmatter(
        raw: str, path: Path
    ) -> tuple[dict[str, Any], str]:
        """Extract YAML frontmatter and the Markdown body from a SKILL.md."""
        if not raw.startswith("---"):
            msg = f"missing frontmatter delimiters in {path}"
            raise ValueError(msg)

        parts = raw.split("---", 2)
        if len(parts) < 3:
            msg = f"missing frontmatter delimiters in {path}"
            raise ValueError(msg)

        frontmatter_text = parts[1].strip()
        body = parts[2].strip()

        frontmatter = yaml.safe_load(frontmatter_text)
        if not isinstance(frontmatter, dict):
            msg = f"frontmatter is not a mapping in {path}"
            raise ValueError(msg)

        return frontmatter, body

    @staticmethod
    def _normalise_path(path: str) -> str:
        """Ensure the path starts with ``/`` and has no double slashes."""
        if not path.startswith("/"):
            path = "/" + path
        while "//" in path:
            path = path.replace("//", "/")
        return path

    @staticmethod
    def _has_traversal(normalised_path: str) -> bool:
        """Return True if the path contains ``..`` segments."""
        segments = normalised_path.split("/")
        return ".." in segments


# ---------------------------------------------------------------------------
# Skill read audit wrapper
# ---------------------------------------------------------------------------


class _SkillReadAudit:
    """Records audit entries for ``/skills/`` reads via the FilesystemMiddleware."""

    def __init__(self, audit_store: Any) -> None:
        self._audit_store = audit_store

    def record(self, incident_id: str, path: str) -> None:
        """Record a skill-read audit entry."""
        if path.startswith("/skills/"):
            self._audit_store.record(
                incident_id=incident_id,
                action="skill_read",
                details={"path": path},
            )

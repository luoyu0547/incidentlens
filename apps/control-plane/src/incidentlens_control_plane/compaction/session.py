"""Session memory projection and persistence.

Deterministic session memory projection from investigation state and messages.
No model calls required — pure projection logic for fast compaction.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field

from incidentlens_contracts.models import Evidence, Hypothesis
from incidentlens_control_plane.compaction.domain import (
    CompactionConfig,
    CompactionOutcome,
    CompactionResult,
)


class Budget(BaseModel):
    """Budget constraints for session memory."""

    model_config = ConfigDict(extra="forbid")

    max_messages: int = Field(default=200, ge=0)
    max_tokens: int = Field(default=100_000, ge=0)
    current_messages: int = Field(default=0, ge=0)
    current_tokens: int = Field(default=0, ge=0)


class OutputReference(BaseModel):
    """Reference to an output artifact."""

    model_config = ConfigDict(extra="forbid")

    artifact_type: str
    path: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(tz=timezone.utc))


class SessionMemorySnapshot(BaseModel):
    """Projects Objective, verified facts, rejected directions, loaded Skills,
    completed work, next action, constraints, output references, budget,
    recoverable errors.

    This is the deterministic projection of session state — no model calls.
    """

    model_config = ConfigDict(extra="forbid")

    # Incident identification
    incident_id: str
    session_id: str

    # Objective and progress
    objective: str = ""
    current_phase: str = ""
    current_round: int = 0
    max_rounds: int = 8

    # Verified facts and evidence
    verified_facts: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    hypotheses: list[Hypothesis] = Field(default_factory=list)

    # Rejected directions
    rejected_directions: list[str] = Field(default_factory=list)

    # Loaded skills
    loaded_skills: list[str] = Field(default_factory=list)

    # Completed work
    completed_work: list[str] = Field(default_factory=list)

    # Next action
    next_action: str = ""

    # Constraints
    constraints: list[str] = Field(default_factory=list)

    # Output references
    output_references: list[OutputReference] = Field(default_factory=list)

    # Budget tracking
    budget: Budget = Field(default_factory=Budget)

    # Recoverable errors
    recoverable_errors: list[str] = Field(default_factory=list)

    # Metadata
    created_at: datetime = Field(default_factory=lambda: datetime.now(tz=timezone.utc))
    revision: int = Field(default=1, ge=1)


class SessionMemoryValidation(BaseModel):
    """Result of validating a session memory snapshot."""

    model_config = ConfigDict(extra="forbid")

    valid: bool
    missing_evidence_ids: list[str] = Field(default_factory=list)
    missing_hypothesis_ids: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class SessionMemoryStore:
    """Atomic per-incident persistence to .incidentlens/sessions/.

    Writes to a temporary file first, then atomically renames to the
    final path to prevent corruption on crash.
    """

    def __init__(self, base_dir: Path | str) -> None:
        """Initialize the store with a base directory.

        Args:
            base_dir: Root directory for session storage. Will be created
                      if it does not exist.
        """
        self._base_dir = Path(base_dir)
        self._base_dir.mkdir(parents=True, exist_ok=True)

    def _session_path(self, incident_id: str, session_id: str) -> Path:
        """Get the path for a session memory file."""
        return self._base_dir / incident_id / f"{session_id}.json"

    def _session_dir(self, incident_id: str) -> Path:
        """Get the directory for an incident's sessions."""
        return self._base_dir / incident_id

    def save(self, snapshot: SessionMemorySnapshot) -> Path:
        """Atomically persist a session memory snapshot.

        Uses write-to-temp-then-rename pattern for crash safety.

        Returns:
            Path to the persisted snapshot file.
        """
        target_path = self._session_path(snapshot.incident_id, snapshot.session_id)
        target_path.parent.mkdir(parents=True, exist_ok=True)

        # Write to temporary file in the same directory for atomic rename
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=target_path.parent,
                suffix=".tmp",
                prefix=f"{snapshot.session_id}.",
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(snapshot.model_dump_json(indent=2))
                    f.flush()
                    os.fsync(f.fileno())
            except BaseException:
                # Clean up temp file on error
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise

            # Atomic rename
            try:
                os.replace(tmp_path, target_path)
            except BaseException:
                # Clean up temp file on replace failure
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except OSError as e:
            raise RuntimeError(f"Failed to persist session memory: {e}") from e

        return target_path

    def load(self, incident_id: str, session_id: str) -> SessionMemorySnapshot | None:
        """Load a session memory snapshot.

        Returns None if the snapshot does not exist.
        """
        path = self._session_path(incident_id, session_id)
        if not path.exists():
            return None

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return SessionMemorySnapshot(**data)
        except (json.JSONDecodeError, ValueError) as e:
            # Corrupted file - return None rather than raising
            return None

    def list_sessions(self, incident_id: str) -> list[str]:
        """List all session IDs for an incident."""
        incident_dir = self._session_dir(incident_id)
        if not incident_dir.exists():
            return []

        return [
            p.stem
            for p in incident_dir.iterdir()
            if p.suffix == ".json"
        ]

    def delete(self, incident_id: str, session_id: str) -> bool:
        """Delete a session memory snapshot.

        Returns True if deleted, False if not found.
        """
        path = self._session_path(incident_id, session_id)
        if path.exists():
            path.unlink()
            return True
        return False


def project_session_memory(
    state: Mapping[str, Any],
    messages: Sequence[Mapping[str, Any]],
    *,
    session_id: str = "",
) -> SessionMemorySnapshot:
    """Project session memory from state and messages.

    This is a pure function — no model calls, fully deterministic.
    Extracts the essential information from the investigation state
    and message history to create a compact snapshot.

    Args:
        state: Investigation state dictionary (IncidentAgentState fields).
        messages: Message history from the agent.
        session_id: Optional session identifier. If empty, derived from incident_id.

    Returns:
        SessionMemorySnapshot with projected state.
    """
    incident_id = state.get("incident_id", "")
    if not session_id:
        session_id = f"{incident_id}-session"

    # Extract evidence IDs from state
    evidence_list = state.get("evidence", [])
    if isinstance(evidence_list, list):
        evidence_ids = [
            e.get("id", "") if isinstance(e, dict) else getattr(e, "id", "")
            for e in evidence_list
        ]
    else:
        evidence_ids = []

    # Extract hypotheses
    hypotheses_list = state.get("hypotheses", [])
    if isinstance(hypotheses_list, list):
        hypotheses = [
            Hypothesis(**h) if isinstance(h, dict) else h
            for h in hypotheses_list
        ]
    else:
        hypotheses = []

    # Extract loaded skills
    loaded_skills = state.get("loaded_skill_names", [])
    if not isinstance(loaded_skills, list):
        loaded_skills = []

    # Build verified facts from evidence content
    verified_facts: list[str] = []
    for e in evidence_list:
        if isinstance(e, dict):
            content = e.get("content", {})
        else:
            content = getattr(e, "content", {})
        if isinstance(content, dict):
            summary = content.get("summary", "")
            if summary:
                verified_facts.append(summary)

    # Extract constraints from state
    constraints: list[str] = []
    if state.get("max_rounds"):
        constraints.append(f"Max rounds: {state.get('max_rounds', 8)}")
    if state.get("fallback_used"):
        constraints.append("Using fallback model")

    # Extract recoverable errors
    recoverable_errors: list[str] = []
    last_error = state.get("last_error_code")
    if last_error:
        recoverable_errors.append(str(last_error))

    # Calculate budget from messages
    message_count = len(messages)
    # Rough token estimate: ~4 chars per token
    total_chars = sum(
        len(str(m.get("content", "")))
        for m in messages
        if isinstance(m, dict)
    )
    estimated_tokens = total_chars // 4

    budget = Budget(
        max_messages=state.get("max_rounds", 8) * 25,  # Rough estimate
        max_tokens=100_000,
        current_messages=message_count,
        current_tokens=estimated_tokens,
    )

    # Determine next action from state
    next_action = ""
    status = state.get("status", "")
    if status == "needs_more_evidence":
        next_action = "Gather more evidence"
    elif status == "report_ready":
        next_action = "Generate report"
    elif state.get("conclusion_phase"):
        next_action = "Evaluate conclusion"
    else:
        next_action = "Continue investigation"

    return SessionMemorySnapshot(
        incident_id=incident_id,
        session_id=session_id,
        objective=state.get("alert", {}).get("summary", f"Investigate incident {incident_id}"),
        current_phase=state.get("phase", "unknown"),
        current_round=state.get("current_round", 0),
        max_rounds=state.get("max_rounds", 8),
        verified_facts=verified_facts,
        evidence_ids=evidence_ids,
        hypotheses=hypotheses,
        rejected_directions=[],
        loaded_skills=loaded_skills,
        completed_work=[],
        next_action=next_action,
        constraints=constraints,
        output_references=[],
        budget=budget,
        recoverable_errors=recoverable_errors,
    )


def validate_session_memory(
    snapshot: SessionMemorySnapshot,
    evidence_ids: list[str] | None = None,
) -> SessionMemoryValidation:
    """Validate a session memory snapshot against evidence requirements.

    Checks that:
    - All evidence IDs in the snapshot exist in the provided evidence list
    - Snapshot size is within limits
    - Required fields are present

    Args:
        snapshot: The session memory snapshot to validate.
        evidence_ids: List of valid evidence IDs from the investigation.

    Returns:
        SessionMemoryValidation with validation results.
    """
    errors: list[str] = []
    warnings: list[str] = []
    missing_evidence: list[str] = []
    missing_hypothesis: list[str] = []

    # Validate evidence references
    if evidence_ids is not None:
        valid_evidence = set(evidence_ids)
        for ref_id in snapshot.evidence_ids:
            if ref_id not in valid_evidence:
                missing_evidence.append(ref_id)
                errors.append(f"Evidence ID '{ref_id}' not found in investigation")

    # Validate hypothesis evidence references
    if evidence_ids is not None:
        valid_evidence = set(evidence_ids)
        for hyp in snapshot.hypotheses:
            for eid in hyp.supporting_evidence_ids:
                if eid not in valid_evidence:
                    missing_evidence.append(eid)
                    errors.append(f"Hypothesis evidence ID '{eid}' not found")
            for eid in hyp.contradicting_evidence_ids:
                if eid not in valid_evidence:
                    missing_evidence.append(eid)
                    errors.append(f"Hypothesis contradicting evidence ID '{eid}' not found")

    # Check for empty objective
    if not snapshot.objective:
        warnings.append("Session has no objective defined")

    # Check for empty next action
    if not snapshot.next_action:
        warnings.append("Session has no next action defined")

    # Check budget
    if snapshot.budget.current_messages > snapshot.budget.max_messages:
        errors.append(
            f"Message count {snapshot.budget.current_messages} exceeds "
            f"limit {snapshot.budget.max_messages}"
        )

    return SessionMemoryValidation(
        valid=len(errors) == 0,
        missing_evidence_ids=list(set(missing_evidence)),
        missing_hypothesis_ids=list(set(missing_hypothesis)),
        errors=errors,
        warnings=warnings,
    )

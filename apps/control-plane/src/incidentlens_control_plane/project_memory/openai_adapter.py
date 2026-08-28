"""Tool-free Project Memory extraction and selection over the shared transport.

Every model-backed Project Memory operation goes through the Task 1
:class:`~incidentlens_control_plane.investigation.model_transport.OpenAICompatibleTransport`
— no second ``urlopen`` path.  Both calls are tool-free (``"tools": []``) and
bounded:

- :meth:`OpenAIProjectMemoryAdapter.extract` receives only terminal conclusion
  summaries, a bounded Session Memory snapshot, a verification summary and the
  owned Evidence references.  Raw tool output and log bodies are never included
  in the prompt.
- :meth:`OpenAIProjectMemoryAdapter.select` receives only a bounded catalog of
  (id, services, kind, fact) metadata and the current symptom/service scope.

This module also owns the :class:`ProjectMemoryCoordinator`, the runtime service
that (a) enqueues asynchronous extractions off the orchestrator's completion
path and drains them on demand, and (b) renders the bounded advisory attachment
for a fresh parent request with deterministic fallback.  An extraction or
selection failure never alters the investigation's completion state: failures
are recorded as a redacted event and the coordinator degrades to no attachment.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Any

from incidentlens_control_plane.events.broker import RuntimeEventBroker
from incidentlens_control_plane.events.store import RuntimeEventStore
from incidentlens_control_plane.events.types import RuntimeEventType
from incidentlens_control_plane.investigation.events import InvestigationEventPublisher
from incidentlens_control_plane.investigation.model_transport import (
    OpenAICompatibleConfig,
)
from incidentlens_control_plane.project_memory.service import (
    ProjectMemoryService,
    candidate_rejection,
)
from incidentlens_control_plane.project_memory.store import (
    ProjectMemoryNotFound,
    ProjectMemoryStore,
)
from incidentlens_control_plane.project_memory.types import (
    ProjectMemoryCandidate,
    ProjectMemoryEntry,
    ProjectMemoryExtractionRequest,
    ProjectMemoryRejected,
)

_MAX_EXTRACT_CANDIDATES = 5
_MAX_SELECT_LIMIT = 5
_MAX_CATALOG = 100
_MAX_SNAPSHOT_CHARS = 4_000
_MAX_SUMMARY_CHARS = 600

_JSON_INSTRUCTION = (
    "Respond with one strict JSON object only, no prose. "
)

_EXTRACTION_SYSTEM_PROMPT = (
    "You are the IncidentLens project memory extractor.  A verified "
    "investigation of a cloud incident completed, and you must extract at most "
    f"{_MAX_EXTRACT_CANDIDATES} verified, evidence-backed project facts that are "
    "stable and reusable by a future investigation of the same project.  Only "
    "persist what the completed investigation actually confirmed: verified "
    "service relationships, recurring failure modes, successful repairs and "
    "verification procedures, and exercised rollback lessons.  Never invent "
    "conclusions, never persist unverified hypotheses, secrets, credentials, "
    "raw logs, or one-off volatile values presented as stable facts.  "
    "Structure the response exactly as: "
    '{"candidates": [{"memory_id": str, "kind": str, "fact": str, '
    '"service_names": [str], "evidence_ids": [str]}]}.  kind must be one of '
    '"verified_fact", "service_relationship", "failure_mode", "repair", '
    '"rollback_lesson".  Cite only evidence ids from the owned set. '
    + _JSON_INSTRUCTION
)

_SELECT_SYSTEM_PROMPT = (
    "You are the IncidentLens project memory selector.  Given a current "
    "incident symptom, the service scope, and a bounded catalog of active "
    "project memories, choose the most relevant memory_id entries a fresh "
    "investigation should revalidate.  Prefer exact service overlap, then "
    "symptom-term overlap, then newest first.  Return at most the requested "
    "limit ids, using only ids present in the catalog.  Struct the response "
    "exactly as: {\"memory_ids\": [str,...]}.  "
    + _JSON_INSTRUCTION
)


class ProjectMemoryAdapterError(Exception):
    """A model-powered Project Memory call failed and must degrade safely."""


def _content_from_response(response: dict[str, Any]) -> str:
    """Pull the assistant text content out of a chat-completions envelope."""
    if not isinstance(response, dict):
        raise ProjectMemoryAdapterError("project memory response is not an object")
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ProjectMemoryAdapterError("project memory response has no choices")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise ProjectMemoryAdapterError("project memory response has no message")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ProjectMemoryAdapterError("project memory response content is empty")
    return content.strip()


def extract_candidates_from_json(
    raw: dict[str, Any], *, owned_evidence_ids: Iterable[str]
) -> tuple[ProjectMemoryCandidate, ...]:
    """Parse and filter one extraction response into surviving candidates.

    Strict parsing drops any item that is structurally invalid (an unknown
    ``kind`` such as a raw-log wrapper, a missing required field, or an
    oversized field); ``candidate_rejection`` then drops recognized but
    ineligible items (unverified hypothesis, secret-like or oversized facts,
    empty provenance, foreign evidence).  Only fully eligible candidates reach
    the persistence service.
    """
    owned = set(owned_evidence_ids)
    payload = json.loads(_content_from_response(raw))
    if isinstance(payload, dict):
        items = payload.get("candidates")
    else:
        items = payload
    if not isinstance(items, list):
        return ()
    survivors: list[ProjectMemoryCandidate] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            candidate = ProjectMemoryCandidate.model_validate(item)
        except Exception:  # noqa: BLE001 - one malformed item never fails the batch
            continue
        if candidate_rejection(candidate, owned_evidence_ids=owned) is not None:
            continue
        survivors.append(candidate)
        if len(survivors) >= _MAX_EXTRACT_CANDIDATES:
            break
    return tuple(survivors)


def _bounded(value: str, width: int) -> str:
    return value if len(value) <= width else value[:width]


class OpenAIProjectMemoryAdapter:
    """Tool-free extraction and selection through the shared transport."""

    def __init__(
        self,
        config: OpenAICompatibleConfig,
        transport: Any,
        *,
        service: ProjectMemoryService | None = None,
    ) -> None:
        self._config = config
        self._transport = transport
        self._service = service

    # -- extraction -----------------------------------------------------------

    def extract(
        self, request: ProjectMemoryExtractionRequest
    ) -> tuple[ProjectMemoryCandidate, ...]:
        """Ask the model for verified candidates detached from raw tool output."""
        payload = {
            "model": self._config.model,
            "messages": [
                {"role": "system", "content": _EXTRACTION_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        self._bounded_extraction_input(request),
                        ensure_ascii=False,
                    ),
                },
            ],
            "tools": [],
            "response_format": {"type": "json_object"},
        }
        try:
            response = self._transport.chat_completions(payload)
        except Exception as exc:  # noqa: BLE001 - transport failures stay redacted
            raise ProjectMemoryAdapterError(
                "project memory extraction model call failed"
            ) from exc
        try:
            return extract_candidates_from_json(
                response, owned_evidence_ids=request.owned_evidence_ids
            )
        except Exception as exc:  # noqa: BLE001 - a malformed body degrades to none
            raise ProjectMemoryAdapterError(
                "project memory extraction response was not strict JSON"
            ) from exc

    def _bounded_extraction_input(
        self, request: ProjectMemoryExtractionRequest
    ) -> dict[str, object]:
        investigation = request.investigation
        return {
            "project_id": investigation.project_id,
            "service": investigation.service,
            "symptom": investigation.symptom,
            "conclusions": tuple(
                _bounded(item, _MAX_SUMMARY_CHARS)
                for item in request.conclusion_summaries
            ),
            "verification_summary": _bounded(
                request.verification_summary, _MAX_SUMMARY_CHARS
            ),
            "session_memory_snapshot": _bounded(
                request.session_memory_snapshot, _MAX_SNAPSHOT_CHARS
            ),
            "owned_evidence_ids": tuple(request.owned_evidence_ids),
        }

    # -- selection ------------------------------------------------------------

    def select(
        self,
        catalog: Iterable[ProjectMemoryEntry],
        query: dict[str, object],
        limit: int = _MAX_SELECT_LIMIT,
    ) -> tuple[str, ...]:
        """Score a bounded catalog against the query and return chosen ids.

        On any model or parse failure this falls back to the deterministic
        ``service.select_relevant`` selection so the fresh investigation still
        receives a bounded, relevant attachment.
        """
        bounded = tuple(catalog)[:_MAX_CATALOG]
        if not bounded:
            return ()
        try:
            payload = self._build_selection_payload(bounded, query, limit)
            response = self._transport.chat_completions(payload)
            return self._parse_selection_response(response, bounded=bounded)
        except Exception:  # noqa: BLE001 - always degrade to deterministic selection
            return self._deterministic_selection(bounded, query, limit)

    def _build_selection_payload(
        self,
        catalog: tuple[ProjectMemoryEntry, ...],
        query: dict[str, object],
        limit: int,
    ) -> dict[str, object]:
        symptom = str(query.get("symptom") or "")
        services = query.get("services") or ()
        catalog_meta = [
            {
                "memory_id": entry.memory_id,
                "kind": entry.kind.value,
                "service_names": entry.service_names,
                "fact": _bounded(entry.fact, _MAX_SUMMARY_CHARS),
            }
            for entry in catalog
        ]
        user_input = {
            "symptom": symptom,
            "services": tuple(str(item) for item in services),
            "catalog": catalog_meta,
            "limit": limit,
        }
        return {
            "model": self._config.model,
            "messages": [
                {"role": "system", "content": _SELECT_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(user_input, ensure_ascii=False),
                },
            ],
            "tools": [],
            "response_format": {"type": "json_object"},
        }

    def _parse_selection_response(
        self, response: dict[str, Any], *, bounded: tuple[ProjectMemoryEntry, ...]
    ) -> tuple[str, ...]:
        known = {entry.memory_id for entry in bounded}
        payload = json.loads(_content_from_response(response))
        if not isinstance(payload, dict):
            raise ProjectMemoryAdapterError("select response is not a JSON object")
        chosen = payload.get("memory_ids")
        if not isinstance(chosen, list):
            raise ProjectMemoryAdapterError("select response has no memory_ids")
        result: list[str] = []
        for memory_id in chosen:
            if not isinstance(memory_id, str) or memory_id not in known:
                continue
            if memory_id in result:
                continue
            result.append(memory_id)
            if len(result) >= _MAX_SELECT_LIMIT:
                break
        return tuple(result)

    def _deterministic_selection(
        self,
        catalog: tuple[ProjectMemoryEntry, ...],
        query: dict[str, object],
        limit: int,
    ) -> tuple[str, ...]:
        if self._service is None:
            return ()
        project_id = str(query.get("project_id") or "")
        symptom = str(query.get("symptom") or "")
        services = query.get("services") or ()
        try:
            picked = self._service.select_relevant(
                project_id, symptom, tuple(str(item) for item in services), limit=limit
            )
        except Exception:  # noqa: BLE001 - deterministic selection is best-effort
            return ()
        known = {entry.memory_id for entry in catalog}
        return tuple(entry.memory_id for entry in picked if entry.memory_id in known)


class ProjectMemoryCoordinator:
    """Owns automatic extraction and advisory rendering for Project Memory.

    ``enqueue`` schedules one extraction without awaiting the model call, so
    completion never blocks; ``drain_pending`` awaits every scheduled
    extraction.  Extraction is admitted per surviving candidate (parse-time
    rejection plus one atomic ``accept_extracted`` call per candidate), so a
    single model batch can persist its valid entries while its invalid entries
    are dropped without weakening the service's batch atomicity.  Failures
    emit a redacted ``agent_hook`` event and never raise into the caller.
    """

    def __init__(
        self,
        *,
        store: ProjectMemoryStore,
        service: ProjectMemoryService,
        adapter: OpenAIProjectMemoryAdapter | None,
        events: RuntimeEventStore | None = None,
        broker: RuntimeEventBroker | None = None,
    ) -> None:
        self._store = store
        self._service = service
        self._adapter = adapter
        self._publisher = (
            InvestigationEventPublisher(events, broker)
            if events is not None and broker is not None
            else None
        )
        self._pending: set[asyncio.Task[None]] = set()

    # -- extraction -----------------------------------------------------------

    def enqueue(self, request: ProjectMemoryExtractionRequest) -> None:
        """Schedule one extraction without blocking the orchestrator loop."""
        task = asyncio.get_running_loop().create_task(self._extract(request))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def drain_pending(self) -> None:
        """Await every scheduled extraction, then clear the pending set."""
        if not self._pending:
            return
        pending = tuple(self._pending)
        self._pending = set()
        await asyncio.gather(*pending, return_exceptions=True)

    async def _extract(self, request: ProjectMemoryExtractionRequest) -> None:
        investigation = request.investigation
        try:
            if self._adapter is None:
                self._emit_redacted(request, "no model transport; extraction skipped")
                return
            candidates = self._adapter.extract(request)
        except Exception as exc:  # noqa: BLE001 - extraction failure never propagates
            self._emit_redacted(request, self._redacted_reason(exc))
            return
        now = datetime.now(UTC)
        for candidate in candidates:
            entry = self._service.materialize_candidate(
                candidate, investigation, now=now
            )
            try:
                self._service.accept_extracted(
                    (entry,), investigation, request.owned_evidence_ids
                )
            except ProjectMemoryRejected as exc:
                self._emit_redacted(request, self._redacted_reason(exc))

    def _redacted_reason(self, exc: BaseException) -> str:
        message = str(exc)
        if not message:
            return type(exc).__name__
        return _bounded(message, 240)

    def _emit_redacted(
        self, request: ProjectMemoryExtractionRequest, reason: str
    ) -> None:
        if self._publisher is None:
            return
        self._publisher.emit(
            RuntimeEventType.AGENT_HOOK,
            hook_type="project_memory",
            agent_run_id=request.agent_run_id,
            investigation_id=request.investigation.investigation_id,
            action_name="extract_memory",
            status="failed",
            metadata={"reason": reason},
        )

    # -- advisory rendering for a fresh parent inspection ---------------------

    def render_relevant(
        self,
        project_id: str,
        symptom: str,
        services: Iterable[str],
        limit: int = _MAX_SELECT_LIMIT,
    ) -> str:
        """Render a bounded relevant attachment for a fresh investigation.

        Uses model selection when an adapter is wired in, always falling back
        to the deterministic ``service.render_entries`` path; any selection or
        rendering failure degrades to no attachment.
        """
        try:
            if self._adapter is not None:
                catalog = self._store.list_active(project_id, limit=_MAX_CATALOG)
                query = {
                    "project_id": project_id,
                    "symptom": symptom,
                    "services": tuple(services),
                }
                selected_ids = self._adapter.select(catalog, query, limit=limit)
                if selected_ids:
                    selected: list[ProjectMemoryEntry] = []
                    for memory_id in selected_ids:
                        try:
                            selected.append(self._store.get(memory_id))
                        except ProjectMemoryNotFound:
                            continue
                    if selected:
                        return self._service.render_entries(tuple(selected))
        except Exception:  # noqa: BLE001 - degrade to deterministic rendering
            pass
        return self._service.render_relevant(
            project_id, symptom, services, limit=limit
        )


def build_memory_renderer(
    coordinator: ProjectMemoryCoordinator,
) -> Callable[[str, str, Iterable[str]], str]:
    """Return the bounded renderer injected into ``AgentContextManager``."""
    return coordinator.render_relevant


__all__ = [
    "OpenAIProjectMemoryAdapter",
    "ProjectMemoryAdapterError",
    "ProjectMemoryCoordinator",
    "build_memory_renderer",
    "extract_candidates_from_json",
]

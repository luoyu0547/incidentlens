"""Tool-free adapter tests: strict-JSON extraction and deterministic fallback.

These exercise :class:`OpenAIProjectMemoryAdapter` against a fake transport
that returns canned chat-completions envelopes — no network is ever touched.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from incidentlens_control_plane.investigation.state_machine import InvestigationStatus
from incidentlens_control_plane.investigation.types import (
    Investigation,
    InvestigationBudget,
    UsageCounters,
)
from incidentlens_control_plane.project_memory.openai_adapter import (
    OpenAIProjectMemoryAdapter,
    extract_candidates_from_json,
)
from incidentlens_control_plane.project_memory.service import (
    ProjectMemoryService,
)
from incidentlens_control_plane.project_memory.store import ProjectMemoryStore
from incidentlens_control_plane.project_memory.types import (
    ProjectMemoryCandidate,
    ProjectMemoryEntry,
    ProjectMemoryExtractionRequest,
    ProjectMemoryKind,
)


def _investigation(
    *,
    project_id: str = "p1",
    symptom: str = "canary database errors",
) -> Investigation:
    moment = datetime(2026, 8, 20, 10, 0, tzinfo=UTC)
    return Investigation(
        investigation_id="inv-1",
        incident_id="inc-1",
        project_id=project_id,
        target_id="target-1",
        service="canary-db",
        symptom=symptom,
        status=InvestigationStatus.COMPLETED,
        budget=InvestigationBudget(),
        usage=UsageCounters(),
        created_at=moment,
        updated_at=moment,
    )


def _request(*, owned_evidence_ids: tuple[str, ...] = ("ev-1",)) -> ProjectMemoryExtractionRequest:
    return ProjectMemoryExtractionRequest(
        investigation=_investigation(),
        agent_run_id="run-1",
        owned_evidence_ids=owned_evidence_ids,
        conclusion_summaries=("canary db recovered after restarting the pod",),
        session_memory_snapshot='{"objective": "recover canary-db", "confirmed_facts": []}',
        verification_summary="verified via health check",
    )


def _content(*, candidates: object) -> dict[str, object]:
    return {
        "choices": [{"message": {"content": json.dumps(candidates, ensure_ascii=False)}}]
    }


class _FakeTransport:
    """A scripted ``chat_completions`` boundary that records payloads."""

    def __init__(
        self,
        response: dict[str, object] | None = None,
        *,
        error: Exception | None = None,
    ):
        self._response = response
        self._error = error
        self.payloads: list[dict[str, object]] = []

    def chat_completions(self, payload: dict[str, object]) -> dict[str, object]:
        self.payloads.append(payload)
        if self._error is not None:
            raise self._error
        if self._response is None:
            raise AssertionError("no canned response configured")
        return self._response


@pytest.fixture()
def store(tmp_path: Path) -> ProjectMemoryStore:
    created = ProjectMemoryStore(lambda: sqlite3.connect(tmp_path / "runtime.db"))
    created.migrate()
    return created


@pytest.fixture()
def service(store: ProjectMemoryStore) -> ProjectMemoryService:
    return ProjectMemoryService(store)


def _entry(
    memory_id: str,
    *,
    service_names: tuple[str, ...] = ("canary-db",),
    fact: str = "canary-db recovered by restarting the canary pod",
    kind: ProjectMemoryKind = ProjectMemoryKind.FACT,
    source_investigation_id: str = "inv-1",
    evidence_ids: tuple[str, ...] = ("ev-1",),
) -> ProjectMemoryEntry:
    moment = datetime(2026, 8, 20, 10, 0, tzinfo=UTC)
    return ProjectMemoryEntry(
        memory_id=memory_id,
        project_id="p1",
        service_names=service_names,
        fact=fact,
        kind=kind,
        source_investigation_id=source_investigation_id,
        evidence_ids=evidence_ids,
        created_at=moment,
        last_confirmed_at=moment,
    )


def _adapter(
    transport: _FakeTransport,
    *,
    service: ProjectMemoryService | None = None,
) -> OpenAIProjectMemoryAdapter:
    from incidentlens_control_plane.investigation.model_transport import (
        OpenAICompatibleConfig,
    )

    return OpenAIProjectMemoryAdapter(
        OpenAICompatibleConfig(api_key="k", base_url="https://llm.example/v1", model="m"),
        transport,
        service=service,
    )


# -- extraction ---------------------------------------------------------------


def test_extract_parses_strict_json_and_forwards_tool_free_payload() -> None:
    transport = _FakeTransport(
        _content(
            candidates={
                "candidates": [
                    {
                        "memory_id": "mem-1",
                        "kind": "verified_fact",
                        "fact": "canary-db recovered after restarting the canary pod",
                        "service_names": ["canary-db"],
                        "evidence_ids": ["ev-1"],
                    }
                ]
            }
        )
    )
    adapter = _adapter(transport)
    candidates = adapter.extract(_request())

    assert candidates == (
        ProjectMemoryCandidate(
            memory_id="mem-1",
            project_id="",
            service_names=("canary-db",),
            fact="canary-db recovered after restarting the canary pod",
            kind=ProjectMemoryKind.FACT,
            source_investigation_id="",
            evidence_ids=("ev-1",),
        ),
    )
    payload = transport.payloads[0]
    assert payload["tools"] == []
    assert payload["model"] == "m"


def test_extract_drops_unverified_hypothesis_but_keeps_valid_fact() -> None:
    transport = _FakeTransport(
        _content(
            candidates={
                "candidates": [
                    {
                        "memory_id": "mem-bad",
                        "kind": "unverified_hypothesis",
                        "fact": "the outage may be a capacity limit",
                        "service_names": ["canary-db"],
                        "evidence_ids": ["ev-1"],
                    },
                    {
                        "memory_id": "mem-good",
                        "kind": "repair",
                        "fact": "restarting the canary pod repaired canary-db",
                        "service_names": ["canary-db"],
                        "evidence_ids": ["ev-1"],
                    },
                ]
            }
        )
    )
    candidates = _adapter(transport).extract(_request())

    assert [item.memory_id for item in candidates] == ["mem-good"]


def test_extract_drops_foreign_evidence_and_secret_and_oversized_candidates() -> None:
    transport = _FakeTransport(
        _content(
            candidates={
                "candidates": [
                    {
                        "memory_id": "mem-foreign",
                        "kind": "verified_fact",
                        "fact": "service B depends on canary-db",
                        "service_names": ["canary-db"],
                        "evidence_ids": ["ev-foreign"],
                    },
                    {
                        "memory_id": "mem-secret",
                        "kind": "verified_fact",
                        "fact": "the api key = sk-abcd1234efgh5678 for the service",
                        "service_names": ["canary-db"],
                        "evidence_ids": ["ev-1"],
                    },
                    {
                        "memory_id": "mem-huge",
                        "kind": "failure_mode",
                        "fact": "raw log line " + "x" * 2200,
                        "service_names": ["canary-db"],
                        "evidence_ids": ["ev-1"],
                    },
                    {
                        "memory_id": "mem-raw",
                        "kind": "raw_log",
                        "fact": "2026-08-14T10:00:01Z ERROR checkout timeout",
                        "service_names": ["canary-db"],
                        "evidence_ids": ["ev-1"],
                    },
                    {
                        "memory_id": "mem-good",
                        "kind": "failure_mode",
                        "fact": "canary-db fails over when the disk fills",
                        "service_names": ["canary-db"],
                        "evidence_ids": ["ev-1"],
                    },
                ]
            }
        )
    )
    candidates = _adapter(transport).extract(_request())

    assert [item.memory_id for item in candidates] == ["mem-good"]


def test_candidate_rejection_matches_service_rules() -> None:
    rejected = extract_candidates_from_json(
        _content(
            candidates={
                "candidates": [
                    {
                        "memory_id": "m1",
                        "kind": "unverified_hypothesis",
                        "fact": "maybe the disk",
                        "service_names": [],
                        "evidence_ids": ["ev-1"],
                    }
                ]
            }
        ),
        owned_evidence_ids={"ev-1"},
    )
    assert rejected == ()


# -- selection ----------------------------------------------------------------


def test_select_parses_valid_json_and_returns_only_catalog_ids(store: ProjectMemoryStore) -> None:
    store.upsert(_entry("mem-a", fact="canary-db restarts clear the errors"))
    store.upsert(_entry("mem-b", service_names=("auth-api",), fact="auth restart recipe"))
    transport = _FakeTransport(
        _content(candidates={"memory_ids": ["mem-a", "mem-not-in-catalog"]})
    )
    adapter = _adapter(transport)
    catalog = store.list_active("p1", limit=100)

    selected = adapter.select(
        catalog,
        {"project_id": "p1", "symptom": "canary errors", "services": ("canary-db",)},
        limit=5,
    )

    assert selected == ("mem-a",)


def test_select_falls_back_to_deterministic_on_bad_model_output(
    store: ProjectMemoryStore, service: ProjectMemoryService
) -> None:
    store.upsert(_entry("mem-a", fact="canary-db restarts clear the errors"))
    store.upsert(
        _entry("mem-b", service_names=("auth-api",), fact="auth restart recipe")
    )
    transport = _FakeTransport(_content(candidates={"memory_ids": 42}))
    adapter = _adapter(transport, service=service)
    catalog = store.list_active("p1", limit=100)

    selected = adapter.select(
        catalog,
        {"project_id": "p1", "symptom": "restart", "services": ("canary-db",)},
        limit=5,
    )

    assert selected == ("mem-a", "mem-b")


def test_select_falls_back_to_deterministic_on_transport_error(
    store: ProjectMemoryStore, service: ProjectMemoryService
) -> None:
    store.upsert(_entry("mem-a", evidence_ids=("ev-1",)))
    transport = _FakeTransport(error=RuntimeError("boom"))
    adapter = _adapter(transport, service=service)
    catalog = store.list_active("p1", limit=100)

    selected = adapter.select(
        catalog,
        {"project_id": "p1", "symptom": "restart", "services": ("canary-db",)},
        limit=5,
    )

    assert selected == ("mem-a",)

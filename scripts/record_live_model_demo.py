"""Run the real MaaS provider against a disposable, controlled SSH target.

This is a documentation-recording harness, not a mock: it creates a fresh SSH
key, starts ``infra/test-ssh``, writes a known log through the real SSH
transport, and runs the normal IncidentLens orchestrator in ``llm_agent``
mode.  The resulting JSON only contains persisted, redacted runtime records.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from incidentlens_control_plane.config import RuntimeSettings
from incidentlens_control_plane.events.types import RuntimeEventType
from incidentlens_control_plane.investigation.fake_provider import FakeProviderRegistry
from incidentlens_control_plane.investigation.types import (
    AgentBudget,
    AgentScope,
    InvestigationBudget,
    MessageRole,
    TextBlock,
    TranscriptMessage,
)
from incidentlens_control_plane.logs.types import LogScope
from incidentlens_control_plane.project_registry.types import (
    ProjectRegistration,
    ServiceRegistration,
    TargetRegistration,
)
from incidentlens_control_plane.remote_ops.asyncssh_adapter import AsyncSshTransportFactory
from incidentlens_control_plane.remote_ops.sessions import SessionManager
from incidentlens_control_plane.runtime import build_runtime


def _run(command: list[str], *, env: dict[str, str] | None = None) -> str:
    return subprocess.run(command, check=True, text=True, capture_output=True, env=env).stdout


def _host_key(container_name: str) -> str:
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        result = subprocess.run(
            ["docker", "exec", container_name, "cat", "/etc/ssh/ssh_host_ed25519_key.pub"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        time.sleep(0.5)
    raise RuntimeError("timed out waiting for the disposable SSH host key")


async def _seed_log(factory: AsyncSshTransportFactory, target: TargetRegistration) -> None:
    sessions = SessionManager(factory)
    try:
        session = await sessions.connect(target)
        await session.transport.write_bytes(
            PurePosixPath("/workspace/service/live.log"),
            b"2026-08-14T10:00:01Z ERROR checkout request_id=req-42 upstream payment timeout\n"
            b"2026-08-14T10:00:02Z WARN retry exhausted request_id=req-42 status=502\n",
            mode=0o644,
        )
    finally:
        await sessions.close_all()


_CONTEXT_OVERRIDE_FIELDS = frozenset(
    {
        "agent_context_window_tokens",
        "agent_context_max_output_tokens",
        "agent_context_reserve_tokens",
        "agent_tool_result_budget_chars",
        "agent_context_max_message_groups",
        "agent_context_keep_recent_tool_results",
        "agent_compact_max_failures",
        "agent_reactive_keep_recent_groups",
    }
)
_PREFILL_COMPLETE_GROUPS = "prefill_complete_groups"


def _effective_settings(
    settings: RuntimeSettings,
    overrides: dict[str, object] | None,
) -> tuple[RuntimeSettings, int]:
    values = overrides or {}
    unsupported = set(values) - _CONTEXT_OVERRIDE_FIELDS - {_PREFILL_COMPLETE_GROUPS}
    if unsupported:
        raise ValueError(f"unsupported context override: {sorted(unsupported)!r}")
    prefill = values.get(_PREFILL_COMPLETE_GROUPS, 0)
    if not isinstance(prefill, int) or isinstance(prefill, bool) or prefill < 0:
        raise ValueError("prefill_complete_groups must be a non-negative integer")
    updates = {key: values[key] for key in _CONTEXT_OVERRIDE_FIELDS if key in values}
    merged = settings.model_dump(mode="python")
    merged.update(updates)
    return RuntimeSettings.model_validate(merged, strict=True), prefill


@dataclass(frozen=True, slots=True)
class LiveModelRunResult:
    investigation: dict[str, object]
    run: dict[str, object]
    rounds: tuple[dict[str, object], ...]
    tool_calls: tuple[dict[str, object], ...]
    transcript: tuple[dict[str, object], ...]
    compact_boundaries: tuple[dict[str, object], ...]
    evidence: tuple[dict[str, object], ...]
    conclusions: tuple[dict[str, object], ...]
    hooks: tuple[dict[str, object], ...]
    report: dict[str, object]
    provider_type: str = ""
    provider_model: str | None = None
    markdown_path: Path | None = None
    html_path: Path | None = None

    def to_record(self) -> dict[str, object]:
        return {
            "investigation": self.investigation,
            "run": self.run,
            "rounds": list(self.rounds),
            "tool_calls": list(self.tool_calls),
            "transcript": list(self.transcript),
            "compact_boundaries": list(self.compact_boundaries),
            "evidence": list(self.evidence),
            "conclusions": list(self.conclusions),
            "hooks": list(self.hooks),
            "report": self.report,
        }


async def run_live_model_workflow(
    settings: RuntimeSettings,
    factory: AsyncSshTransportFactory,
    target: TargetRegistration,
    service: ServiceRegistration,
    *,
    context_overrides: dict[str, object] | None = None,
    fake_provider_registry: FakeProviderRegistry | None = None,
) -> LiveModelRunResult:
    """Run the recording workflow against an already-started SSH target."""
    effective_settings, prefill_complete_groups = _effective_settings(settings, context_overrides)
    runtime = build_runtime(
        effective_settings,
        transport_factory=factory,
        fake_provider_registry=fake_provider_registry,
    )
    try:
        project_id = "recording-project"
        runtime.projects.create(
            ProjectRegistration(
                project_id=project_id,
                display_name="Live MaaS recording",
                targets=(target,),
                services=(service,),
            ),
            now=datetime.now(UTC),
        )
        investigation = runtime.investigations.create_investigation(
            project_id=project_id,
            target_id=target.target_id,
            service=service.compose_service,
            symptom=(
                "checkout requests return 502; inspect the authorized live log "
                "and identify the observable failure chain"
            ),
            incident_id="recording-incident",
            budget=InvestigationBudget(
                max_rounds=5, max_tool_calls=5, max_no_new_evidence_rounds=2
            ),
        )
        if fake_provider_registry is not None:
            pending = fake_provider_registry.script("pending")
            if not pending:
                pending = fake_provider_registry.script("run-recording")
            if not pending:
                raise ValueError("fake_provider_registry requires a pending recording script")
            fake_provider_registry.set_pending_script(pending)
        async def prefill_context(run: object) -> None:
            if not prefill_complete_groups:
                return
            runtime.investigation_store.append_transcript_message(
                TranscriptMessage(
                    agent_run_id=run.agent_run_id,
                    sequence=1,
                    role=MessageRole.USER,
                    blocks=(TextBlock(text=(
                        "Prefill context follows the ordinary investigation prompt. "
                        "Use only persisted evidence and preserve the task scope."
                    )),),
                    created_at=datetime.now(UTC),
                )
            )
            large_text = "bounded context detail " * 800
            for group in range(prefill_complete_groups):
                base = group * 2 + 2
                for sequence, role in ((base, MessageRole.ASSISTANT), (base + 1, MessageRole.USER)):
                    runtime.investigation_store.append_transcript_message(
                        TranscriptMessage(
                            agent_run_id=run.agent_run_id,
                            sequence=sequence,
                            role=role,
                            blocks=(TextBlock(text=f"prefill group {group}: {large_text}"),),
                            created_at=datetime.now(UTC),
                        )
                    )

        run = await runtime.investigations.start(
            investigation.investigation_id,
            AgentScope(
                project_id=project_id,
                target_id=target.target_id,
                scope=LogScope.HOST,
                allowed_host_paths=(PurePosixPath("/workspace/service"),),
            ),
            parent_budget=AgentBudget(max_rounds=5, max_tool_calls=5, max_no_new_evidence_rounds=2),
            before_run=prefill_context,
        )
        if fake_provider_registry is not None and run.status.value != "completed":
            if run.status.value == "paused_missing_evidence":
                run = await runtime.investigations.resume_run(run.agent_run_id)
            if run.status.value != "completed":
                raise RuntimeError(f"fake recording workflow did not complete: {run.status.value}")
        report = runtime.reports.generate(investigation.investigation_id)
        store = runtime.investigation_store
        investigation_record = runtime.investigations.get_investigation(
            investigation.investigation_id
        ).model_dump(mode="json")
        root_run_id = run.agent_run_id
        owned_run_ids = {root_run_id}
        hooks = tuple(
            event.model_dump(mode="json")
            for event in runtime.events.list_after(0, limit=1_000)
            if event.event_type is RuntimeEventType.AGENT_HOOK
            and event.payload.get("agent_run_id") in owned_run_ids
        )
        return LiveModelRunResult(
            investigation=investigation_record,
            run=run.model_dump(mode="json"),
            rounds=tuple(
                item.model_dump(mode="json") for item in store.list_rounds(run.agent_run_id)
            ),
            tool_calls=tuple(
                item.model_dump(mode="json")
                for item in store.list_tool_calls(agent_run_id=run.agent_run_id)
            ),
            transcript=tuple(
                item.model_dump(mode="json")
                for item in store.list_transcript_messages(run.agent_run_id)
            ),
            compact_boundaries=tuple(
                item.model_dump(mode="json")
                for item in store.list_compact_boundaries(run.agent_run_id)
            ),
            evidence=tuple(
                item.model_dump(mode="json")
                for item in runtime.evidence.list_for_incident(investigation.incident_id)
            ),
            conclusions=tuple(
                item.model_dump(mode="json")
                for item in runtime.investigations.list_conclusions(
                    investigation_id=investigation.investigation_id
                )
            ),
            hooks=hooks,
            report=report.metadata.model_dump(mode="json"),
            provider_type=type(runtime.investigations._orchestrator._provider).__name__,
            provider_model=effective_settings.llm_active_model,
            markdown_path=report.markdown_path,
            html_path=report.html_path,
        )
    finally:
        await runtime.recovery.shutdown()
        await runtime.sessions.close_all()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--report-dir",
        type=Path,
        help="optional directory to receive the real Markdown and HTML report",
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    compose = root / "infra" / "test-ssh" / "compose.yaml"
    temporary = Path(tempfile.mkdtemp(prefix="incidentlens-live-model-"))
    key = temporary / "id_ed25519"
    project_name = "incidentlens-live-model-recording"
    env = {**os.environ, "TEST_AUTHORIZED_KEYS": str(key.with_suffix(".pub"))}

    try:
        _run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)])
        subprocess.run(
            ["docker", "compose", "-f", str(compose), "-p", project_name, "up", "-d", "--build"],
            check=True,
            env=env,
        )
        port = int(
            _run(
                [
                    "docker",
                    "compose",
                    "-f",
                    str(compose),
                    "-p",
                    project_name,
                    "port",
                    "test-ssh",
                    "22",
                ],
                env=env,
            )
            .strip()
            .rsplit(":", 1)[-1]
        )
        container_id = _run(
            ["docker", "compose", "-f", str(compose), "-p", project_name, "ps", "-q", "test-ssh"],
            env=env,
        ).strip()
        container_name = (
            _run(["docker", "inspect", "-f", "{{.Name}}", container_id]).strip().lstrip("/")
        )
        known_hosts = temporary / "known_hosts"
        known_hosts.write_text(f"[127.0.0.1]:{port} {_host_key(container_name)}\n")

        target = TargetRegistration(
            target_id="recording-target", host="127.0.0.1", ssh_user="incidentlens", port=port
        )
        service = ServiceRegistration(
            compose_service="test-ssh",
            container_names=(container_name,),
            allowed_log_paths=("/workspace/service/live.log",),
            allowed_host_paths=(PurePosixPath("/workspace/service"),),
            allowed_container_paths=(PurePosixPath("/workspace/service"),),
        )
        factory = AsyncSshTransportFactory(
            client_key_paths=(str(key),), known_hosts_path=str(known_hosts)
        )
        asyncio.run(_seed_log(factory, target))

        settings = RuntimeSettings.from_environment().model_copy(
            update={
                "data_dir": temporary / "runtime",
                "report_output_dir": temporary / "reports",
                "agent_mode": "llm_agent",
            }
        )
        result = asyncio.run(run_live_model_workflow(settings, factory, target, service))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result.to_record(), ensure_ascii=False, indent=2) + "\n")
        if args.report_dir is not None:
            args.report_dir.mkdir(parents=True, exist_ok=True)
            # Report files remain owned by the runtime's configured report directory.
            if result.markdown_path is not None and result.html_path is not None:
                shutil.copy2(
                    result.markdown_path,
                    args.report_dir / "live-model-report.md",
                )
                shutil.copy2(
                    result.html_path,
                    args.report_dir / "live-model-report.html",
                )
        print(f"wrote {args.output}")
        print(
            f"status={result.run['status']}, rounds={result.run['usage']['rounds']}, "
            f"tools={result.run['usage']['tool_calls']}"
        )
    finally:
        subprocess.run(
            ["docker", "compose", "-f", str(compose), "-p", project_name, "down", "--volumes"],
            check=False,
            env=env,
        )


if __name__ == "__main__":
    main()

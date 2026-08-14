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
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from incidentlens_control_plane.config import RuntimeSettings
from incidentlens_control_plane.investigation.types import (
    AgentBudget,
    AgentScope,
    InvestigationBudget,
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
        runtime = build_runtime(settings, transport_factory=factory)
        runtime.projects.create(
            ProjectRegistration(
                project_id="recording-project",
                display_name="Live MaaS recording",
                targets=(target,),
                services=(service,),
            ),
            now=datetime.now(UTC),
        )
        investigation = runtime.investigations.create_investigation(
            project_id="recording-project",
            target_id=target.target_id,
            service="test-ssh",
            symptom=(
                "checkout requests return 502; inspect the authorized live log "
                "and identify the observable failure chain"
            ),
            incident_id="recording-incident",
            budget=InvestigationBudget(
                max_rounds=5, max_tool_calls=5, max_no_new_evidence_rounds=2
            ),
        )
        run = asyncio.run(
            runtime.investigations.start(
                investigation.investigation_id,
                AgentScope(
                    project_id="recording-project",
                    target_id=target.target_id,
                    scope=LogScope.HOST,
                    allowed_host_paths=(PurePosixPath("/workspace/service"),),
                ),
                parent_budget=AgentBudget(
                    max_rounds=5, max_tool_calls=5, max_no_new_evidence_rounds=2
                ),
            )
        )
        report = runtime.reports.generate(investigation.investigation_id)
        record = {
            "investigation": runtime.investigations.get_investigation(
                investigation.investigation_id
            ).model_dump(mode="json"),
            "run": run.model_dump(mode="json"),
            "rounds": [
                item.model_dump(mode="json")
                for item in runtime.investigation_store.list_rounds(agent_run_id=run.agent_run_id)
            ],
            "tool_calls": [
                item.model_dump(mode="json")
                for item in runtime.investigation_store.list_tool_calls(
                    agent_run_id=run.agent_run_id
                )
            ],
            "evidence": [
                item.model_dump(mode="json")
                for item in runtime.evidence.list_for_incident(investigation.incident_id)
            ],
            "conclusions": [
                item.model_dump(mode="json")
                for item in runtime.investigations.list_conclusions(
                    investigation_id=investigation.investigation_id
                )
            ],
            "report": report.metadata.model_dump(mode="json"),
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
        if args.report_dir is not None:
            args.report_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(report.markdown_path, args.report_dir / "live-model-report.md")
            shutil.copy2(report.html_path, args.report_dir / "live-model-report.html")
        print(f"wrote {args.output}")
        print(f"status={run.status.value}, rounds={run.usage.rounds}, tools={run.usage.tool_calls}")
    finally:
        subprocess.run(
            ["docker", "compose", "-f", str(compose), "-p", project_name, "down", "--volumes"],
            check=False,
            env=env,
        )


if __name__ == "__main__":
    main()

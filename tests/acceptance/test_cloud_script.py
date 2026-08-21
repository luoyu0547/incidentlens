from pathlib import Path


SCRIPT = Path("scripts/cloud_acceptance_target.sh").read_text()


def test_script_exposes_only_target_commands() -> None:
    for command in ("provision", "status", "verify-precondition", "stop"):
        assert command in SCRIPT
    assert "/opt/incidentlens-target" in SCRIPT


def test_script_rejects_broad_or_untrusted_host_inputs() -> None:
    assert "invalid ssh alias" in SCRIPT
    assert "test ! -e /opt/incidentlens" in SCRIPT
    assert "tar -C infra" in SCRIPT
    assert "rm -rf" not in SCRIPT


def test_script_requires_loopback_published_ports() -> None:
    assert "127\\\\.0\\\\.0\\\\.1" in SCRIPT
    assert "compose.cloud.yaml" in SCRIPT

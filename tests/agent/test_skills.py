from pathlib import Path

import pytest

from incidentlens_control_plane.agent.skills import SkillRuntime

EXPECTED = {
    "downstream-timeout",
    "downstream-error",
    "database-pool-exhaustion",
    "dependency-unavailable",
    "deployment-regression",
}


def test_all_five_skills_are_validated_together(investigation_audit_store) -> None:
    runtime = SkillRuntime(Path("skills"), investigation_audit_store)
    definitions = runtime.validate()
    assert {item.name for item in definitions} == EXPECTED
    assert all(item.policy.minimum_independent_evidence >= 2 for item in definitions)
    assert all(item.reference_paths for item in definitions)


def test_initial_skill_prompt_contains_metadata_not_full_body(
    investigation_audit_store,
) -> None:
    runtime = SkillRuntime(Path("skills"), investigation_audit_store)
    prompt = runtime.metadata_prompt()
    assert "downstream-timeout" in prompt
    assert "## Stop conditions" not in prompt
    assert "trace-latency-guide.md" not in prompt


async def test_backend_allows_skill_reads_and_denies_everything_else(
    investigation_audit_store,
) -> None:
    runtime = SkillRuntime(Path("skills"), investigation_audit_store)
    assert (await runtime.read_file("/skills/downstream-timeout/SKILL.md")).ok
    assert not (await runtime.read_file("/etc/passwd")).ok
    assert not (await runtime.write_file("/skills/downstream-timeout/x.md", "x")).ok
    assert not (await runtime.read_file("/skills/../config/models.yaml")).ok


@pytest.mark.parametrize(
    "mutation, message",
    [
        ("duplicate_name", "duplicate skill name"),
        ("unknown_tool", "unknown allowed tool"),
        ("missing_policy", "evidence-policy.yaml"),
        ("missing_frontmatter", "frontmatter"),
        ("path_traversal", "path traversal"),
    ],
)
def test_invalid_skill_fails_startup(
    tmp_path: Path,
    mutation: str,
    message: str,
    investigation_audit_store,
) -> None:
    build_invalid_skill_tree(tmp_path, mutation)
    with pytest.raises(ValueError, match=message):
        SkillRuntime(tmp_path, investigation_audit_store).validate()


def build_invalid_skill_tree(root: Path, mutation: str) -> None:
    valid = """---
name: downstream-timeout
description: Diagnose downstream timeout symptoms.
license: MIT
compatibility: IncidentLens phase 3
metadata:
  version: "1.0.0"
allowed-tools: read_file search_logs
---
# Skill
## Applicable symptoms
## Investigation order
## Candidate hypothesis
## Minimum supporting evidence
## Contradictions
## Stop conditions
## Forbidden behavior
"""
    skill = root / "downstream-timeout"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(valid, encoding="utf-8")
    (skill / "evidence-policy.yaml").write_text(
        "skill_name: downstream-timeout\n"
        "cause_code: payment_latency_spike\n"
        "required_evidence_types: [trace, log]\n"
        "minimum_independent_evidence: 2\n"
        "direct_contradictions: [normal downstream latency]\n",
        encoding="utf-8",
    )
    (skill / "references").mkdir()
    (skill / "references" / "guide.md").write_text("# Guide\n", encoding="utf-8")
    mutate_skill_tree(root, mutation)


def mutate_skill_tree(root: Path, mutation: str) -> None:
    """Apply a single mutation to the valid skill tree for negative testing."""
    match mutation:
        case "duplicate_name":
            dup = root / "duplicate"
            dup.mkdir(parents=True)
            (dup / "SKILL.md").write_text(
                (root / "downstream-timeout" / "SKILL.md").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            (dup / "evidence-policy.yaml").write_text(
                (
                    root / "downstream-timeout" / "evidence-policy.yaml"
                ).read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            (dup / "references").mkdir()
        case "unknown_tool":
            skill_md = root / "downstream-timeout" / "SKILL.md"
            content = skill_md.read_text(encoding="utf-8")
            skill_md.write_text(
                content.replace("search_logs", "delete_database"),
                encoding="utf-8",
            )
        case "missing_policy":
            (root / "downstream-timeout" / "evidence-policy.yaml").unlink()
        case "missing_frontmatter":
            skill_md = root / "downstream-timeout" / "SKILL.md"
            content = skill_md.read_text(encoding="utf-8")
            # Remove both --- delimiters
            skill_md.write_text(
                content.replace("---", ""),
                encoding="utf-8",
            )
        case "path_traversal":
            outside = root.parent / "outside.md"
            outside.write_text("secret", encoding="utf-8")
            refs = root / "downstream-timeout" / "references"
            escape = refs / "escape.md"
            escape.symlink_to(outside)
        case _:
            raise AssertionError(mutation)

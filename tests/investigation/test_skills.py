"""Runtime skill registry tests: discovery, metadata, ordering and safety."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from incidentlens_control_plane.investigation.skills import SkillInfo, SkillRegistry


def write_skill(root: Path, name: str, body: str = "# Heading\n\nbody", *,
                frontmatter: str | None = None) -> Path:
    """Create ``root/<name>/SKILL.md`` and return the directory."""
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    content = body if frontmatter is None else f"{frontmatter}\n{body}"
    (directory / "SKILL.md").write_text(content, encoding="utf-8")
    return directory


def test_none_root_yields_empty_registry() -> None:
    registry = SkillRegistry(root=None)

    assert registry.catalog() == ()
    assert registry.diagnostics == ()
    assert registry.list_skills() == "No skills available."
    assert "unknown skill: anything" in registry.load_skill("anything")


def test_missing_root_yields_empty_registry(tmp_path: Path) -> None:
    registry = SkillRegistry(root=tmp_path / "does-not-exist")

    assert registry.catalog() == ()
    assert registry.diagnostics == ()


def test_catalog_sorted_by_name(tmp_path: Path) -> None:
    write_skill(tmp_path, "zed", frontmatter="---\nname: zeta\n---\n# Zeta\nbody zeta")
    write_skill(tmp_path, "alpha", frontmatter="---\nname: alpha\n---\n# Alpha\nbody alpha")
    write_skill(tmp_path, "mid", frontmatter="---\nname: mike\n---\n# Mike\nbody mike")

    registry = SkillRegistry(root=tmp_path)

    assert [skill.name for skill in registry.catalog()] == ["alpha", "mike", "zeta"]


def test_frontmatter_metadata_parsed(tmp_path: Path) -> None:
    write_skill(
        tmp_path,
        "db-pool",
        body="# Database Pool Exhaustion\n\nCheck the pool settings.",
        frontmatter=(
            "---\n"
            "name: database-pool\n"
            "description: Diagnose connection pool exhaustion\n"
            "unknown-key: ignored\n"
            "---\n"
        ),
    )

    registry = SkillRegistry(root=tmp_path)

    (skill,) = registry.catalog()
    assert isinstance(skill, SkillInfo)
    assert skill.name == "database-pool"
    assert skill.description == "Diagnose connection pool exhaustion"
    assert skill.body.startswith("# Database Pool Exhaustion\n\nCheck the pool settings.")

    loaded = registry.load_skill("database-pool")
    assert loaded == "# Database Pool Exhaustion\n\nCheck the pool settings."


def test_fallback_metadata_when_no_frontmatter(tmp_path: Path) -> None:
    write_skill(tmp_path, "fallback-skill", body="# Fallback Heading\n\nSome body.")

    registry = SkillRegistry(root=tmp_path)

    (skill,) = registry.catalog()
    assert skill.name == "fallback-skill"
    assert skill.description == "Fallback Heading"
    assert skill.body == "# Fallback Heading\n\nSome body."


def test_unknown_name_load_returns_bounded_diagnostic(tmp_path: Path) -> None:
    write_skill(tmp_path, "alpha", body="# Alpha\nbody")
    write_skill(tmp_path, "beta", body="# Beta\nbody")

    registry = SkillRegistry(root=tmp_path)

    result = registry.load_skill("nope")
    assert "unknown skill: nope" in result
    assert "alpha" in result
    assert "beta" in result
    # bounded: never the body, never a path
    assert "Alpha" not in result
    assert "/" not in result


def test_traversal_and_absolute_names_rejected_as_unknown(tmp_path: Path) -> None:
    write_skill(tmp_path, "alpha", body="# Alpha\nbody")

    registry = SkillRegistry(root=tmp_path)

    for name in ("../alpha", "/etc/passwd", "alpha/../../x"):
        result = registry.load_skill(name)
        assert f"unknown skill: {name}" in result
        assert "Alpha" not in result


def test_non_directory_entry_skipped_with_diagnostic(tmp_path: Path) -> None:
    (tmp_path / "loose-file.txt").write_text("not a skill", encoding="utf-8")
    write_skill(tmp_path, "alpha", body="# Alpha\nbody")

    registry = SkillRegistry(root=tmp_path)

    names = {skill.name for skill in registry.catalog()}
    assert names == {"alpha"}
    assert any("loose-file.txt" in d and "not a directory" in d for d in registry.diagnostics)


def test_missing_skill_md_skipped_with_diagnostic(tmp_path: Path) -> None:
    (tmp_path / "empty-dir").mkdir()
    write_skill(tmp_path, "alpha", body="# Alpha\nbody")

    registry = SkillRegistry(root=tmp_path)

    assert {skill.name for skill in registry.catalog()} == {"alpha"}
    assert any(
        "empty-dir" in d and "SKILL.md" in d and "missing" in d
        for d in registry.diagnostics
    )


def test_unreadable_skill_md_skipped_with_diagnostic(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("cannot simulate unreadable file as root")
    directory = write_skill(tmp_path, "locked", body="# Locked\nbody")
    path = directory / "SKILL.md"
    path.chmod(0o000)
    try:
        registry = SkillRegistry(root=tmp_path)
    finally:
        path.chmod(0o644)

    assert {skill.name for skill in registry.catalog()} == set()
    assert any(
        "locked" in d and "SKILL.md" in d and "unreadable" in d
        for d in registry.diagnostics
    )


def test_only_immediate_children_scanned(tmp_path: Path) -> None:
    write_skill(tmp_path, "outer", body="# Outer\nbody")
    write_skill(tmp_path, "outer/inner", body="# Inner\nbody")

    registry = SkillRegistry(root=tmp_path)

    # ``outer`` is an immediate child; ``outer/inner`` is nested and not scanned.
    assert [skill.name for skill in registry.catalog()] == ["outer"]

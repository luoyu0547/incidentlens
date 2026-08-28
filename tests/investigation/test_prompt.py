"""SystemPromptBuilder tests: stable guidance, dynamic context and caching."""

from __future__ import annotations

from pathlib import Path

from incidentlens_control_plane.investigation.prompt import (
    PromptContext,
    SystemPromptBuilder,
)
from incidentlens_control_plane.investigation.skills import SkillInfo, SkillRegistry
from incidentlens_control_plane.logs.types import LogScope


def _write_skill(
    root: Path, name: str, *, description: str, body: str = "# Body\n\ncontent"
) -> None:
    """Write ``root/<name>/SKILL.md`` with frontmatter and return nothing."""
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n{body}",
        encoding="utf-8",
    )


def _context(
    *,
    tool_names: tuple[str, ...] = ("log_query", "file_edit"),
    scope: LogScope = LogScope.HOST,
    is_child: bool = False,
    memory_present: bool = True,
    skills: tuple[SkillInfo, ...] = (),
) -> PromptContext:
    return PromptContext(
        tool_names=tool_names,
        scope=scope,
        is_child=is_child,
        memory_present=memory_present,
        skill_catalog=skills,
    )


def test_stable_guidance_present_and_ordered_before_dynamic() -> None:
    prompt = SystemPromptBuilder().build(_context())

    assert "受保护路径" in prompt
    assert prompt.index("受保护路径") < prompt.index("当前运行动态上下文")


def test_dynamic_tool_and_skill_text_reflects_context(tmp_path: Path) -> None:
    _write_skill(tmp_path, "db-pool", description="排查连接池耗尽")
    registry = SkillRegistry(root=tmp_path)

    prompt = SystemPromptBuilder().build(
        _context(
            tool_names=("file_edit", "log_query"),
            scope=LogScope.CONTAINER,
            is_child=True,
            memory_present=False,
            skills=registry.catalog(),
        )
    )

    assert "- 运行作用域（scope）：container" in prompt
    assert "- 本次是否为子任务：是" in prompt
    assert "- 会话记忆可用：否" in prompt
    assert "- 本次运行注册的可用工具：file_edit, log_query" in prompt
    assert "- 已注册技能目录：" in prompt
    assert "- db-pool：排查连接池耗尽" in prompt
    # skills render name + description only, never the body
    assert "# Body" not in prompt


def test_dynamic_skills_empty_catalog_renders_no_skills() -> None:
    prompt = SystemPromptBuilder().build(_context(skills=SkillRegistry(root=None).catalog()))

    assert "- 已注册技能目录：无" in prompt


def test_cache_identity_equal_contexts() -> None:
    builder = SystemPromptBuilder()

    first = builder.build(_context())
    second = builder.build(_context())

    assert first is second


def test_cache_key_is_order_independent_for_tools_and_skills(tmp_path: Path) -> None:
    _write_skill(tmp_path, "beta", description="技能 B")
    _write_skill(tmp_path, "alpha", description="技能 A")
    registry = SkillRegistry(root=tmp_path)

    builder = SystemPromptBuilder()
    left = builder.build(
        _context(tool_names=("file_edit", "log_query"), skills=registry.catalog())
    )
    right = builder.build(
        _context(tool_names=("log_query", "file_edit"), skills=registry.catalog())
    )

    assert left is right


def test_invalidation_on_changed_tool_names() -> None:
    builder = SystemPromptBuilder()
    base = builder.build(_context(tool_names=("log_query", "file_edit")))
    changed = builder.build(_context(tool_names=("log_query", "file_edit", "host_read")))

    assert changed != base


def test_invalidation_on_changed_scope() -> None:
    builder = SystemPromptBuilder()
    base = builder.build(_context(scope=LogScope.HOST))
    changed = builder.build(_context(scope=LogScope.CONTAINER))

    assert changed != base


def test_invalidation_on_changed_is_child() -> None:
    builder = SystemPromptBuilder()
    base = builder.build(_context(is_child=False))
    changed = builder.build(_context(is_child=True))

    assert changed != base


def test_invalidation_on_changed_memory_present() -> None:
    builder = SystemPromptBuilder()
    base = builder.build(_context(memory_present=True))
    changed = builder.build(_context(memory_present=False))

    assert changed != base


def test_invalidation_on_changed_skill_catalog(tmp_path: Path) -> None:
    _write_skill(tmp_path, "alpha", description="技能 A")
    registry = SkillRegistry(root=tmp_path)

    builder = SystemPromptBuilder()
    base = builder.build(_context(skills=SkillRegistry(root=None).catalog()))
    changed = builder.build(_context(skills=registry.catalog()))

    assert changed != base


def test_prompt_never_encodes_round_or_stage_schedule() -> None:
    prompt = SystemPromptBuilder().build(_context())

    assert "本轮为" not in prompt
    assert "只能调用 file_edit" not in prompt
    assert "必须只调用 compact_context" not in prompt

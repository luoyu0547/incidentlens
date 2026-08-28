# System Prompt and Skill Loading Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a deterministic, cached runtime system-prompt builder and safe two-level skill loading to the IncidentLens Agent Runtime.

**Architecture:** Keep provider tool schemas structured, while a new prompt module assembles stable and dynamic text sections from a canonical context. Add a filesystem-backed runtime skill registry with name-only lookup, expose its catalog in the prompt, and bind `list_skills`/`load_skill` as read-only registry tools. Pass the rendered prompt into context budgeting so the budget matches the actual request.

**Tech Stack:** Python 3.12, Pydantic models, existing ToolRegistry/ToolExecutor, pytest/pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-08-28-system-prompt-skill-loading-design.md`

## Global Constraints

- Runtime skills are separate from `.claude/skills` and are never loaded from that directory.
- Existing policy, approval, scope, evidence, and hook enforcement remains authoritative.
- Model-provided skill names are resolved only through the in-memory registry; no arbitrary path is opened.
- Skill catalog and loaded content are bounded and deterministic.

### Task 1: Add runtime skill registry

**Files:**
- Create: `apps/control-plane/src/incidentlens_control_plane/investigation/skills.py`
- Test: `tests/investigation/test_skills.py`

**Interfaces:**
- `SkillRegistry(root: Path | None = None)`
- `SkillRegistry.list_skills() -> str`
- `SkillRegistry.load_skill(name: str) -> str`
- `SkillRegistry.catalog() -> tuple[SkillInfo, ...]`

- [ ] **Step 1: Write failing tests** for sorted catalog output, frontmatter/fallback metadata, unknown names, malformed files, and traversal-like names.
- [ ] **Step 2: Run `uv run pytest -q tests/investigation/test_skills.py` and verify failure.**
- [ ] **Step 3: Implement bounded immediate-child scanning, simple frontmatter parsing, deterministic ordering, and name-only loading.
- [ ] **Step 4: Run the skill tests and verify they pass.**

### Task 2: Register skill tools in the existing executor

**Files:**
- Modify: `apps/control-plane/src/incidentlens_control_plane/investigation/tools.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/investigation/tool_executor.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/runtime.py`
- Test: `tests/investigation/test_tool_executor.py`

**Interfaces:**
- Add `TOOL_LIST_SKILLS` and `TOOL_LOAD_SKILL` definitions with zero-argument and name-argument schemas.
- Inject one `SkillRegistry` into `ToolExecutor`; handlers return bounded text through normal `ToolOutcome` handling.

- [ ] **Step 1: Add failing tests** asserting schemas are visible, tools need no approval, and load uses registry names without opening a supplied path.
- [ ] **Step 2: Run the focused tests and verify failure.**
- [ ] **Step 3: Add definitions, dependency injection, and async handlers while preserving existing evidence/redaction behavior.
- [ ] **Step 4: Run focused executor/runtime tests and verify they pass.**

### Task 3: Build and cache dynamic system prompts

**Files:**
- Create: `apps/control-plane/src/incidentlens_control_plane/investigation/prompt.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/investigation/openai_provider.py`
- Test: `tests/investigation/test_prompt.py`
- Modify: `tests/investigation/test_openai_provider.py`

**Interfaces:**
- `PromptContext` captures tool names, scope, child-task flag, memory-present flag, and skill catalog.
- `SystemPromptBuilder.build(context: PromptContext) -> str` returns a deterministic cached string.

- [ ] **Step 1: Add failing tests** for stable section ordering, dynamic tool/skill text, cache identity on equal contexts, and invalidation on changed context.
- [ ] **Step 2: Run focused prompt/provider tests and verify failure.**
- [ ] **Step 3: Move stable guidance into sections, implement canonical JSON cache keys, and have `_system_prompt` delegate to the builder using request data and registry catalog.
- [ ] **Step 4: Run prompt/provider tests and verify they pass.**

### Task 4: Include the rendered prompt in context budgeting

**Files:**
- Modify: `apps/control-plane/src/incidentlens_control_plane/investigation/context.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/runtime.py`
- Test: `tests/investigation/test_context.py`

- [ ] **Step 1: Add a failing test** showing a non-empty rendered prompt contributes to `ContextBudget.system_tokens`.
- [ ] **Step 2: Run the focused context test and verify failure.**
- [ ] **Step 3: Wire the same builder/rendered prompt into `ContextBudgetPolicy` or its estimator input without double-counting attachments.
- [ ] **Step 4: Run context, provider, and runtime tests; verify the full investigation test subset passes.**

### Task 5: Final verification and documentation

**Files:**
- Modify: `README.md` only if runtime skill configuration needs user-facing documentation.

- [ ] **Step 1: Run `uv run pytest -q tests/investigation tests/integration/test_live_model_workflow_unit.py`.**
- [ ] **Step 2: Run lint/type checks available in the repository.**
- [ ] **Step 3: Review the diff for accidental `.claude/skills` coupling, unbounded reads, or prompt-budget mismatches.**


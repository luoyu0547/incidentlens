# IncidentLens System Prompt and Skill Loading Design

## Goal

Give the IncidentLens Agent Runtime a state-aware, cached system-prompt builder and a safe two-level skill-loading mechanism without weakening existing execution policy, approval, scope, or evidence controls.

## Scope

The change applies to the Agent Runtime provider/context path. It does not change Claude Code's own `.claude/skills` behavior and does not scan `.claude/skills` as runtime skills.

## Design

### System prompt

Create a focused prompt module with stable sections for identity, safety/evidence discipline, investigation guidance, and output contract. Dynamic sections expose the current registered skill catalog and bounded runtime state such as the active tool names, run scope, child-task status, and whether session/project memory is available. The builder returns one deterministic string and caches it by a canonical JSON context key; a changed tool set, scope, or memory state produces a new value.

The OpenAI-compatible provider calls the builder for every request. Tool schemas continue to be sent through the provider's structured `tools` field, while the prompt only names the available tools. The context manager receives the same rendered system prompt (or an equivalent token-counting callback) so budget estimates include the real prompt rather than the current empty default.

### Skill loading

Create a runtime skill registry that scans a configured `skills/` directory (with an explicit injectable root for tests), accepting only immediate child directories containing `SKILL.md`. Parse a small YAML-like frontmatter subset for `name` and `description`; fall back to directory name and the first heading when absent. Registry entries retain bounded metadata and the full markdown body.

Expose two read-only operations:

1. `list_skills()` returns a compact catalog for the dynamic prompt section.
2. `load_skill(name)` resolves only registry names and returns the skill markdown as a tool result. Unknown names return a bounded diagnostic. No model-provided path is opened, so traversal is impossible.

The runtime tool registry registers these operations for every agent scope. They have no approval requirement and do not create evidence or mutate the investigation. Skill load failures degrade to a tool result; malformed or unreadable files are skipped from the catalog with deterministic diagnostics available to logs/tests.

### Compatibility and safety

Existing provider output validation, ToolExecutor policy checks, approvals, scope validation, and transcript persistence remain authoritative. Hooks remain non-authorizing. Runtime skills are deliberately separate from development-only `.claude/skills`.

## Verification

- Prompt sections are deterministic, dynamic state changes invalidate the cache, and identical contexts hit the cache.
- Actual registered tool names and skill catalog appear in the prompt/request without hard-coded round schedules.
- Context budgeting counts the rendered system prompt.
- Skill frontmatter parsing, fallback metadata, catalog ordering, unknown skill handling, and traversal-resistant loading are tested.
- Runtime tool schemas expose `list_skills` and `load_skill`; loading a skill adds bounded content to the conversation through the normal tool-result path.
- Existing hooks, provider, context, and runtime test suites continue to pass.

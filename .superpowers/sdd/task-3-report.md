# Task 3: Make LangGraph the Single Agent Checkpoint Source

## Status: DONE

## What was done

### Files Created

1. **`apps/control-plane/src/incidentlens_control_plane/agent/types.py`**
   - `IncidentAgentState` -- extends `langchain.agents.middleware.AgentState` with investigation fields; uses `Annotated` reducers for `evidence` (merge-by-id), `loaded_skill_names` (dedupe), `model_call_count`/`tool_call_count` (additive).
   - `InvestigationContext` -- immutable Pydantic model holding `incident_id` and `RuntimeMode`.
   - `RootCauseProposal` -- strict Pydantic model (`extra="forbid"`) for structured root-cause output.

2. **`apps/control-plane/src/incidentlens_control_plane/agent/projection.py`**
   - `project_investigation_state(raw)` -- validates each `Hypothesis`/`Evidence` individually, then constructs an `InvestigationState` from the raw mapping. Does not fill absent fields from a second database.

3. **`apps/control-plane/src/incidentlens_control_plane/agent/checkpoint.py`**
   - `AgentCheckpointRuntime` -- async context manager that owns an `aiosqlite.Connection` and an `AsyncSqliteSaver`. Exposes `saver` for graph compilation and `config_for(incident_id)` returning `{"configurable": {"thread_id": incident_id}}`.

4. **`tests/agent/test_langgraph_state.py`**
   - `test_projection_validates_domain_state` -- verifies projection round-trips a full raw dict to `InvestigationState`.
   - `test_sqlite_checkpoint_uses_incident_id_as_thread_id` -- builds a minimal `StateGraph(IncidentAgentState)`, compiles with `AgentCheckpointRuntime.saver`, invokes via `ainvoke`, and asserts `thread_id == incident_id` and state values persisted.

### Files Modified

5. **`apps/control-plane/src/incidentlens_control_plane/agent/state.py`**
   - Added 7 new fields to `InvestigationState`: `loaded_skill_names`, `model_profile`, `model_call_count`, `tool_call_count`, `fallback_used`, `last_error_code`, `last_checkpoint_id` -- all with explicit defaults.
   - Updated `CheckpointStore` docstring to mark it as deterministic-baseline compatibility only.
   - Fixed `CheckpointStore.load` to use the `incident_id` parameter instead of the redundant `row.incident_id`.

## Design decisions

- **Connection lifecycle**: `AgentCheckpointRuntime` manages `aiosqlite.connect()` directly rather than using `AsyncSqliteSaver.from_conn_string()`. The `from_conn_string` context manager closes the connection on exit, making the saver unusable outside its scope. Direct connection management keeps the saver alive for the full context duration.
- **Lazy schema setup**: `AsyncSqliteSaver.aput()` calls `setup()` internally, so explicit table creation is unnecessary.
- **`langchain.agents.middleware.AgentState`**: Confirmed available in the installed langchain version. `IncidentAgentState` inherits its `messages` field and `add_messages` reducer.

## Commits

- `6d3248c` feat: add langgraph investigation checkpoints

## Test summary

267 passed, 1 skipped, 25 errors (Docker integration tests requiring a running daemon -- pre-existing, unrelated). 0 failures.

## Report file

`/Users/chenxueqiang/Documents/code/incidentlens/.superpowers/sdd/task-3-report.md`

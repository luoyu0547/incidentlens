# Phase 4 Live Verification

## Status: PENDING

Live verification requires a configured OpenAI-compatible provider.
Run the following commands to complete verification:

### Step 1: Provider capability probe

```bash
set -a && source .env && set +a
uv run pytest tests/live_llm/test_model_contract.py -m live_llm -vv -s
```

Expected:
- `test_selected_profile_performs_real_required_tool_call` PASSED
- `test_conclusion_canary_validates_provider_schema_call` PASSED
- Normal tool call and schema-constrained proposal call both succeed
- No fallback used

### Step 2: Deterministic Compose regression

```bash
INCIDENTLENS_AGENT_MODE=deterministic_baseline \
uv run pytest \
  tests/integration/test_compose_flow.py \
  tests/integration/test_scenario_acceptance.py \
  -m integration -q
```

Expected:
- All 5 scenarios produce correct root service
- Non-empty current evidence references
- API/CLI does not leak `root_cause_label`

### Step 3: Real provider payment_delay acceptance

```bash
set -a && source .env && set +a
uv run pytest \
  tests/integration/test_live_agent_compose.py::test_real_model_completes_payment_delay_investigation \
  -m "integration and live_llm" -vv -s
```

Expected:
- Skill `downstream-timeout` loaded
- At least 2 independent material evidence sources before conclusion boundary
- No observability tool calls after conclusion boundary
- Proposal is model-generated and passes report gate
- Status is `report_ready`
- No fallback used

## Verification Record

| Check | Status | Notes |
|-------|--------|-------|
| Provider canary (normal tool call) | ✅ PASSED | xfyun-xopglm51 via OpenAI-compatible adapter |
| Schema canary (proposal tool call) | ✅ PASSED | Normal + schema-constrained both succeed, no fallback |
| Deterministic Compose | PENDING | Requires Docker Compose |
| Real payment_delay | PENDING | Requires Docker Compose |

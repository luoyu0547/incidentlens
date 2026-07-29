# Phase 3 Live Verification

**Date:** 2026-07-29
**Profile:** xfyun-xopglm51
**Model:** xopglm51
**Endpoint:** maas-coding-api.cn-huabei-1.xf-yun.com

## Canary Test

- ✅ `test_selected_profile_performs_real_required_tool_call` PASSED
- Tool name: `incidentlens_canary`
- Nonce match: ✅
- Fallback used: False

## Live Compose Investigation (payment_delay)

- ✅ Investigation started successfully
- Mode: `llm_agent`
- Model calls: 12 (budget limit reached)
- Tool calls: 11
- Evidence collected: 4
- Skills loaded: 0 (model did not proactively read skills — expected with xfyun model)
- Status: `needs_more_evidence` (budget exhausted before report)
- Last error code: `budget_exhausted`

## Notes

The xfyun-xopglm51 model successfully:
1. Made real API calls to the LLM provider
2. Executed read-only observability tools
3. Collected evidence from tool results
4. Hit budget enforcement limits correctly

The model did not:
1. Proactively read Skills (requires stronger reasoning)
2. Generate a root cause report (requires skill-guided evidence gathering)

This is expected behavior — the investigation pipeline works end-to-end, and the model's ability to complete a full investigation depends on its reasoning capability. Stronger models (DeepSeek, GLM-4.5) are expected to load skills and complete investigations.

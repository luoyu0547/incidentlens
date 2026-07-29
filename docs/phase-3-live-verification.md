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
- Tool calls: 7
- Evidence collected: 6
- Skills loaded: `downstream-timeout`
- Real telemetry collected:
  - Five payment traces with approximately 6-second duration
  - A complete representative trace containing the payment completion span
  - `Payment processing delay observed: 6000ms`
  - `payment_latency_ms=6000`
- Status: `needs_more_evidence` (budget exhausted before report)
- Last error code: `budget_exhausted`

## Phase 3 Review Fixes

- Project accepted structured proposals into the public report state.
- Require the matching Skill before the report gate can accept a proposal.
- Persist Skill reads and Skill load failures in per-incident state.
- Keep invalid-tool counters isolated per incident.
- Bind observability calls to the active incident on the server.
- Return useful bounded tool-result summaries to the model.
- Deduplicate identical observability calls.
- Resume completed agent turns with an explicit continuation message.
- Emit observable completion telemetry for delayed payment calls.
- Extend real-model request timeouts and make the Compose delay detectable.
- Repair affected API/runtime tests and application initialization.

## Deferred to the Next Phase

The configured xfyun model gathers sufficient current-incident evidence but does
not reliably emit `RootCauseProposal`. It continues issuing observability queries
until the model-call budget is exhausted. The provider also does not reliably
honor the requested structured tool choice.

The next phase must implement and verify a provider-compatible convergence path:

1. Once the report policy has sufficient material evidence, invoke a bounded
   conclusion step that exposes only the structured proposal schema.
2. Preserve model authorship of the conclusion; do not hard-code a root cause.
3. Record structured-output validation and report-gate rejection reasons in the
   audit trail.
4. Stop further observability calls after the conclusion boundary is reached.
5. Pass both the live model canary and
   `test_real_model_completes_payment_delay_investigation`.

Until those conditions pass, Phase 3 is **functionally implemented but not fully
accepted by the real-model Compose criterion**.

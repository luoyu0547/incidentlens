# Task 3 Report: Evidence Rules and Guarded Root-Service Reports

## What Was Implemented

### 1. Evidence Rules Module (`evidence_rules.py`)
- Created `EvidenceAssessment` Pydantic model with `candidate_service`, `root_cause`, `supports`, `contradicts` fields
- Implemented `assess_evidence(evidence) -> list[EvidenceAssessment]` with deterministic pattern matching
- Five mappings implemented:
  - `payment_delay` -> `payment-service` / `payment_latency_spike` (via search_logs latency keywords, get_slow_traces duration, query_metrics latency values)
  - `payment_error_rate` -> `payment-service` / `payment_service_degradation` (via search_logs error keywords, query_metrics error_rate values)
  - `db_pool_exhaustion` -> `order-service` / `database_connection_leak` (via search_logs pool/connection keywords, query_metrics pool metrics)
  - `dependency_unavailable` -> `order-service` / `network_partition` (via search_logs dependency/502 keywords, get_service_dependencies order->payment edges)
  - `deployment_regression` -> `payment-service` / `bad_deployment` (via list_recent_deployments with any version for payment-service)
- Pattern matchers are dispatched by `source_tool` to the appropriate assessor function
- Evidence that doesn't match any pattern returns an empty list

### 2. Hypothesis Model Extension (`models.py`)
- Added `root_service: str = ""` field to `Hypothesis` model
- Added `cause_code: str = ""` field to `Hypothesis` model
- Both default to empty string for backward compatibility

### 3. Report Guard Enhancement (`reporting.py`)
- `can_generate_report(state)` now requires:
  1. At least one CONFIRMED hypothesis
  2. The confirmed hypothesis must have a non-empty `root_service`
  3. All `supporting_evidence_ids` must be owned by the current incident (present in `state.evidence`)
- `generate_report(state)` now returns:
  - `root_service`: from confirmed hypothesis
  - `root_cause`: cause_code from confirmed hypothesis (falls back to description)
  - `evidence_ids`: supporting evidence IDs from confirmed hypothesis
  - `findings`: evidence summaries (unchanged)
  - `hypotheses`: all hypotheses with final status (unchanged)
  - `rounds_completed`: round count (unchanged)
  - `incident_id`: incident identifier (unchanged)
  - `uncertainty`: 1.0 - confidence of primary confirmed hypothesis

### 4. Engine Integration (`engine.py`)
- Imported `assess_evidence` from `evidence_rules`
- `_generate_hypotheses()`: sets `root_service` on initial hypotheses from alert service and `cause_code` from historical cases
- `_update_hypotheses()`: uses `assess_evidence()` for deterministic pattern matching, creates new hypotheses when assessments don't match existing ones, merges assessments into existing hypotheses by (service, cause_code) key
- Legacy keyword-based evidence association preserved as fallback for hypotheses without root_service/cause_code
- Bug fix: corrected cross-reference logic (`ev.id not in ev.supports_hypothesis_ids` -> `hyp.id not in ev.supports_hypothesis_ids`)

### 5. Routes (`investigations.py`)
- No explicit changes needed; SSE `report_ready` event already forwards `state.report` dict which now includes new fields

## What Was Tested and Test Results

### Test File: `tests/agent/test_evidence_rules.py` (30 tests)
- `TestAssessEvidencePaymentDelay` (2 tests): payment latency logs and slow traces
- `TestAssessEvidencePaymentErrorRate` (2 tests): payment error logs and high error rate metrics
- `TestAssessEvidenceDBPoolExhaustion` (2 tests): pool exhaustion logs and pool metrics
- `TestAssessEvidenceDependencyUnavailable` (2 tests): dependency failure logs and dependency graph
- `TestAssessEvidenceDeploymentRegression` (2 tests): deployment with buggy version
- `TestAssessEvidenceNoMatch` (2 tests): unrelated evidence and empty evidence
- `TestReportGuardRootService` (2 tests): missing root_service rejected, present root_service accepted
- `TestReportGuardEvidenceOwnership` (2 tests): foreign evidence rejected, owned evidence accepted
- `TestReportGuardNoConfirmedHypothesis` (1 test): active hypothesis rejected
- `TestReportFormat` (4 tests): root_service, cause_code, evidence_ids, no root_cause_label
- `TestHypothesisFields` (4 tests): root_service and cause_code fields with defaults
- `TestEvidenceAssessmentModel` (2 tests): model creation and defaults
- `TestReportGuardPartialEvidence` (2 tests): partially owned evidence rejected, all owned accepted
- `TestReportUncertainty` (1 test): uncertainty field present and correct

### All test results: 218 passed (including 30 new + 21 existing engine + 36 tools + 131 others)

## TDD Evidence

### RED Phase
- Ran `uv run pytest tests/agent/test_evidence_rules.py` before implementation
- 21 tests failed (AttributeError for missing `cause_code`/`root_service` on Hypothesis, ImportError for missing `evidence_rules` module, AssertionError for report guard/format)
- 4 tests passed (report guard tests that happened to work with existing `can_generate_report` logic)

### GREEN Phase
- Implemented all code changes
- All 30 evidence rules tests pass
- All 21 existing engine tests still pass
- All 36 tools tests still pass
- Full suite: 218 passed, 0 failed

## Files Changed

1. **Created**: `apps/control-plane/src/incidentlens_control_plane/agent/evidence_rules.py` (239 lines)
2. **Created**: `tests/agent/test_evidence_rules.py` (649 lines)
3. **Modified**: `packages/contracts/src/incidentlens_contracts/models.py` (+2 lines: root_service, cause_code)
4. **Modified**: `apps/control-plane/src/incidentlens_control_plane/agent/reporting.py` (rewritten guard and report format)
5. **Modified**: `apps/control-plane/src/incidentlens_control_plane/agent/engine.py` (+107 lines: assess_evidence integration, hypothesis field updates)
6. **Modified**: `uv.lock` (dependency update from previous work)

## Self-Review Findings

1. **Design constraint satisfied**: The engine consumes persisted Evidence only and does NOT import scenario definitions. The `evidence_rules.py` module uses pattern matching on evidence content, not `root_cause_label`.
2. **root_cause_label never leaks**: Verified that `root_cause_label` does not appear in hypothesis model_dump(), report dict, or any API output. The scenarios routes already explicitly exclude it.
3. **Backward compatibility**: `root_service` and `cause_code` default to empty string, so existing code that creates Hypothesis without these fields continues to work.
4. **Bug fix**: Corrected cross-reference logic in `_update_hypotheses` where `ev.id not in ev.supports_hypothesis_ids` should have been `hyp.id not in ev.supports_hypothesis_ids`.
5. **Legacy fallback**: The keyword-based evidence association is preserved for hypotheses that don't have root_service/cause_code yet, ensuring smooth transition.

## Issues or Concerns

1. The `deployment_regression` mapping creates a `bad_deployment` assessment for ANY deployment to payment-service (not just buggy versions). This is by design per the task brief -- any recent deployment is a candidate for regression. However, this could create false positives in production. The pattern could be refined later to require additional error evidence.
2. ~~The `get_slow_traces` assessor requires a `service` field in the trace item, but the actual tool output from `GetSlowTracesTool` doesn't include `service` in the trace items. This means slow trace evidence won't match the payment_latency_spike pattern in practice. The engine's legacy keyword-based fallback will still handle this case.~~ **FIXED**: The slow trace assessor now matches by duration threshold only, without requiring a `service` field.

---

## Code Review Fix Report (2026-07-28)

### Findings Fixed

#### Critical: Report guard does not enforce non-empty evidence_ids
- **File**: `apps/control-plane/src/incidentlens_control_plane/agent/reporting.py`
- **Fix**: Added `if not primary.supporting_evidence_ids: return False` check before the subset check. The empty-set-vacuous-truth problem is now eliminated.
- **Test**: Added `TestReportGuardNonEmptyEvidenceIds` class with 2 tests: `test_report_rejects_empty_evidence_ids` and `test_report_rejects_empty_evidence_ids_even_with_evidence_present`.

#### Important: `_assess_slow_trace_item` will never match real tool output
- **File**: `apps/control-plane/src/incidentlens_control_plane/agent/evidence_rules.py`
- **Fix**: Removed the `item.get("service", "")` check. The assessor now matches any slow trace with `duration_seconds > 5` as supporting evidence for `payment_latency_spike`. This aligns with `GetSlowTracesTool` output format which returns `{trace_id, duration_seconds, span_count}` without a `service` field.
- **Test**: Updated `test_slow_traces_payment_service_creates_payment_latency` to use realistic tool output format (trace items without `service` field).

#### Important: No assessor ever sets `contradicts=True`
- **File**: `apps/control-plane/src/incidentlens_control_plane/agent/evidence_rules.py`
- **Fix**: Added contradicts assessments in 4 assessor functions:
  - `_assess_log_item`: Normal payment-service logs (INFO/WARN with "normal", "ok", "healthy", etc.) contradict `payment_latency_spike` and `payment_service_degradation`. Healthy order-service logs contradict `database_connection_leak` and `network_partition`.
  - `_assess_metric_item`: Low error rate (<=0.05) contradicts `payment_service_degradation`. Normal latency (<=100) contradicts `payment_latency_spike`. Healthy pool (<=5) contradicts `database_connection_leak`.
  - `_assess_deployment_item`: Same version deployment ("same" in version string) contradicts `bad_deployment`.
- **Tests**: Added `TestContradictsAssessments` class with 5 tests covering all contradicts patterns.

#### Minor: Redundant branches in `_assess_deployment_item`
- **File**: `apps/control-plane/src/incidentlens_control_plane/agent/evidence_rules.py`
- **Fix**: Simplified the `if` (buggy keyword) and `elif` (any version) branches to a single `if version:` check, since both produced the identical `EvidenceAssessment`.

#### Minor: Duplicate test `test_deployment_with_errors_creates_bad_deployment`
- **File**: `tests/agent/test_evidence_rules.py`
- **Fix**: Replaced the duplicate test with a genuinely different scenario: it now tests deployment evidence with a non-buggy version combined with error log evidence, verifying both produce assessments pointing to payment-service.

### Test Results

**Command**: `.venv/bin/python -m pytest tests/agent/test_evidence_rules.py tests/agent/test_investigation_engine.py -v`

**Results**: 58 passed, 0 failed
- `tests/agent/test_evidence_rules.py`: 37 passed (was 30, +7 new tests)
- `tests/agent/test_investigation_engine.py`: 21 passed (unchanged)

### Files Changed

1. `apps/control-plane/src/incidentlens_control_plane/agent/reporting.py` — Added non-empty evidence_ids guard
2. `apps/control-plane/src/incidentlens_control_plane/agent/evidence_rules.py` — Fixed slow trace assessor, added contradicts patterns, simplified deployment branches
3. `tests/agent/test_evidence_rules.py` — Added 7 new tests, updated 2 existing tests

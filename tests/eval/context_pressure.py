"""Real-trace pressure and continuity evaluator.

Verifies that semantic compaction actually crossed the configured pressure
threshold, produced a durable compact boundary, materially reduced the token
footprint, preserved objective/constraints/Todo/Evidence/hash state, continued
without replaying the full prior history, and reached a successful terminal
outcome.  Every failure has a stable name used by the CLI and by the acceptance
manifests.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

try:
    from .types import HarnessTrace
except ImportError:  # pragma: no cover - bare-script fallback
    import sys as _sys

    _sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from tests.eval.types import HarnessTrace

_REDUCTION_RATIO = 0.7
_REPLAY_RATIO = 0.8
_PRESERVED_FLAGS = ("objective", "constraints", "todos", "evidence", "hashes")


@dataclass(frozen=True)
class PressureResult:
    passed: bool
    failures: tuple[str, ...]


@dataclass(frozen=True)
class PressureMetrics:
    before_tokens: int
    after_tokens: int
    threshold_tokens: int | None = None


@dataclass(frozen=True)
class FinalState:
    objective: bool = True
    constraints: bool = True
    todos: bool = True
    evidence: bool = True
    hashes: bool = True


@dataclass(frozen=True)
class _TraceView:
    compact_boundary_count: int
    pre_round_tokens: tuple[int, ...]
    post_round_tokens: tuple[int, ...]
    terminal_success: bool


def evaluate_pressure(trace, metrics, final_state) -> PressureResult:
    view = _trace_view(trace)
    m = _coerce_metrics(metrics)
    preserved = _coerce_final_state(final_state)
    failures: list[str] = []
    if m.threshold_tokens is not None and m.before_tokens <= m.threshold_tokens:
        failures.append("threshold_not_crossed")
    if view.compact_boundary_count == 0:
        failures.append("no_compact_boundary")
    if m.after_tokens * 10 >= m.before_tokens * int(_REDUCTION_RATIO * 10):
        failures.append("no_token_reduction")
    for flag in _PRESERVED_FLAGS:
        if not getattr(preserved, flag):
            failures.append(f"{flag}_lost")
    if _history_replayed(view):
        failures.append("history_replayed")
    if not view.terminal_success:
        failures.append("task_incomplete")
    return PressureResult(not failures, tuple(failures))


# ---------------------------------------------------------------------------
# trace normalization
# ---------------------------------------------------------------------------


def _trace_view(trace) -> _TraceView:
    if isinstance(trace, HarnessTrace):
        return _view_from_harness(trace)
    if isinstance(trace, (str, Path)):
        return _view_from_path(Path(trace))
    if hasattr(trace, "to_record"):
        return _view_from_harness(HarnessTrace.from_live_result(trace))
    events = list(trace)
    return _view_from_events(events)


def _view_from_path(path: Path) -> _TraceView:
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        raise ValueError(f"trace is empty: {path}")
    if text.lstrip().startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict) and ("run" in data or "investigation" in data):
            return _view_from_harness(HarnessTrace.from_live_result(_DictRecord(data)))
    events = [json.loads(line) for line in text.splitlines() if line.strip()]
    return _view_from_events(events)


class _DictRecord:
    """Adapter so ``HarnessTrace.from_live_result`` accepts a raw record dict."""

    def __init__(self, record: dict[str, object]) -> None:
        self._record = record

    def to_record(self) -> dict[str, object]:
        return self._record


def _view_from_harness(trace: HarnessTrace) -> _TraceView:
    boundaries = sorted(trace.compact_boundaries, key=lambda boundary: boundary.created_at)
    last_created = boundaries[-1].created_at if boundaries else None
    pre: list[int] = []
    post: list[int] = []
    for round_ in trace.rounds:
        tokens = round_.provider_usage.input_tokens
        if last_created is not None and round_.created_at <= last_created:
            pre.append(tokens)
        else:
            post.append(tokens)
    return _TraceView(
        compact_boundary_count=len(boundaries),
        pre_round_tokens=tuple(pre),
        post_round_tokens=tuple(post),
        terminal_success=_terminal_state_from_harness(trace),
    )


def _terminal_state_from_harness(trace: HarnessTrace) -> bool:
    return trace.run.status.value == "completed" or (
        trace.investigation.status.value == "completed"
    )


def _view_from_events(events) -> _TraceView:
    compact_indexes = [
        line_index
        for line_index, event in enumerate(events)
        if isinstance(event, dict) and event.get("event_type") == "context.compacted"
    ]
    last_compact = compact_indexes[-1] if compact_indexes else None
    pre: list[int] = []
    post: list[int] = []
    for line_index, event in enumerate(events):
        if not isinstance(event, dict) or event.get("event_type") != "model_round.completed":
            continue
        payload = event.get("payload") or {}
        tokens = _as_int(payload.get("input_tokens"))
        if last_compact is None or line_index <= last_compact:
            pre.append(tokens)
        else:
            post.append(tokens)
    return _TraceView(
        compact_boundary_count=len(compact_indexes),
        pre_round_tokens=tuple(pre),
        post_round_tokens=tuple(post),
        terminal_success=_terminal_state_from_events(events),
    )


def _terminal_state_from_events(events) -> bool:
    for event in events:
        if not isinstance(event, dict):
            continue
        event_type = event.get("event_type")
        payload = event.get("payload") or {}
        status = payload.get("status", "completed")
        if event_type == "investigation.completed" and str(status) == "completed":
            return True
        if event_type == "agent_run.completed" and str(status) == "completed":
            return True
    return False


def _history_replayed(view: _TraceView) -> bool:
    pre_max = max(view.pre_round_tokens) if view.pre_round_tokens else 0
    post_max = max(view.post_round_tokens) if view.post_round_tokens else 0
    return (
        view.compact_boundary_count > 0
        and pre_max > 0
        and post_max >= int(pre_max * _REPLAY_RATIO)
    )


# ---------------------------------------------------------------------------
# metrics / final-state coercion
# ---------------------------------------------------------------------------


def _coerce_metrics(value) -> PressureMetrics:
    if isinstance(value, PressureMetrics):
        return value
    if isinstance(value, dict):
        return PressureMetrics(
            before_tokens=_as_int(value.get("before_tokens", 0)),
            after_tokens=_as_int(value.get("after_tokens", 0)),
            threshold_tokens=_as_int_or_none(value.get("threshold_tokens")),
        )
    return PressureMetrics(
        before_tokens=_as_int(getattr(value, "before_tokens", 0)),
        after_tokens=_as_int(getattr(value, "after_tokens", 0)),
        threshold_tokens=_as_int_or_none(getattr(value, "threshold_tokens", None)),
    )


def _coerce_final_state(value) -> FinalState:
    if isinstance(value, FinalState):
        return value
    if isinstance(value, dict):
        return FinalState(
            **{flag: bool(value.get(flag, True)) for flag in _PRESERVED_FLAGS}
        )
    return FinalState(
        **{flag: bool(getattr(value, flag, True)) for flag in _PRESERVED_FLAGS}
    )


def _as_int(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _as_int_or_none(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate a real context-pressure trace.")
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument("--metrics", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    if not args.manifest.is_file():
        print(f"missing manifest artifact: {args.manifest}", file=sys.stderr)
        return 2
    try:
        metrics = PressureMetrics(**json.loads(args.metrics.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, TypeError) as error:
        print(f"invalid metrics artifact: {error}", file=sys.stderr)
        return 2
    try:
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        manifest = {}
    final_state = FinalState(
        **{flag: bool(manifest.get(flag, True)) for flag in _PRESERVED_FLAGS}
    )
    try:
        result = evaluate_pressure(args.trace, metrics, final_state)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"could not evaluate pressure trace: {error}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "passed": result.passed,
                "failures": list(result.failures),
                "metrics": {
                    "before_tokens": metrics.before_tokens,
                    "after_tokens": metrics.after_tokens,
                    "threshold_tokens": metrics.threshold_tokens,
                },
            },
            indent=2,
        )
    )
    return 0 if result.passed else 1


if __name__ == "__main__":
    sys.exit(_main())


__all__ = ["FinalState", "PressureMetrics", "PressureResult", "evaluate_pressure"]

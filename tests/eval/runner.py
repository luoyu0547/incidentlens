"""CLI and programmatic deterministic harness runner."""
from __future__ import annotations
import argparse, asyncio, json
from pathlib import Path
try:
    from .metrics import evaluate_trace
    from .scenarios import SCENARIOS
    from .types import HarnessEvalResult, HarnessTrace
except ImportError:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from tests.eval.metrics import evaluate_trace
    from tests.eval.scenarios import SCENARIOS
    from tests.eval.types import HarnessEvalResult, HarnessTrace
EXPECTED_SCENARIOS = {name for name, _ in SCENARIOS}
async def run_scenario(name: str) -> HarnessTrace:
    for candidate, execute in SCENARIOS:
        if candidate == name:
            return await execute()
    raise KeyError(name)
async def run_all() -> tuple[HarnessEvalResult, ...]:
    results = []
    for name in sorted(EXPECTED_SCENARIOS):
        results.append(evaluate_trace(await run_scenario(name)))
    return tuple(results)
def render_json(results: tuple[HarnessEvalResult, ...]) -> str:
    return json.dumps([result.model_dump(mode="json") for result in results], ensure_ascii=False, indent=2) + "\n"
def render_table(results: tuple[HarnessEvalResult, ...]) -> str:
    lines = ["scenario                         grounded bypass pairing compact child rounds tools tokens elapsed"]
    for r in results:
        lines.append(f"{r.scenario:32} {str(r.grounded_completion):8} {r.scope_policy_bypass_count:6} {r.tool_pairing_rate:7.2f} {str(r.compaction_recovered):7} {r.child_exactly_once_rate:5.2f} {r.rounds:6} {r.tool_calls:5} {r.input_tokens+r.output_tokens:6} {r.elapsed_seconds:7.3f}")
    return "\n".join(lines)
async def _main(args: argparse.Namespace) -> None:
    results = await run_all()
    print(render_table(results))
    if args.json:
        Path(args.json).write_text(render_json(results), encoding="utf-8")
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--json")
    asyncio.run(_main(parser.parse_args()))

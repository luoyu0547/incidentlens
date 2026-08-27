import type { HypothesisSummaryView } from '@incidentlens/protocol';
export function HypothesisList({ hypotheses }: { hypotheses?: HypothesisSummaryView[] }) { return <section aria-label="假设"><h3>假设</h3>{hypotheses?.length ? <ul>{hypotheses.map((h) => <li key={h.hypothesis_id}><strong>{h.status}</strong>：<span>{h.summary}</span></li>)}</ul> : <p>暂无假设</p>}</section>; }

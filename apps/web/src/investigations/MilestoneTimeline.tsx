import type { InvestigationMilestoneView } from '@incidentlens/protocol';
import { Timestamp } from '../shared/Timestamp';
export function MilestoneTimeline({ milestones }: { milestones?: InvestigationMilestoneView[] }) { const ordered = [...(milestones ?? [])].sort((a, b) => a.occurred_at.localeCompare(b.occurred_at)); return <section aria-label="调查里程碑"><h3>里程碑</h3>{ordered.length ? <ol>{ordered.map((m) => <li key={m.event_id}><Timestamp value={m.occurred_at} /> {m.summary ?? m.event_type}{m.status ? `（${m.status}）` : ''}</li>)}</ol> : <p>暂无里程碑</p>}</section>; }

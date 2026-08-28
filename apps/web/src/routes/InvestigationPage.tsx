import { useParams } from '@tanstack/react-router';
import { useQuery } from '@tanstack/react-query';
import { investigationQuery } from '../api/queries';
import { InvestigationSummary } from '../investigations/InvestigationSummary';
import { MilestoneTimeline } from '../investigations/MilestoneTimeline';
import { HypothesisList } from '../investigations/HypothesisList';
import { InvestigationEvidence } from '../investigations/InvestigationEvidence';
import { Timestamp } from '../shared/Timestamp';

export function InvestigationPage() {
  const { investigationId } = useParams({ from: '/investigations/$investigationId' });
  const { data, isPending } = useQuery(investigationQuery(investigationId ?? ''));
  if (isPending) return <section aria-label="调查详情"><h2>调查 {investigationId}</h2><p>正在加载…</p></section>;
  if (!data) return <section aria-label="调查详情"><h2>调查 {investigationId}</h2><p>未找到调查。</p></section>;
  return <section aria-label="调查详情"><InvestigationSummary investigation={data} /><p>创建时间：<Timestamp value={data.created_at} /></p><p>更新时间：<Timestamp value={data.updated_at} /></p><MilestoneTimeline milestones={data.milestones} /><HypothesisList hypotheses={data.hypotheses} /><InvestigationEvidence evidence={data.evidence} /><section aria-label="结论"><h3>结论</h3><p>{data.conclusion?.summary ?? '暂无结论'}</p></section><section aria-label="处理摘要"><h3>处理摘要</h3>{data.change_summaries?.length ? <ul>{data.change_summaries.map((change) => <li key={change.changeset_id}>{change.status}：{change.scopes.join('、')}</li>)}</ul> : <p>暂无处理摘要</p>}</section></section>;
}

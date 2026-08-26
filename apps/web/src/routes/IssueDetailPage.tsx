import { useParams } from '@tanstack/react-router';
import { useQuery } from '@tanstack/react-query';
import { issueQuery } from '../api/queries';
import { IssueSummary } from '../issues/IssueSummary';
import { EvidenceList } from '../issues/EvidenceList';
import { ResolutionPanel } from '../issues/ResolutionPanel';
import { VerificationPanel } from '../issues/VerificationPanel';

export function IssueDetailPage() {
  const { issueId } = useParams({ from: '/issues/$issueId' });
  const { data, isPending } = useQuery(issueQuery(issueId ?? ''));
  if (isPending) return <section aria-label="问题详情"><h2>问题详情</h2><p>正在加载…</p></section>;
  if (!data) return <section aria-label="问题详情"><h2>问题详情</h2><p>未找到问题。</p></section>;
  return <section aria-label="问题详情"><h2>问题详情</h2><IssueSummary issue={data} /><p>根因：{data.root_cause ?? '未知'}</p><p>根因置信度：{data.root_cause_confidence === null || data.root_cause_confidence === undefined ? '未提供' : data.root_cause_confidence}</p>{data.status === 'waiting_approval' ? <p>需要在 CLI 中处理待审批事项。</p> : null}<EvidenceList evidence={data.evidence} /><ResolutionPanel resolution={data.resolution} /><VerificationPanel verification={data.verification} /></section>;
}

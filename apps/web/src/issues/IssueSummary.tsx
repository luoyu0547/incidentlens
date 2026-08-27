import type { IssueView } from '@incidentlens/protocol';
import { Timestamp } from '../shared/Timestamp';

export function IssueSummary({ issue }: { issue: IssueView }) {
  return <article><h3>{issue.symptom}</h3><p>状态：{issue.status}</p><p>服务：{issue.service_id}；目标：{issue.target_id}</p><p>严重程度：{issue.severity ?? '未知'}</p><p>根因：{issue.root_cause ?? '未知'}</p><p>根因置信度：{issue.root_cause_confidence === null || issue.root_cause_confidence === undefined ? '未提供' : issue.root_cause_confidence}</p><p>创建时间：<Timestamp value={issue.created_at} /></p><p>更新时间：<Timestamp value={issue.updated_at} /></p></article>;
}

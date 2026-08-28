import { Link, useSearch } from '@tanstack/react-router';
import { useQuery } from '@tanstack/react-query';
import type { IssueListQuery } from '@incidentlens/protocol';
import { issuesQuery } from '../api/queries';
import { IssueSummary } from '../issues/IssueSummary';

function readFilter(value: unknown): string | undefined {
  return typeof value === 'string' && value.length > 0 ? value : undefined;
}

/** Read-only issue list; URL filters are forwarded unchanged to the server. */
export function IssuesPage() {
  const search = useSearch({ strict: false });
  const params = search as Record<string, unknown>;
  const query: IssueListQuery = {
    status: readFilter(params.status) as IssueListQuery['status'],
    target_id: readFilter(params.target_id),
    service_id: readFilter(params.service_id),
    after: readFilter(params.after),
  };
  const { data, isPending } = useQuery(issuesQuery(query));

  return <section aria-label="问题列表"><h2>问题</h2>{isPending ? <p>正在加载问题…</p> : data?.items.length ? <ul>{data.items.map((issue) => <li key={issue.issue_id}><Link to="/issues/$issueId" params={{ issueId: issue.issue_id }}><IssueSummary issue={issue} /></Link></li>)}</ul> : <p>无活动问题</p>}</section>;
}

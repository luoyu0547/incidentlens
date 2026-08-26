import { useParams } from '@tanstack/react-router';

/**
 * IssueDetailPage placeholder. The full issue detail view arrives in a later
 * task. The route uses the `issueId` path parameter so it is matched by the
 * router and reachable from navigation.
 */
export function IssueDetailPage() {
  const { issueId } = useParams({ strict: false });
  return <h2>问题详情 {issueId}</h2>;
}

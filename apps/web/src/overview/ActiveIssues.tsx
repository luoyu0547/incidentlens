/**
 * Active (unresolved) issue summary.
 *
 * The overview projection exposes open-issue counts but not per-issue severity or
 * status, so this section surfaces the counts and which services currently have
 * open issues, each linking to the service detail read. The global count links to
 * the full issues page. A workspace with no open issues renders an explanatory
 * message instead.
 */
import { Link } from '@tanstack/react-router';
import type { OverviewServiceView, OverviewTargetView } from '@incidentlens/protocol';
import { StatusBadge } from '../shared/StatusBadge';
import { normalizeLogRouteSearch } from '../logs/log-search';

const DEFAULT_LOG_SEARCH = normalizeLogRouteSearch({});

export interface OpenIssueServiceRow {
  readonly target: OverviewTargetView;
  readonly service: OverviewServiceView;
}

export function ActiveIssues({
  openIssueCount,
  services,
}: {
  openIssueCount: number;
  services: readonly OpenIssueServiceRow[];
}) {
  if (openIssueCount === 0 && services.length === 0) {
    return <p>无活动问题</p>;
  }

  return (
    <div className="active-issues">
      <Link to="/issues">开放问题：{openIssueCount}</Link>
      <ul>
        {services.map(({ target, service }) => (
          <li key={`${target.target_id}:${service.service_id}`}>
            <Link to="/services/$serviceId" params={{ serviceId: service.service_id }} search={DEFAULT_LOG_SEARCH}>
              {service.service_id}
            </Link>
            <StatusBadge status={service.status} />
            <span>开放问题：{service.open_issue_count}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

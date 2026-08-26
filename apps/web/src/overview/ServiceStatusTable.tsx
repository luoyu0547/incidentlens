/**
 * Service status table.
 *
 * Flat table of every discovered service across all targets. Each row carries an
 * explicit status (icon + text), the owning target host, and the container /
 * open-issue / pending-approval counts. Service names link to the service detail
 * read. A row without services (the "no discovered services" state) renders an
 * explanatory message instead.
 */
import { Link } from '@tanstack/react-router';
import type { OverviewServiceView, OverviewTargetView } from '@incidentlens/protocol';
import { StatusBadge } from '../shared/StatusBadge';
import { normalizeLogRouteSearch } from '../logs/log-search';

const DEFAULT_LOG_SEARCH = normalizeLogRouteSearch({});
import { Timestamp } from '../shared/Timestamp';

export interface ServiceRow {
  readonly target: OverviewTargetView;
  readonly service: OverviewServiceView;
}

export function ServiceStatusTable({ rows }: { rows: readonly ServiceRow[] }) {
  if (rows.length === 0) {
    return <p>未发现服务</p>;
  }

  return (
    <table className="service-table">
      <thead>
        <tr>
          <th>服务</th>
          <th>状态</th>
          <th>目标</th>
          <th>容器</th>
          <th>开放问题</th>
          <th>待审批</th>
          <th>最近观测</th>
        </tr>
      </thead>
      <tbody>
        {rows.map(({ target, service }) => (
          <tr key={`${target.target_id}:${service.service_id}`}>
            <td>
              <Link to="/services/$serviceId" params={{ serviceId: service.service_id }} search={DEFAULT_LOG_SEARCH}>
                {service.service_id}
              </Link>
            </td>
            <td>
              <StatusBadge status={service.status} />
            </td>
            <td>{target.host}</td>
            <td>{service.container_count}</td>
            <td>{service.open_issue_count}</td>
            <td>{service.pending_approval_count}</td>
            <td>{service.last_observed_at ? <Timestamp value={service.last_observed_at} /> : '—'}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

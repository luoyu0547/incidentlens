/**
 * Target status list.
 *
 * Renders each discovered target as a list row (not a KPI card wall): its name,
 * aggregated health status, host, service count, and — when available — the most
 * recent test and observation times. Only server redacted/safe fields are shown:
 * no authentication refs, credential hints, host-key material, or raw paths.
 */
import type { OverviewTargetView } from '@incidentlens/protocol';
import { StatusBadge } from '../shared/StatusBadge';
import { Timestamp } from '../shared/Timestamp';

export function TargetStatusList({ targets }: { targets: readonly OverviewTargetView[] }) {
  if (targets.length === 0) {
    return <p>未发现目标</p>;
  }

  return (
    <ul className="target-list">
      {targets.map((target) => (
        <li key={target.target_id}>
          <span className="target-name">{target.name}</span>
          <StatusBadge status={target.status} />
          <span className="target-host">主机 {target.host}</span>
          <span className="target-service-count">服务 {target.service_count}</span>
          {target.last_tested_at ? (
            <span className="target-tested">
              最近检测 <Timestamp value={target.last_tested_at} />
            </span>
          ) : null}
          {target.last_observed_at ? (
            <span className="target-observed">
              最近观测 <Timestamp value={target.last_observed_at} />
            </span>
          ) : null}
        </li>
      ))}
    </ul>
  );
}

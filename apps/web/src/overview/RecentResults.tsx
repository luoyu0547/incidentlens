/**
 * Recent resolution/verification summary.
 *
 * Lists the most recent resolved incidents: symptom, owning service/target,
 * server-redacted resolution summary, verification summary (only when present),
 * and links to the issue and investigation reads. A workspace with no resolutions
 * renders an explanatory message instead.
 */
import { Link } from '@tanstack/react-router';
import type { ResolutionSummary } from '@incidentlens/protocol';
import { Timestamp } from '../shared/Timestamp';

export function RecentResults({ resolutions }: { resolutions: readonly ResolutionSummary[] }) {
  if (resolutions.length === 0) {
    return <p>暂无处理结果</p>;
  }

  return (
    <ul className="recent-results">
      {resolutions.map((resolution) => (
        <li key={resolution.investigation_id}>
          <strong>{resolution.symptom}</strong>
          <div className="meta">
            服务 {resolution.service_id} · 目标 {resolution.target_id} · 处理时间{' '}
            <Timestamp value={resolution.resolved_at} />
          </div>
          <p className="resolution">{resolution.resolution_summary}</p>
          {resolution.verification_summary ? (
            <p className="verification">验证：{resolution.verification_summary}</p>
          ) : null}
          <div className="links">
            <Link to="/issues/$issueId" params={{ issueId: resolution.issue_id }}>
              查看问题
            </Link>
            <Link
              to="/investigations/$investigationId"
              params={{ investigationId: resolution.investigation_id }}
            >
              查看调查
            </Link>
          </div>
        </li>
      ))}
    </ul>
  );
}

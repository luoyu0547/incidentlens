/**
 * Shared health-status badge.
 *
 * Every health state is conveyed by both an icon and a Chinese label so the
 * meaning never depends on color alone. The badge is used across the overview
 * and service detail surfaces.
 */
import type { HealthStatus } from '@incidentlens/protocol';
import { AlertTriangle, CheckCircle2, HelpCircle, XCircle } from 'lucide-react';
import type { LucideIcon } from 'lucide-react';

interface StatusMeta {
  readonly label: string;
  readonly color: string;
  readonly Icon: LucideIcon;
}

/** Static metadata — deliberately not exported so the file stays component-only. */
const STATUS_META: Record<HealthStatus, StatusMeta> = {
  healthy: { label: '健康', color: '#16a34a', Icon: CheckCircle2 },
  degraded: { label: '降级', color: '#d97706', Icon: AlertTriangle },
  unreachable: { label: '不可达', color: '#dc2626', Icon: XCircle },
  unknown: { label: '未知', color: '#6b7280', Icon: HelpCircle },
};

export function StatusBadge({ status }: { status: HealthStatus }) {
  const meta = STATUS_META[status] ?? STATUS_META.unknown;
  return (
    <span className="status-badge">
      <meta.Icon aria-hidden="true" style={{ color: meta.color }} />
      <span>{meta.label}</span>
    </span>
  );
}

import type { ServiceDetailView } from '@incidentlens/protocol';
import { Timestamp } from '../shared/Timestamp';

const STATUS_LABELS = { healthy: '健康', degraded: '降级', unreachable: '不可达', unknown: '未知' } as const;

export function ServiceHeader({ service }: { readonly service: ServiceDetailView }) {
  return (
    <header className="service-header">
      <h2>服务详情</h2>
      <h3>服务 {service.service_id}</h3>
      <p>{STATUS_LABELS[service.status]}</p>
      <p>生成时间：<Timestamp value={service.generated_at} /></p>
    </header>
  );
}

import type { ServiceDetailView } from '@incidentlens/protocol';
import { Timestamp } from '../shared/Timestamp';

const STATUS_LABELS = { healthy: '健康', degraded: '降级', unreachable: '不可达', unknown: '未知' } as const;

function Observation({ value }: { readonly value?: string | null }) {
  return value ? <Timestamp value={value} /> : '—';
}

export function ServiceFacts({ service }: { readonly service: ServiceDetailView }) {
  return (
    <section className="service-facts" aria-label="服务状态">
      <h3>服务状态</h3>
      <dl>
        <dt>服务</dt>
        <dd>{service.service_id}</dd>
        <dt>状态</dt>
        <dd>{STATUS_LABELS[service.status]}</dd>
        <dt>最近观测</dt>
        <dd><Observation value={service.last_observed_at} /></dd>
      </dl>
      {service.instances.map((instance) => (
        <article className="service-facts__instance" key={instance.target_id}>
          <h4>{instance.target_name}</h4>
          <dl>
            <dt>目标</dt>
            <dd>{instance.target_id}</dd>
            <dt>主机</dt>
            <dd>{instance.host}</dd>
            <dt>状态</dt>
            <dd>{STATUS_LABELS[instance.status]}</dd>
            <dt>容器</dt>
            <dd>{instance.container_names.length ? instance.container_names.join('、') : '—'}</dd>
            <dt>最近测试</dt>
            <dd><Observation value={instance.last_tested_at} /></dd>
            <dt>最近观测</dt>
            <dd><Observation value={instance.last_observed_at} /></dd>
          </dl>
        </article>
      ))}
    </section>
  );
}

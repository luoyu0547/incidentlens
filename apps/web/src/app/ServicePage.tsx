import { useQuery } from '@tanstack/react-query';
import { useParams } from '@tanstack/react-router';
import { serviceQuery } from '../api/queries';
import { LogViewerPlaceholder } from '../services/LogViewerPlaceholder';
import { RelatedInvestigations } from '../services/RelatedInvestigations';
import { ServiceFacts } from '../services/ServiceFacts';
import { ServiceHeader } from '../services/ServiceHeader';
import { ServiceIssues } from '../services/ServiceIssues';

export function ServicePage() {
  const { serviceId } = useParams({ strict: false });
  const { data: service, error } = useQuery(serviceQuery(serviceId ?? ''));

  if (error) {
    return <p role="alert">加载服务时出现问题。</p>;
  }

  if (!service) {
    return <p>正在加载服务…</p>;
  }

  return (
    <div>
      <ServiceHeader service={service} />
      <ServiceFacts service={service} />
      <ServiceIssues issueIds={service.issue_ids} />
      <RelatedInvestigations investigationIds={service.investigation_ids} />
      {service.pending_approval_count > 0 && <p>等待 CLI 中的操作者决策</p>}
      <LogViewerPlaceholder
        serviceId={service.service_id}
        targetId={service.target_ids[0] ?? ''}
        initialSearch={{}}
      />
    </div>
  );
}

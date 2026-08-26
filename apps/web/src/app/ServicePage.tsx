import { useQuery } from '@tanstack/react-query';
import { useNavigate, useParams, useSearch } from '@tanstack/react-router';
import { serviceQuery } from '../api/queries';
import { LogViewer } from '../logs/LogViewer';
import { normalizeLogRouteSearch } from '../logs/log-search';
import { RelatedInvestigations } from '../services/RelatedInvestigations';
import { ServiceFacts } from '../services/ServiceFacts';
import { ServiceHeader } from '../services/ServiceHeader';
import { ServiceIssues } from '../services/ServiceIssues';

export function ServicePage() {
  const { serviceId } = useParams({ strict: false });
  const routeSearch = useSearch({ strict: false });
  const navigate = useNavigate();
  const { data: service, error } = useQuery(serviceQuery(serviceId ?? ''));

  if (error) return <p role="alert">加载服务时出现问题。</p>;
  if (!service) return <p>正在加载服务…</p>;

  return (
    <div>
      <ServiceHeader service={service} />
      <ServiceFacts service={service} />
      <ServiceIssues issueIds={service.issue_ids} />
      <RelatedInvestigations investigationIds={service.investigation_ids} />
      {service.pending_approval_count > 0 && <p>等待 CLI 中的操作者决策</p>}
      <LogViewer
        serviceId={service.service_id}
        targetId={service.target_ids[0] ?? ''}
        initialSearch={normalizeLogRouteSearch(routeSearch)}
        onSearchChange={(search) => void navigate({ search: () => search as never })}
      />
    </div>
  );
}

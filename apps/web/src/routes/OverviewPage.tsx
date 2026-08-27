/**
 * Cloud overview page.
 *
 * Read-only dashboard over the `overview` projection. URL state is authoritative
 * and the data flows from the guarded {@link readonlyClient} via `getOverview()`.
 * The full title heading is always rendered (during loading and error states too)
 * so the route remains identifiable without depending on a successful fetch.
 *
 * Layout favors a table/list hierarchy instead of a KPI-card wall; every health
 * status is conveyed by icon + Chinese text, never color alone.
 */
import { useQuery } from '@tanstack/react-query';
import { ReadonlyApiError, type OverviewView } from '@incidentlens/protocol';
import { overviewQuery } from '../api/queries';
import { Timestamp } from '../shared/Timestamp';
import { TargetStatusList } from '../overview/TargetStatusList';
import { ServiceStatusTable, type ServiceRow } from '../overview/ServiceStatusTable';
import { ActiveIssues, type OpenIssueServiceRow } from '../overview/ActiveIssues';
import { RecentResults } from '../overview/RecentResults';

export function OverviewPage() {
  const { data, isPending, isError, error } = useQuery(overviewQuery);
  const requiresAuthentication = error instanceof ReadonlyApiError && (error.status === 401 || error.status === 403);

  return (
    <div className="overview-page">
      <h2>总览</h2>
      {isPending ? <p>加载中...</p> : null}
      {requiresAuthentication ? (
        <section className="workspace-auth" aria-label="工作区认证">
          <h3>需要登录工作区</h3>
          <p>当前浏览器没有有效的工作区会话。请先完成登录，再刷新此页面。</p>
        </section>
      ) : isError ? (
        <p role="alert">加载页面时出现问题，请稍后重试。</p>
      ) : null}
      {data ? <OverviewBody data={data} /> : null}
    </div>
  );
}

function OverviewBody({ data }: { data: OverviewView }) {
  const serviceRows: ServiceRow[] = data.targets.flatMap((target) =>
    (target.services ?? []).map((service) => ({ target, service })),
  );
  const openIssueServices: OpenIssueServiceRow[] = serviceRows.filter(
    ({ service }) => service.open_issue_count > 0,
  );
  const counts = data.service_counts;

  return (
    <div className="overview">
      <dl className="overview-counts">
        <div>
          <dt>开放问题</dt>
          <dd>{data.open_issue_count}</dd>
        </div>
        <div>
          <dt>活动调查</dt>
          <dd>{data.active_investigation_count}</dd>
        </div>
        <div>
          <dt>待处理审批</dt>
          <dd>{data.pending_approval_count}</dd>
        </div>
        <div>
          <dt>生成时间</dt>
          <dd>
            <Timestamp value={data.generated_at} />
          </dd>
        </div>
      </dl>

      <ul className="service-counts">
        <li>健康 {counts.healthy ?? 0}</li>
        <li>降级 {counts.degraded ?? 0}</li>
        <li>不可达 {counts.unreachable ?? 0}</li>
        <li>未知 {counts.unknown ?? 0}</li>
      </ul>

      <section aria-label="目标状态">
        <h3>目标</h3>
        <TargetStatusList targets={data.targets} />
      </section>

      <section aria-label="服务状态">
        <h3>服务</h3>
        <ServiceStatusTable rows={serviceRows} />
      </section>

      <section aria-label="活动问题">
        <h3>活动问题</h3>
        <ActiveIssues openIssueCount={data.open_issue_count} services={openIssueServices} />
      </section>

      <section aria-label="最近处理结果">
        <h3>最近处理结果</h3>
        <RecentResults resolutions={data.recent_resolutions} />
      </section>
    </div>
  );
}

/**
 * TanStack Query option factories for every read-only API surface.
 *
 * Every query function calls the guarded {@link readonlyClient} singleton so
 * all HTTP traffic flows through the read-only boundary.
 */
import { queryOptions } from '@tanstack/react-query';
import { readonlyClient } from './client';
import { queryKeys } from './query-keys';
import type {
  ServiceLogQuery,
  IssueListQuery,
  InvestigationListQuery,
} from '@incidentlens/protocol';

/** Workspace overview — the top-level dashboard snapshot. */
export const overviewQuery = queryOptions({
  queryKey: queryKeys.overview,
  queryFn: () => readonlyClient.getOverview(),
});

/** All registered targets. */
export const targetsQuery = queryOptions({
  queryKey: queryKeys.targets,
  queryFn: ({ signal }) => readonlyClient.listTargets(signal),
});

/** Services under a specific target. */
export const targetServicesQuery = (targetId: string) =>
  queryOptions({
    queryKey: queryKeys.targetServices(targetId),
    queryFn: ({ signal }) => readonlyClient.listTargetServices(targetId, signal),
  });

/** Single service detail. */
export const serviceQuery = (serviceId: string) =>
  queryOptions({
    queryKey: queryKeys.service(serviceId),
    queryFn: ({ signal }) => readonlyClient.getService(serviceId, signal),
  });

/** Paginated log records for a service. */
export const serviceLogsQuery = (serviceId: string, query: ServiceLogQuery) =>
  queryOptions({
    queryKey: queryKeys.serviceLogs(serviceId, query),
    queryFn: ({ signal }) => readonlyClient.getServiceLogs(serviceId, query, signal),
  });

/** Paginated issue list. */
export const issuesQuery = (query: IssueListQuery) =>
  queryOptions({
    queryKey: queryKeys.issues(query),
    queryFn: ({ signal }) => readonlyClient.listIssues(query, signal),
  });

/** Single issue detail. */
export const issueQuery = (issueId: string) =>
  queryOptions({
    queryKey: queryKeys.issue(issueId),
    queryFn: ({ signal }) => readonlyClient.getIssue(issueId, signal),
  });

/** Paginated investigation list. */
export const investigationsQuery = (query: InvestigationListQuery) =>
  queryOptions({
    queryKey: queryKeys.investigations(query),
    queryFn: ({ signal }) => readonlyClient.listInvestigations(query, signal),
  });

/** Single investigation summary. */
export const investigationQuery = (id: string) =>
  queryOptions({
    queryKey: queryKeys.investigation(id),
    queryFn: ({ signal }) => readonlyClient.getInvestigationSummary(id, signal),
  });

/** Single evidence detail. */
export const evidenceQuery = (id: string) =>
  queryOptions({
    queryKey: queryKeys.evidence(id),
    queryFn: ({ signal }) => readonlyClient.getEvidence(id, signal),
  });

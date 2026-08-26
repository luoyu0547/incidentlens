/**
 * Centralized, stable query keys for TanStack Query.
 *
 * Log keys use canonical filter serialization without cursor parsing — the
 * cursor is passed as a raw query parameter and never inspected or transformed.
 */
import type {
  ServiceLogQuery,
  IssueListQuery,
  InvestigationListQuery,
} from '@incidentlens/protocol';

/**
 * Canonical filter serialization for log/issue/investigation list queries.
 *
 * Sorts keys alphabetically and returns a flat array of `[key, value]` pairs
 * so the resulting query key is deterministic regardless of property creation
 * order. The cursor is included verbatim — no parsing or transformation.
 */
function canonicalFilter(query: object): unknown[] {
  const keys = Object.keys(query).sort();
  return keys.flatMap((k) => [k, (query as Record<string, unknown>)[k]]);
}

export const queryKeys = {
  /** Workspace overview snapshot. */
  overview: ['overview'] as const,

  /** Registered target list. */
  targets: ['targets'] as const,

  /** Services belonging to a specific target. */
  targetServices: (targetId: string) => ['targets', targetId, 'services'] as const,

  /** Single service detail. */
  service: (serviceId: string) => ['services', serviceId] as const,

  /** Paginated log records for a service. Cursor is not parsed. */
  serviceLogs: (serviceId: string, query: ServiceLogQuery) =>
    ['services', serviceId, 'logs', ...canonicalFilter(query)] as const,

  /** Paginated issue list. */
  issues: (query: IssueListQuery) => ['issues', ...canonicalFilter(query)] as const,

  /** Single issue detail. */
  issue: (issueId: string) => ['issues', issueId] as const,

  /** Paginated investigation list. */
  investigations: (query: InvestigationListQuery) =>
    ['investigations', ...canonicalFilter(query)] as const,

  /** Single investigation summary. */
  investigation: (id: string) => ['investigations', id] as const,

  /** Single evidence detail. */
  evidence: (id: string) => ['evidence', id] as const,
};

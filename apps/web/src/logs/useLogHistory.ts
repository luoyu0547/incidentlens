import { useInfiniteQuery } from '@tanstack/react-query';
import type { LogPage } from '@incidentlens/protocol';
import { readonlyClient } from '../api/client';
import { logSearchToQuery, type LogRouteSearch } from './log-search';

export function useLogHistory(serviceId: string, search: LogRouteSearch) {
  return useInfiniteQuery<LogPage>({
    queryKey: ['service-logs', serviceId, search],
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam, signal }) => readonlyClient.getServiceLogs(serviceId, logSearchToQuery(search, pageParam as string | undefined), signal),
    getNextPageParam: (page) => page.has_more && page.next_cursor ? page.next_cursor : undefined,
    staleTime: 10_000,
    enabled: search.mode === 'history' && !Boolean(search.from && search.to && Date.parse(search.from) > Date.parse(search.to)),
  });
}

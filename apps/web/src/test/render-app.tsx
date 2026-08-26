/**
 * Test render helper with TanStack Router + QueryClient + MSW.
 *
 * Every test that exercises routing or data fetching should use this helper so
 * the environment is consistent (memory history, fresh QueryClient, MSW
 * isolated).
 */
import { createMemoryHistory, RouterProvider } from '@tanstack/react-router';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render } from '@testing-library/react';
import { createTestRouter } from '../router';

export interface RenderAppOptions {
  /** Initial URL entries for the memory history. Defaults to `['/']`. */
  initialEntries?: string[];
  /** Initial index in the entries array. */
  initialIndex?: number;
}

/**
 * Render the full application (router + query client) inside a memory history
 * for testing. Returns the rendered result plus the QueryClient for
 * invalidating queries mid-test.
 */
export function renderApp(options: RenderAppOptions = {}) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
        staleTime: 0,
        gcTime: 0,
      },
    },
  });
  const history = createMemoryHistory({
    initialEntries: options.initialEntries ?? ['/'],
    initialIndex: options.initialIndex,
  });
  const router = createTestRouter(history);

  const result = render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );

  return { result, queryClient, router, history };
}

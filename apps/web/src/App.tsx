import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { RouterProvider } from '@tanstack/react-router';
import { router } from './router';

/**
 * Query client defaults:
 * - GET snapshots use a finite stale time so the UI does not refetch on every
 *   keystroke or component remount.
 * - Retries only network failures and 5xx servers; 401/403/404 are never
 *   blindly retried.
 * - Errors surface through route/error boundaries without exposing raw bodies.
 */
function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 30_000,
        retry: (failureCount, error) => {
          if (failureCount >= 2) {
            return false;
          }
          if (error instanceof Error && 'status' in error) {
            const status = (error as Error & { status?: number }).status;
            if (status !== undefined) {
              return status >= 500;
            }
          }
          return true;
        },
      },
    },
  });
}

const queryClient = createQueryClient();

export function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  );
}

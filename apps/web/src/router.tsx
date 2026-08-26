/**
 * Application router.
 *
 * URL is the source of truth for all navigation and search state. Route
 * paths follow the workspace route table:
 *
 * ```text
 * /                                  OverviewPage
 * /services/$serviceId               ServicePage
 * /issues                            IssuesPage
 * /issues/$issueId                   IssueDetailPage
 * /investigations/$investigationId   InvestigationPage
 * ```
 *
 * Every route carries a Chinese title in `staticData`; navigation labels are
 * also Chinese. Detail-page params are read inside the page components via
 * `useParams`.
 */
import { createRootRoute, createRoute, createRouter } from '@tanstack/react-router';
import { AppShell } from './app/AppShell';
import { NotFoundPage } from './app/NotFoundPage';
import { OverviewPage } from './routes/OverviewPage';
import { ServicePage } from './app/ServicePage';
import { IssuesPage } from './routes/IssuesPage';
import { IssueDetailPage } from './routes/IssueDetailPage';
import { InvestigationPage } from './routes/InvestigationPage';
import { RoutePending } from './app/RoutePending';
import { RouteError } from './app/RouteError';

/** Stable Chinese route titles keyed by route path. */
const ROUTE_TITLES: Record<string, string> = {
  '/': '总览',
  '/services/$serviceId': '服务',
  '/issues': '问题',
  '/issues/$issueId': '问题详情',
  '/investigations/$investigationId': '调查',
};

const rootRoute = createRootRoute({
  component: AppShell,
  pendingComponent: RoutePending,
  errorComponent: RouteError,
  notFoundComponent: NotFoundPage,
});

const overviewRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/',
  component: OverviewPage,
  staticData: { title: ROUTE_TITLES['/'] },
});

const serviceRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/services/$serviceId',
  component: ServicePage,
  staticData: { title: ROUTE_TITLES['/services/$serviceId'] },
});

const issuesRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/issues',
  component: IssuesPage,
  staticData: { title: ROUTE_TITLES['/issues'] },
});

const issueDetailRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/issues/$issueId',
  component: IssueDetailPage,
  staticData: { title: ROUTE_TITLES['/issues/$issueId'] },
});

const investigationRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/investigations/$investigationId',
  component: InvestigationPage,
  staticData: { title: ROUTE_TITLES['/investigations/$investigationId'] },
});

const routeTree = rootRoute.addChildren([
  overviewRoute,
  serviceRoute,
  issuesRoute,
  issueDetailRoute,
  investigationRoute,
]);

/**
 * The production router instance. Created once so the instance and its internal
 * stores are stable across hot reloads.
 */
export const router = createRouter({
  routeTree,
  defaultPendingComponent: RoutePending,
  defaultErrorComponent: RouteError,
  defaultNotFoundComponent: NotFoundPage,
  defaultPreloadStaleTime: 0,
});

/**
 * Create a fresh router instance bound to an injected history. Tests use this
 * with a memory history; production uses the default browser history where the
 * URL is authoritative.
 */
export function createTestRouter<THistory extends NonNullable<Parameters<typeof createRouter>[0]['history']>>(
  history: THistory,
) {
  return createRouter({
    routeTree,
    history,
    defaultPendingComponent: RoutePending,
    defaultErrorComponent: RouteError,
    defaultNotFoundComponent: NotFoundPage,
    defaultPreloadStaleTime: 0,
  });
}

declare module '@tanstack/react-router' {
  interface Register {
    router: typeof router;
  }
}

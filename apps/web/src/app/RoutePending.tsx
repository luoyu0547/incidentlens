/**
 * Default pending/loading state for route transitions.
 *
 * Shown while the route's loader (if any) is resolving. This component is
 * deliberately minimal — no raw response bodies or internal state is exposed.
 */
export function RoutePending() {
  return <div>加载中...</div>;
}

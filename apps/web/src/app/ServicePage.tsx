import { useParams } from '@tanstack/react-router';

/**
 * ServicePage placeholder.
 *
 * The full service detail view (instances, logs, issues) arrives in a later
 * task. The route itself uses the `serviceId` path parameter so it is matched
 * by the router and visible in navigation.
 */
export function ServicePage() {
  const { serviceId } = useParams({ strict: false });
  return <h2>服务 {serviceId}</h2>;
}

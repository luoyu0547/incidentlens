import { useParams } from '@tanstack/react-router';

/**
 * InvestigationPage placeholder. The full investigation summary view arrives in
 * a later task. The route uses the `investigationId` path parameter so it is
 * matched by the router and reachable from navigation.
 */
export function InvestigationPage() {
  const { investigationId } = useParams({ strict: false });
  return <h2>调查 {investigationId}</h2>;
}

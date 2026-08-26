import { Link } from '@tanstack/react-router';

export function RelatedInvestigations({ investigationIds }: { readonly investigationIds: readonly string[] }) {
  return (
    <section aria-label="关联调查">
      <h3>关联调查</h3>
      {investigationIds.length === 0 ? (
        <p>无关联调查</p>
      ) : (
        <ul>
          {investigationIds.map((investigationId) => (
            <li key={investigationId}>
              <Link to="/investigations/$investigationId" params={{ investigationId }}>
                {investigationId}
              </Link>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

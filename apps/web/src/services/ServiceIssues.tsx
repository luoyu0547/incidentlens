import { Link } from '@tanstack/react-router';

export function ServiceIssues({ issueIds }: { readonly issueIds: readonly string[] }) {
  return (
    <section aria-label="关联问题">
      <h3>关联问题</h3>
      {issueIds.length === 0 ? (
        <p>无关联问题</p>
      ) : (
        <ul>
          {issueIds.map((issueId) => (
            <li key={issueId}>
              <Link to="/issues/$issueId" params={{ issueId }}>{issueId}</Link>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

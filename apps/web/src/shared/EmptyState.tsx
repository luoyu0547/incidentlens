export interface EmptyStateProps { readonly title: string; readonly description?: string; }

export function EmptyState({ title, description }: EmptyStateProps) {
  return <section role="status" aria-label={title} tabIndex={0}><h3>{title}</h3>{description ? <p>{description}</p> : null}</section>;
}

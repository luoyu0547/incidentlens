export interface StackTraceProps { readonly headline: string; readonly lines: readonly string[]; }

export function StackTrace({ headline, lines }: StackTraceProps) {
  return <pre aria-label="stack trace"><strong>{headline}</strong>{lines.length > 0 && `\n${lines.join('\n')}`}</pre>;
}

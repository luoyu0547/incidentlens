/**
 * Shared timestamp renderer.
 *
 * Formats an ISO-8601 timestamp for a Chinese audience and exposes the original
 * machine-readable value via the `datetime` attribute so the semantic instant is
 * preserved regardless of the human-presented format. The formatter is internal
 * (not exported) so the file stays component-only for fast-refresh.
 */
export function Timestamp({ value, timeZone }: { value: string; timeZone?: string }) {
  const formatted = new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
    timeZone,
  }).format(new Date(value));

  return <time dateTime={value}>{formatted}</time>;
}

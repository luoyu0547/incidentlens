import { useState } from 'react';

export interface StructuredJsonProps { readonly value: unknown; readonly depth?: number; readonly label?: string; }

export function StructuredJson({ value, depth = 0, label }: StructuredJsonProps) {
  const [open, setOpen] = useState(depth < 2);
  const object = typeof value === 'object' && value !== null;
  if (!object) return <span>{label ? <><span>{label}: </span></> : null}<span>{String(value)}</span></span>;
  const entries = Array.isArray(value) ? value.map((item, index) => [String(index), item] as const) : Object.entries(value as Record<string, unknown>);
  const title = Array.isArray(value) ? `[${entries.length}]` : `{${entries.length}}`;
  return <div className="structured-json">
    <button type="button" aria-expanded={open} onClick={() => setOpen((current) => !current)}>{label ? `${label}: ` : ''}{title}</button>
    {open && <div role="group" style={{ paddingLeft: '1rem' }}>{entries.map(([key, child]) => <div key={key}><StructuredJson label={key} value={child} depth={depth + 1} /></div>)}</div>}
  </div>;
}

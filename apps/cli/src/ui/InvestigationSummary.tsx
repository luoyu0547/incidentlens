import React from 'react';
import { Box, Text } from 'ink';
import type { InvestigationSummaryView } from '@incidentlens/protocol';

export interface InvestigationSummaryProps { readonly investigation: InvestigationSummaryView; readonly noColor?: boolean }

const max = 160;
function bounded(value: string): string { return value.length > max ? `${value.slice(0, max - 1)}…` : value; }

export function InvestigationSummary({ investigation, noColor }: InvestigationSummaryProps): React.ReactElement {
  const uncertain = investigation.status.toLowerCase() === 'uncertain' || investigation.status.toLowerCase() === 'unknown';
  const color = noColor ?? process.env.NO_COLOR !== undefined ? undefined : uncertain ? 'magenta' : 'cyan';
  return <Box flexDirection="column" borderStyle="round" borderColor={color} paddingX={1}>
    <Text bold color={color}>Investigation {investigation.investigation_id} — {uncertain ? 'UNCERTAIN' : investigation.status}</Text>
    <Text>Symptom: {bounded(investigation.symptom)}</Text>
    {investigation.hypotheses?.map((h) => <Text key={h.hypothesis_id}>Hypothesis [{h.status}] {h.hypothesis_id}: {bounded(h.summary)}</Text>)}
    {investigation.evidence?.map((e) => <Text key={e.evidence_ref_id}>Evidence {e.evidence_ref_id}: {bounded(e.summary)}</Text>)}
    {investigation.milestones?.map((m) => <Text key={m.event_id}>Milestone [{m.status ?? 'unknown'}] {bounded(m.summary ?? m.event_type)}</Text>)}
    {investigation.pending_approval_ids?.map((id) => <Text key={id}>Approval pending: {id}</Text>)}
    {uncertain && <Text>State is uncertain; automatic retry is unavailable.</Text>}
  </Box>;
}

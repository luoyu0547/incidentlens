import type { EvidenceSnippetView } from '@incidentlens/protocol';
import { EvidenceList } from '../issues/EvidenceList';
export function InvestigationEvidence({ evidence }: { evidence?: EvidenceSnippetView[] }) { return <EvidenceList evidence={evidence} />; }

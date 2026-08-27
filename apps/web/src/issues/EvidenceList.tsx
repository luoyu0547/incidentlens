import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import type { EvidenceSnippetView } from '@incidentlens/protocol';
import { evidenceQuery } from '../api/queries';
import { Timestamp } from '../shared/Timestamp';

function EvidenceItem({ evidence }: { evidence: EvidenceSnippetView }) {
  const [expanded, setExpanded] = useState(false);
  const detail = useQuery({ ...evidenceQuery(evidence.evidence_ref_id), enabled: expanded });
  const reveal = () => {
    if (!expanded) window.history.pushState({}, '', window.location.href);
    setExpanded(true);
  };
  return <li><p>{evidence.summary}</p><p>类型：{evidence.evidence_kind}；时间：<Timestamp value={evidence.created_at} /></p><button type="button" onClick={reveal}>查看已脱敏证据</button>{expanded && (detail.isPending ? <p>正在加载证据…</p> : detail.data ? <pre>{detail.data.content_redacted}</pre> : <p>证据加载失败</p>)}</li>;
}

export function EvidenceList({ evidence }: { evidence?: EvidenceSnippetView[] }) {
  return <section aria-label="证据"><h3>证据</h3>{evidence?.length ? <ul>{evidence.map((item) => <EvidenceItem key={item.evidence_ref_id} evidence={item} />)}</ul> : <p>暂无证据</p>}</section>;
}

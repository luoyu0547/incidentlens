import type { ChangeSummaryView } from '@incidentlens/protocol';
import { Timestamp } from '../shared/Timestamp';

export function ResolutionPanel({ resolution }: { resolution?: ChangeSummaryView | null }) {
  return <section aria-label="处理结果"><h3>处理结果</h3>{resolution ? <dl><dt>状态</dt><dd>{resolution.status}</dd><dt>影响范围</dt><dd>{resolution.scopes.join('、')}</dd><dt>文件数</dt><dd>{resolution.file_count}</dd>{resolution.created_at ? <><dt>创建时间</dt><dd><Timestamp value={resolution.created_at} /></dd></> : null}{resolution.updated_at ? <><dt>更新时间</dt><dd><Timestamp value={resolution.updated_at} /></dd></> : null}</dl> : <p>尚无处理结果</p>}</section>;
}

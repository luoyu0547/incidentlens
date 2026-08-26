import type { VerificationSummaryView } from '@incidentlens/protocol';
import { Timestamp } from '../shared/Timestamp';

export function VerificationPanel({ verification }: { verification?: VerificationSummaryView | null }) {
  const label = verification?.passed === true ? '通过' : verification?.passed === false ? '失败' : verification ? '结论不明确' : '未运行';
  return <section aria-label="验证结果"><h3>验证结果</h3><p>{label}</p>{verification ? <><p>{verification.summary}</p>{verification.validator ? <p>验证器：{verification.validator}</p> : null}<p><Timestamp value={verification.created_at} /></p></> : <p>未运行验证，不能推断成功。</p>}</section>;
}

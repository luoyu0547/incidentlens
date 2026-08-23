# Tencent CVM 真实闭环验收

这不是按固定工具序列播放的演示。IncidentLens 通过真实 SSH transport 操作一个受控 Tencent
CVM 目标，`deepseek-v4-flash` 在正常 Agent Loop 中自行读取注册信息、Compose、配置、源码、
容器状态和请求结果，定位两个独立回归，再完成修复、回滚、故障复现、重新应用和最终验证。

## 结果

| 阶段 | stable/10 | stable/500 | canary/10 | canary/500 |
| --- | ---: | ---: | ---: | ---: |
| 初始故障 | 201 | 429 | 503 | 503 |
| 首次修复 | 201 | 201 | 201 | 201 |
| 回滚 payment 修复 | 201 | 429 | 201 | 429 |
| 重新应用 | 201 | 201 | 201 | 201 |
| 结束后独立 SSH 复查 | 201 | 201 | 201 | 201 |

根因分别是 `payment-service` 的拒付阈值误设为 100，以及 canary 的数据库端口误设为
55432。Agent 把 canary 端口恢复为 5432，并把 payment policy 恢复为项目默认的
policy-a/1000000。首次四格 201 后，操作员使用 IncidentLens 原生 `:rollback` 回滚 payment
changeset；重新构建后两条大额路径真实恢复 429，而 canary 小额仍为 201，证明两个修复可独立
辨别。Agent 随后基于新 SHA 重新提出修复，最终四格再次全部为 201。

运行状态为 `completed`：24 轮、60 次工具调用、42 条持久 evidence、12 次精确批准、0 次未
批准 mutation。完成后自动提取了 5 条项目级、带 investigation 与 evidence provenance 的
active memory，不需要人工审批，也不替代当前事故证据。

## Context 说明

本次每轮最大输入为 33,363 tokens，没有达到语义压缩压力阈值，因此
`context.compacted=0`、`agent_compact_boundaries=0`。这恰好证明 Runtime 没有“每几轮压一次”
或“压缩后只留最近 3 个结果”的固定窗口；在预算内，24 轮 transcript 组保持可见。这里不声称
真实云端运行验证了压缩质量，压缩正确性由独立的压力/恢复测试覆盖。

## 可审计材料

- [Manifest](manifest.json)
- [最终矩阵](final-matrix.jsonl)
- [Asciinema cast](../../assets/hard-cloud-task7m.cast)
- [结构化 trace](../../assets/hard-cloud-task7m.trace.jsonl)
- [纯文本事件记录](../../assets/hard-cloud-task7m.txt)

同步录制版本生成时，Conclusion 已持久化到 SQLite 但尚未发布 `conclusion.created` 事件。发布
trace 因此追加了一条明确标记为 `persisted_runtime_projection` 的 `report.generated` 记录；导出器
会逐项验证引用的 evidence 属于同一 parent run，失败则拒绝导出。代码现已补上实时 Conclusion
事件，后续录制无需该兼容投影。

复核命令：

```bash
./.venv/bin/python -m tests.eval.cloud_closed_loop \
  --trace docs/assets/hard-cloud-task7m.trace.jsonl \
  --matrix docs/cloud-acceptance/hard-incident/final-matrix.jsonl
```

验收器输出：`{"passed": true, "failures": []}`。该结果只证明受控目标上的有边界闭环能力，
不表示 IncidentLens 可以绕过注册范围或无需审批修改任意生产主机。

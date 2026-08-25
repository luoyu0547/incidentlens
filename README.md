# IncidentLens

> 面向已注册云主机的、安全优先事故调查控制台。

IncidentLens 将项目注册、脱敏日志、证据、受限 Agent 调查、人工审批、变更备份和调查报告串成一条可审计的诊断链路。它不是故障注入演示，也不会在远程服务器部署 IncidentLens agent，更不会把通用 SSH Shell 交给模型。

### Tencent CVM 真实 Agent 闭环

2026-08-24 完成了一次受控 Tencent CVM 真实验收。配置的 `deepseek-v4-flash` 通过正常
IncidentLens Agent Loop 和 SSH transport 自主调查两个并发回归：payment 拒付阈值导致 stable
大额请求 429，canary 数据库端口漂移导致全部请求 503。操作员只批准精确显示的文件修改、容器
读取与 Compose 重建；Agent 完成首次修复后，使用 IncidentLens 原生 changeset rollback 真实
恢复 payment 故障，再基于新 SHA 重新应用修复。

| 阶段 | stable/10 | stable/500 | canary/10 | canary/500 |
| --- | ---: | ---: | ---: | ---: |
| 故障基线 | 201 | 429 | 503 | 503 |
| 首次修复 | 201 | 201 | 201 | 201 |
| 回滚 payment | 201 | 429 | 201 | 429 |
| 重新应用/最终复查 | 201 | 201 | 201 | 201 |

该运行最终为 `completed`，共 24 轮、60 次工具调用、42 条持久 evidence、12 次精确批准、
0 次未批准 mutation，并自动提取 5 条有 provenance 的 Project Memory。云端闭环 evaluator 返回
`passed: true`。完整的 [验收说明](docs/cloud-acceptance/hard-incident/README.md)、
[manifest](docs/cloud-acceptance/hard-incident/manifest.json)、
[结构化 trace](docs/assets/hard-cloud-task7m.trace.jsonl)、
[终端 cast](docs/assets/hard-cloud-task7m.cast) 和
[纯文本记录](docs/assets/hard-cloud-task7m.txt) 均已保留并记录 SHA-256。

这次真实运行的单轮峰值输入为 33,363 tokens，未达到压缩压力阈值，因此语义压缩次数为 0。
这不是缺失演示，而是诚实结果：Runtime 不按轮次压缩，也不存在“压缩后只留最近 3 个结果”的
固定窗口；预算内完整 transcript 保持可见。该记录不宣称验证了真实长上下文压缩质量，也不表示
系统可以绕过注册 scope 或审批修改任意生产环境。

## 它解决什么问题

当生产服务出现 5xx、超时或依赖不可用时，IncidentLens 让调查在明确边界内进行：

```text
注册的项目/目标
       │
       ├── 受策略约束的 SSH、文件与容器操作
       ├── 已脱敏的日志查询与订阅
       ▼
追加式证据库 ──> 有界调查运行时 ──> 审批/变更门禁 ──> Markdown + HTML 报告
```

所有对模型可见的外部事实都来自追加式、已脱敏的证据库。模型只能提出工具调用、委派、假设和结论；真正的执行始终经过策略、会话、审批及备份边界。

## 核心能力

- **项目与目标注册**：在 SQLite 中维护项目、SSH 目标、Compose 服务、容器和允许访问的路径。
- **安全远程诊断**：每个目标复用持久 SSH/SFTP/PTY 通道；提供受限的读、列举、搜索、状态和文件操作，而非通用 Shell。
- **日志到证据**：查询主机或已注册容器日志，逐行解析、脱敏并限制到 16 KiB；支持全文检索、订阅与 WebSocket 重放。
- **有界 Agent 运行时**：Provider 只提出方案；编排器限制轮次、工具调用、时间、输出和证据预算，并支持父/子运行、检查点、取消、恢复和重启恢复。调查状态与模型上下文相互分离，Runtime 通过版本化 Session Memory 和最近增量重建有界上下文，完整证据按需读取。
- **人工可控的风险操作**：Docker、PTY、写文件等操作生成精确的一次性审批；永久拒绝 `rm -rf`、重定向、管道和命令替换等危险模式。
- **可回滚的远程变更**：写入前强制创建本地加密备份与远端同目录时间戳备份，使用陈旧写检测和原子替换；多文件变更支持回滚。
- **调查报告**：支持生成 Markdown/HTML 双格式调查报告。

## 快速开始

要求：Python **3.12**、[uv](https://docs.astral.sh/uv/)。Docker 只在运行集成/验收场景时需要。

```bash
# 安装依赖
uv sync

# 运行全部离线测试
uv run pytest -q

# 启动 API
uv run uvicorn incidentlens_control_plane.main:app --reload
```

运行数据默认存储在 `~/.incidentlens`；若要隔离一次本地体验：

```bash
export INCIDENTLENS_DATA_DIR="$(pwd)/.incidentlens-data"
uv run uvicorn incidentlens_control_plane.main:app --reload
```

## 一次调查如何流转

1. 通过 `POST /api/projects` 注册项目、目标和服务范围。
2. 通过 `POST /api/investigations` 创建调查，再使用 `POST /api/investigations/{id}/start` 以限定 scope 启动或恢复它。
3. 调查运行时收集已脱敏证据、提出有证据引用的假设/结论，必要时暂停等待精确审批。
4. 通过 API 查询结论、证据、变更与相关日志；报告文件默认输出至 `$INCIDENTLENS_DATA_DIR/reports/{id}.md` 和 `.html`。

API 启动调查时需要明确 scope，例如主机日志范围：

```json
{
  "scope": {
    "project_id": "checkout",
    "target_id": "prod-sg-1",
    "scope": "host"
  }
}
```

默认使用确定性的 `FakeProvider`，用于可重复的离线调查和验收。设置 `.env` 后可启用任意支持 Chat Completions 的 OpenAI-compatible 模型：

```dotenv
INCIDENTLENS_AGENT_MODE=llm_agent
INCIDENTLENS_LLM_ACTIVE_MODEL=deepseek-v4-flash
INCIDENTLENS_LLM_BASE_URL=https://api.deepseek.com
INCIDENTLENS_LLM_API_KEY=your_key
```

模型 ID 会原样发送给所配置的 OpenAI-compatible API。无论使用 FakeProvider 还是真实模型，模型都只能通过同一受限的 `ModelProvider` 合约提出建议，不能绕过工具白名单、scope、证据校验或审批边界。

## 安全边界

| 边界 | 约束 |
| --- | --- |
| 远程执行 | 不暴露通用 Shell/SSH 工具；命令分为自动读取、需审批与禁止三类。 |
| 路径访问 | 只允许注册根目录内的受限文件操作，拒绝遍历与符号链接逃逸。 |
| 日志与证据 | 不持久化或返回原始日志；所有内容先脱敏、截断，再作为证据引用。 |
| 模型输出 | 只接受白名单工具与已拥有证据的引用；无证据结论会暂停而不是编造。 |
| 高风险变更 | 审批精确、单次使用；写入前必须完成双重备份。 |
| 故障恢复 | 危险的在途调用重启后标记为 `UNCERTAIN`，绝不自动重放。 |

## 部署与产品契约约束

产品 API 的稳定契约位于 `packages/protocol/`，由 `scripts/export_product_contracts.py` 生成，并由 `scripts/check_product_contracts.py` 在 CI 或发布前检查漂移。客户端必须先通过 `/api/v1/version` 协商 API/stream schema 版本；未知版本必须显式失败，不能静默降级。

当前控制面是本地单用户运行时，部署时必须使用**单个 Uvicorn worker**，并挂载持久化本地数据卷（SQLite、加密备份、运行时检查点和报告）。生产环境必须配置认证 profile 与 session signing key，并在 TLS 反向代理后运行以保证 secure cookie；不能把签名密钥或 bearer token 放入 API 请求体、日志或公开 schema。旧 `/api/*` 路由仅作为临时兼容层共存，可通过 `INCIDENTLENS_LEGACY_API_ENABLED` 关闭，不得作为新客户端依赖。CLI/Web stream 连接必须携带并校验 schema version，断线后使用 cursor/sequence 恢复并处理 gap/backpressure 信号。

契约与后端验收：

```bash
uv run python scripts/check_product_contracts.py
uv run pytest tests/contracts tests/acceptance/test_product_api_foundation.py -q
uv run ruff check apps/control-plane/src tests scripts
```


```bash
# 全量离线测试与静态检查
uv run pytest -q
uv run ruff check .

# Phase 4 harness evaluator and deterministic runner
uv run pytest tests/eval/test_harness_eval.py -q
uv run python tests/eval/runner.py --json .incidentlens/harness-eval.json

# Opt-in real model invariants (reuses configured model settings; skipped otherwise)
# Targets: foreign evidence, scope/policy bypass, and unapproved mutation = 0;
# tool pairing and child exactly-once = 100%.
INCIDENTLENS_RUN_LIVE_MODEL_TESTS=1 uv run pytest tests/integration/test_live_model_harness.py -q

# 报告与离线端到端流程
uv run pytest tests/reports/ tests/acceptance/test_e2e_investigation.py -v

# 可选：Docker 验收场景
cd infra/acceptance && docker compose up -d
INCIDENTLENS_RUN_ACCEPTANCE=1 uv run pytest ../../tests/acceptance/test_docker_scenarios.py -v
```

其他可选的真实集成验证：

```bash
INCIDENTLENS_RUN_LIVE_SSH=1 uv run pytest tests/integration/test_live_ssh_tools.py -q
INCIDENTLENS_RUN_LIVE_LOG_TESTS=1 uv run pytest tests/integration/test_live_log_tools.py -q
INCIDENTLENS_RUN_LIVE_AGENT_TESTS=1 uv run pytest tests/integration/test_live_agent_runtime.py -q
```

详细步骤见 [Phase 1](docs/phase-1-local-runtime-verification.md)、[Phase 2](docs/phase-2-remote-tools-verification.md)、[Phase 3](docs/phase-3-hybrid-log-evidence-verification.md) 与 [Phase 4](docs/phase-4-agent-runtime-verification.md) 验证记录。

## 项目结构

```text
apps/control-plane/src/incidentlens_control_plane/
├── project_registry/  # 项目、目标、服务与路径范围
├── remote_ops/        # SSH 会话、命令/路径策略、受限文件操作
├── logs/              # 解析、脱敏、检索、订阅与关联
├── evidence/          # 追加式脱敏证据库
├── investigation/    # 有界编排器、Provider 合约、恢复与工具执行
├── approvals/         # 单次精确审批
├── changes/           # 双备份、原子修改与回滚
└── reports/           # Markdown/HTML 报告
```

`infra/acceptance/` 包含用于 Docker 验收的微服务和故障场景；`tests/` 按领域模块组织离线、集成与验收测试。

## 配置

| 环境变量 | 用途 | 默认值 |
| --- | --- | --- |
| `INCIDENTLENS_DATA_DIR` | SQLite、加密备份库与报告输出目录 | `~/.incidentlens` |
| `INCIDENTLENS_AGENT_MODE` | `fake` 或 `llm_agent` | `fake` |
| `INCIDENTLENS_LLM_BASE_URL` | OpenAI-compatible API 根地址 | 无 |
| `INCIDENTLENS_LLM_ACTIVE_MODEL` | 原样发送给 Provider 的模型 ID | 无 |
| `INCIDENTLENS_LLM_API_KEY` | Provider API Key | 无 |
| `INCIDENTLENS_RUN_LIVE_SSH` | 启用真实 SSH 集成测试 | 未设置（跳过） |
| `INCIDENTLENS_RUN_LIVE_LOG_TESTS` | 启用真实日志集成测试 | 未设置（跳过） |
| `INCIDENTLENS_RUN_LIVE_AGENT_TESTS` | 启用真实 Agent 集成测试 | 未设置（跳过） |
| `INCIDENTLENS_RUN_LIVE_MODEL_TESTS` | 启用真实 model harness invariant 测试 | 未设置（跳过） |

## 当前范围

IncidentLens 是本地单用户 API 服务。远端侧无需部署 agent；实际远程访问仍需在项目注册中显式配置目标与可访问范围。

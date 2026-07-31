# IncidentLens Phase 5：人工确认与组织记忆闭环设计

## 1. 目标

Phase 5 在前四阶段已完成的可复现微服务环境、证据驱动调查、真实 LLM
Agent 和可信结论门禁之上，补齐人工确认与组织记忆治理闭环：

```text
调查完成
  -> 自动生成待审核案例
  -> 人工修改、确认或驳回
  -> 已确认案例进入正式检索集合
  -> 后续调查将历史案例转为候选假设
  -> 当前遥测重新验证历史方向
  -> 记录召回、采用、误导和人工反馈
  -> Web 页面展示治理记录与真实评测结果
```

本阶段主要覆盖 `REQUIREMENTS.md` 中 FR-06、FR-07、FR-08，并完成 MVP 验收标准
第 6、7、8、9、10 项所需的知识闭环。

## 2. 已确认的范围

Phase 5 完整交付：

- `draft / agent_generated / human_verified / deprecated / rejected` 五状态案例生命周期；
- 调查报告自动、幂等地沉淀为 `agent_generated` 案例；
- 根因与解决方案的修改、确认、驳回、废弃和重新审核；
- `helpful / partial / irrelevant / stale / wrong` 案例反馈；
- 案例召回、采用、被当前证据排除和误导统计；
- 症状、服务、根因类别、环境和版本适用范围过滤；
- SQLite FTS5 与可插拔 Embedding 组成的混合检索；
- 无 Embedding 配置或调用失败时的离线关键词降级；
- Web 案例治理、调查 JSON 导出和评测对比面板；
- 从当前简化案例表到 Phase 5 schema 的非破坏性迁移；
- 覆盖领域、存储、API、Agent、评测、Web 和 Compose 闭环的测试。

## 3. 非目标

- 不连接真实企业生产环境；
- 不执行重启、回滚、配置修改或数据库写入等生产处置；
- 不引入 Kubernetes、自动修复、多 Agent、多租户、OAuth 或复杂企业权限；
- 不引入前端框架，继续使用现有原生 HTML、CSS 和 JavaScript；
- 不要求本地大模型或大型 Embedding 模型成为默认依赖；
- 不将历史案例直接作为当前事故的已确认结论；
- 不展示模型隐藏推理或未经核验的思维链；
- 不实现完整事件溯源，案例当前状态保存在关系表中，关键动作使用追加式审计记录。

## 4. 架构选择

采用“关系型案例聚合 + 追加式审计表”。

`case_memory` 保存案例当前快照。审核、反馈、召回、采用和误导等行为写入独立的
追加式记录。状态变化只能通过领域服务完成，存储层不自行决定状态转换。

该方案沿用现有 FastAPI、Pydantic、SQLAlchemy 和 SQLite 技术栈，能够直接支持
过滤、统计、事务和审计，同时避免单 JSON 文档难以约束、完整事件溯源实现成本过高
的问题。

## 5. 组件边界

### 5.1 CaseService

`CaseService` 是案例写操作的唯一入口，负责：

- 从完成的调查中幂等生成 `agent_generated` 案例；
- 校验状态转换；
- 修改案例内容并使其重新进入 `draft`；
- 确认、驳回和废弃案例；
- 执行乐观并发控制；
- 在同一事务内写入审核动作并维护检索索引；
- 返回适合 API 和 Agent 使用的领域对象。

路由、Agent 运行时和 Web 页面不得直接改变案例状态。

### 5.2 CaseRepository

`CaseRepository` 只负责：

- 案例与关联记录的增删查改；
- FTS5 索引写入与删除；
- Embedding 记录持久化；
- 列表、详情、历史和统计查询；
- 事务边界所需的存储操作。

状态机判断、API 错误映射和 Agent 决策不进入 Repository。

### 5.3 HybridCaseRetriever

`HybridCaseRetriever` 负责：

- 只从 `human_verified` 集合检索；
- 执行结构化硬过滤；
- 合并 FTS5 与可选语义召回；
- 计算可解释排序；
- 生成命中字段、得分和相似理由；
- 在 Embedding 不可用时降级到 FTS5。

### 5.4 EmbeddingProvider

Embedding 使用可插拔接口。默认实现为禁用状态，不发起网络调用；配置实现提供：

- 文档向量生成；
- 查询向量生成；
- provider、model、维度和内容哈希元数据；
- 有界超时和可识别错误。

检索功能不能依赖 Embedding 才能工作。

### 5.5 CaseUsageTracker

`CaseUsageTracker` 追加记录：

- `recalled`：案例进入某次调查的召回结果；
- `adopted`：案例根因被转为当前调查的候选假设；
- `validated`：当前证据支持该历史方向；
- `misleading`：当前证据明确排除该历史方向。

每个事件携带案例、调查、候选假设、排名、相似理由和幂等键。

### 5.6 InvestigationExportService

导出服务以只读方式汇总：

- 调查告警与最终状态；
- 最新检查点；
- 已加载 Skill；
- 工具调用审计；
- 假设、证据与证据引用；
- 历史案例召回与采用记录；
- 最终报告；
- 关联案例和审核状态；
- 模型与运行时的非敏感身份信息。

导出结果执行统一密钥脱敏，带 schema version，并受响应大小上限约束。

### 5.7 EvaluationRunStore

评测运行存储保存实际 `RunRecord` 和聚合指标：

- 策略；
- 场景；
- 开始与完成时间；
- 逐场景实际输出；
- 八项聚合指标；
- 运行状态和错误摘要。

Web 只读取已完成的实际结果。没有运行结果时显示空状态，不生成示例成功率。

## 6. 数据模型

### 6.1 case_memory

案例主表至少包含：

- `id`；
- 唯一 `incident_id`；
- `status`；
- `revision`；
- `symptom`；
- `affected_services`；
- `root_cause_category`；
- `root_cause_description`；
- `key_evidence`；
- `investigation_path`；
- `invalid_hypotheses`；
- `resolution`；
- `remediation_advice`；
- `applicability_conditions`；
- `inapplicability_conditions`；
- `environment`；
- `service_version_exact`；
- `service_version_min`；
- `service_version_max`；
- `source_report`；
- `created_at`；
- `updated_at`；
- `verified_at`；
- `deprecated_at`。

嵌套证据、排查路径和报告保留结构化 JSON；需要过滤、约束或排序的字段使用独立列。

### 6.2 case_review_actions

每条审核记录包含：

- `case_id`；
- `action`；
- `from_status`；
- `to_status`；
- `actor`；
- `reason`；
- `before_summary`；
- `after_summary`；
- `created_at`。

审核记录只追加，不更新和删除。

### 6.3 case_feedback

每条反馈包含：

- `case_id`；
- 可选 `incident_id`；
- `rating`；
- `actor`；
- `comment`；
- `idempotency_key`；
- `created_at`。

`rating` 只允许 `helpful / partial / irrelevant / stale / wrong`。

### 6.4 case_usage_events

每条使用事件包含：

- `case_id`；
- `incident_id`；
- 可选 `hypothesis_id`；
- `event_type`；
- `rank`；
- `retrieval_mode`；
- `lexical_score`；
- `semantic_score`；
- `filter_score`；
- `similarity_reason`；
- `idempotency_key`；
- `created_at`。

### 6.5 case_embeddings

每条向量记录包含：

- `case_id`；
- `provider`；
- `model`；
- `dimension`；
- `content_hash`；
- `vector`；
- `updated_at`。

同一案例只保留当前可检索内容对应的向量。案例退出正式检索集合时，其向量不得参与召回。

### 6.6 case_fts

使用 SQLite FTS5 虚拟表索引：

- 症状；
- 根因类别与描述；
- 解决方案；
- 适用条件；
- 服务名称。

FTS 只包含 `human_verified` 案例。索引内容由 CaseService 在事务中维护。

## 7. 状态机

```text
report_ready ──幂等创建──> agent_generated

draft ────────────────确认──> human_verified
agent_generated ──────确认──> human_verified

draft ────────────────驳回──> rejected
agent_generated ──────驳回──> rejected

human_verified ───────废弃──> deprecated

rejected ─────────────修改──> draft
deprecated ───────────修改──> draft
human_verified ───────修改──> draft
agent_generated ──────修改──> draft
draft ────────────────修改──> draft
```

约束：

- `incident_id` 唯一，重复终态执行返回同一案例；
- 客户端不能直接指定状态；
- 修改已确认案例时立即移出 FTS5 和语义召回集合；
- 重新确认后才恢复检索；
- 非法跳转返回冲突，不静默纠正；
- 所有修改请求使用 `expected_version` 对应当前 `revision`；
- 成功修改或状态变化后 `revision` 加一。

## 8. 自动沉淀

自动沉淀发生在调查运行时的终态钩子，不依赖 SSE 或浏览器：

1. 调查进入 `report_ready`；
2. 终态钩子将告警、报告、证据、候选假设、无效假设、工具路径、Skill 和模型身份
   转为案例输入；
3. `CaseService.materialize_from_investigation` 按 `incident_id` 幂等写入；
4. 成功后调查响应和导出结果暴露 `case_id` 与案例状态；
5. API、CLI、恢复执行和重复事件均得到相同案例。

自动生成的案例状态固定为 `agent_generated`。前端不能通过通用创建接口绕过审核并写入
`human_verified`。

## 9. 混合检索

### 9.1 查询与过滤

查询输入包括：

- 症状或自由文本；
- 服务；
- 根因类别；
- 环境；
- 当前服务版本；
- 返回数量。

服务、类别、环境和明确版本范围是硬过滤条件。不适用条件优先于适用条件。

版本规则：

- 可解析语义版本支持最小与最大范围；
- 非标准版本字符串只支持精确匹配；
- 未提供版本限制的案例视为版本通用；
- 查询未提供版本时不根据版本排除案例，但在相似理由中标记版本未验证。

### 9.2 召回与排序

1. 从 `human_verified` 集合应用硬过滤；
2. FTS5 召回关键词候选；
3. Embedding 可用时召回语义候选；
4. 按 `case_id` 取并集；
5. 计算关键词、语义、结构化适配和反馈信号；
6. 返回排序后的独立案例。

相同症状、不同根因的案例不得按文本相似度合并。去重只按 `case_id`。

返回结果包含：

- `retrieval_mode`：`hybrid` 或 `keyword_only`；
- 总分；
- 关键词得分；
- 语义得分；
- 结构化适配得分；
- 命中字段；
- 相似理由；
- 来源、状态和版本范围。

### 9.3 降级

以下情况降级到 `keyword_only`：

- 未配置 Embedding；
- provider 超时或不可用；
- 返回向量维度与索引不一致；
- 当前案例向量缺失或内容哈希过期。

降级写入安全审计信息，不记录密钥，不阻断调查。

## 10. 历史案例重新验证

召回结果只能产生当前调查的候选假设：

1. 召回时记录 `recalled`；
2. Agent 将历史根因转为候选假设时记录 `adopted`；
3. 候选假设不继承历史案例的已确认状态或置信度；
4. 当前调查继续调用只读工具；
5. 当前证据支持历史方向时记录 `validated`；
6. 当前证据明确反驳历史方向时更新当前假设为已排除并记录 `misleading`；
7. 最终报告仍必须通过 Phase 4 的当前证据门禁。

用户对案例给出 `wrong` 反馈不会在没有审核动作的情况下直接删除历史记录；它降低排序
信号并在治理页面突出显示。审核人可以随后废弃该案例。

## 11. API

### 11.1 案例读取

```text
GET /api/cases
GET /api/cases/{case_id}
GET /api/cases/search
GET /api/cases/{case_id}/history
```

列表支持状态、服务、根因类别、环境、版本、更新时间和游标分页。搜索响应包含检索模式、
分项得分和相似理由。

### 11.2 案例写入

```text
PATCH /api/cases/{case_id}
POST  /api/cases/{case_id}/confirm
POST  /api/cases/{case_id}/reject
POST  /api/cases/{case_id}/deprecate
POST  /api/cases/{case_id}/feedback
```

修改、确认、驳回和废弃请求携带 `expected_version`、本地操作者标识和可选原因。操作者标识
用于 Demo 审计，不构成身份认证。

### 11.3 调查与评测

```text
GET /api/investigations/{incident_id}/export
GET /api/evaluations/comparison
```

导出响应使用 `application/json` 和下载文件名。评测对比支持按场景、策略和最近完成时间
读取真实结果。

### 11.4 HTTP 错误语义

- `404 Not Found`：案例或调查不存在；
- `409 Conflict`：非法状态跳转、revision 冲突或幂等键载荷冲突；
- `422 Unprocessable Entity`：字段、版本范围、反馈值或请求结构无效；
- `503 Service Unavailable`：存储尚未配置；
- Embedding 不可用不返回 `503`，而是成功返回关键词降级结果。

## 12. Web 页面

现有调查时间线、工具调用、证据、历史案例和报告面板保留，并增加：

- 当前调查关联的案例状态；
- 待审核案例队列；
- 案例详情和结构化编辑表单；
- 确认、驳回、废弃和重新审核操作；
- revision 冲突提示与重新加载；
- 结构化过滤、检索模式和相似理由；
- 五类反馈；
- 召回、采用、验证和误导统计；
- 审核历史；
- 当前调查 JSON 下载；
- 三种策略、五类场景和八项真实指标对比。

三种策略固定为：

1. `react_no_memory`；
2. `memory_unverified`；
3. `incidentlens_verified`。

八项指标固定为：

- 根因服务识别准确率；
- 根因类型识别准确率；
- 证据引用正确率；
- 首次出现有效假设的轮次；
- 平均工具调用次数；
- 重复工具调用率；
- 历史案例误导率；
- 平均调查耗时。

页面不渲染隐藏推理。不存在评测结果时显示“尚无实际运行结果”，不提供伪造占位指标。

## 13. 评测持久化

现有评测 runner 继续从实际调查输出构造 `RunRecord`。Phase 5 扩展运行记录，使历史案例
误导率来自案例使用事件，而不是工具空结果或手写标签。

每次评测运行：

1. 创建运行记录；
2. 按策略和场景执行真实调查；
3. 保存逐场景 `RunRecord`；
4. 从记录计算八项指标；
5. 标记运行完成；
6. Web 读取最近一次已完成的同组结果。

失败运行保留错误摘要，但不混入成功指标。

## 14. 数据迁移

Phase 5 引入显式 schema version 和事务迁移，禁止通过删除旧表处理字段变化。

迁移规则：

- 现有 `pending_review` 映射为 `draft`；
- 现有 `human_verified` 保持不变；
- 已存在的根因文本迁移为根因描述；
- 已存在的服务、症状、解决方案和证据摘要原样保留；
- 缺少 `incident_id` 的历史案例获得稳定的 legacy 来源标识，不伪造调查关联；
- 迁移完成后重建 FTS5；
- 旧的简化关键词索引不再作为检索来源；
- 迁移失败时回滚，不留下部分新 schema。

## 15. 一致性、恢复与安全

- 案例快照、审核动作和索引变化在同一事务中提交；
- 自动沉淀失败不篡改已完成报告，记录 `case_materialization_failed`；
- 自动沉淀可按 `incident_id` 幂等重试；
- 使用事件和反馈带幂等键，恢复或重复点击不重复计数；
- Embedding 失败写入脱敏审计并降级；
- JSON 导出复用统一密钥脱敏逻辑；
- 导出限制最大响应大小；
- API 不接受任意状态写入；
- 工具安全边界继续保持只读；
- Phase 5 不新增任何生产变更能力。

## 16. 测试策略

### 16.1 领域测试

- 所有合法状态转换；
- 所有非法状态转换；
- 修改已确认案例后退出检索；
- revision 乐观锁；
- `incident_id` 幂等创建；
- 审核记录完整性。

### 16.2 存储与迁移测试

- 当前 schema 到 Phase 5 schema 的数据保留；
- 事务失败回滚；
- FTS5 创建、移除和重建；
- 反馈和使用事件幂等性；
- 统计聚合；
- 非标准版本精确匹配；
- 语义版本范围过滤。

### 16.3 检索测试

- 只返回 `human_verified`；
- 症状、服务、类别、环境和版本过滤；
- 相同症状不同根因保持独立；
- 混合排序与分项得分；
- 相似理由可追溯；
- 未配置 Embedding 的离线检索；
- 超时、维度错误和 provider 错误降级。

语义测试使用确定性 Fake EmbeddingProvider，不依赖真实供应商。

### 16.4 API 测试

- 列表、详情、搜索和历史；
- 编辑、确认、驳回、废弃和反馈；
- 非法跳转；
- revision 冲突；
- 重复幂等请求；
- 调查 JSON 导出；
- 脱敏与大小限制；
- 评测对比空状态和实际结果。

### 16.5 Agent 测试

- 历史案例只生成候选假设；
- recalled、adopted、validated 和 misleading 可追溯；
- 错误历史案例能被当前证据排除；
- 当前证据门禁不被历史状态或反馈绕过；
- 恢复执行不重复生成案例或使用事件。

### 16.6 Web 契约测试

- 待审核队列调用真实案例 API；
- 编辑和状态动作携带 revision；
- 冲突提示不覆盖服务器状态；
- 反馈、导出和对比面板使用真实响应；
- 页面不渲染隐藏推理；
- 无数据时显示明确空状态。

### 16.7 Compose 闭环验收

Compose 验收必须完成：

1. 启动环境；
2. 注入并运行故障；
3. 完成调查；
4. 自动生成 `agent_generated` 案例；
5. 人工确认案例；
6. 在后续调查中召回案例；
7. 用当前证据验证正确方向；
8. 排除至少一个错误历史方向；
9. 写入反馈与使用统计；
10. 导出完整脱敏调查 JSON；
11. 在评测对比 API 和 Web 面板读取实际运行指标。

## 17. 完成标准

Phase 5 只有同时满足以下条件才完成：

1. 五状态案例生命周期及追加式审核审计可用；
2. `report_ready` 幂等产生 `agent_generated` 案例；
3. 只有 `human_verified` 案例可被正式检索；
4. 修改或废弃案例后立即退出检索；
5. 关键词检索完整离线可用；
6. 语义检索失败可观测且不阻断调查；
7. 历史案例召回能够追溯到当前候选假设；
8. 至少一个错误历史案例被当前证据明确排除并记录为误导；
9. Web 可以完成审核、反馈、检索、导出和真实评测对比；
10. 三种策略、五种场景和八项指标均来自实际运行；
11. 旧案例非破坏性迁移成功；
12. 领域、存储、API、Agent、评测、Web 和 Compose 闭环测试通过；
13. Ruff、mypy、非集成测试和秘密扫描通过；
14. README 与评测文档说明配置、降级行为、演示步骤及项目边界。

## 18. 推荐实施顺序

1. 冻结案例领域契约和迁移行为；
2. 实现案例聚合、状态机、Repository 和 CaseService；
3. 实现 FTS5、结构化过滤与可插拔 Embedding；
4. 将召回、采用、验证和误导事件接入 Agent；
5. 将自动案例沉淀接入调查终态；
6. 实现案例治理 API 与调查导出；
7. 持久化真实评测运行并提供对比 API；
8. 扩展原生 Web 页面；
9. 完成 Compose 闭环、质量门禁和文档。

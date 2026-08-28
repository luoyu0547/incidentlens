# Task 2: 生成共享产品协议包 -- 完成报告

**Commit:** `688c8c1` `build(protocol): generate product API contracts`
**分支:** `feat/hard-cloud-incident`

---

## 改动摘要

创建了 `@incidentlens/protocol` npm workspace 包，从后端 OpenAPI 规范自动生成类型、SDK 和 schema，同时实现了手工编写的流解析器和版本兼容性断言。

### 新增文件 (27 files, +11161 lines)

| 文件 | 用途 |
|------|------|
| `packages/protocol/package.json` | 包配置，定义 generate/check/build/typecheck/test 脚本 |
| `packages/protocol/tsconfig.json` | TypeScript 配置，继承 tsconfig.base.json |
| `packages/protocol/openapi-ts.config.ts` | Hey API 代码生成配置 |
| `packages/protocol/tsup.config.ts` | 构建配置 (ESM + .d.ts) |
| `packages/protocol/src/index.ts` | 主入口，re-export 生成代码和流解析器 |
| `packages/protocol/src/stream.ts` | 流帧解析器 (`parseStreamFrame`) 和版本断言 (`assertCompatible`) |
| `packages/protocol/src/dom-shim.d.ts` | BodyInit 类型声明 (生成的 Fetch 客户端需要) |
| `packages/protocol/src/generated/**` | 自动生成的类型、SDK、schemas、Fetch 客户端 |
| `packages/protocol/scripts/check-generated.mjs` | 漂移检查脚本 (临时目录重新生成 + 逐字节对比) |
| `packages/protocol/test/contract.test.ts` | 26 个测试用例 |

### 修改文件

| 文件 | 改动 |
|------|------|
| `package.json` | 添加 `protocol:generate` 和 `protocol:check` 脚本 |
| `package-lock.json` | 新增 @hey-api/openapi-ts 等依赖 |

---

## 生成工具链

- **@hey-api/openapi-ts v0.99.0** -- OpenAPI 到 TypeScript 代码生成器
- **插件:** `@hey-api/typescript` (类型), `@hey-api/sdk` (SDK 函数), `@hey-api/schemas` (验证 schema), `@hey-api/client-fetch` (Fetch 客户端)
- **输入:** `packages/protocol/openapi/v1.json` (35 个 API 操作, 87 个 schema)
- **输出:** `packages/protocol/src/generated/` (5 个顶层文件 + client/ + core/)

### 未使用 @hey-api/zod 的原因

`@hey-api/zod` 插件在 v0.99.0 中与其他插件组合时导致 0 文件输出 (疑似兼容性 bug)。由于流 schema 是独立的 JSON Schema 文件 (不在 OpenAPI 规范中)，Zod 验证本就需要手写，因此决定用 `@hey-api/schemas` 替代。

---

## 测试命令与结果

| 命令 | 结果 |
|------|------|
| `npm run protocol:generate` | 5 files generated (487ms) |
| `npm run protocol:check` | No drift detected |
| `npm test --workspace @incidentlens/protocol` | **26/26 passed** (990ms) |
| `npm run typecheck --workspace @incidentlens/protocol` | 通过 |
| `npm run build --workspace @incidentlens/protocol` | ESM 33KB + DTS 95KB |

### 测试覆盖

1. **OpenAPI 契约就绪测试** (3 tests)
   - 15 个稳定 operation ID 存在且为函数
   - 10 个关键类型从 types.gen.ts 导出
   - schemas.gen.ts 包含验证对象

2. **流 schema 按 event_type 分派** (7 tests)
   - 4 个控制事件 (stream.hello/heartbeat/gap/slow_consumer)
   - 3 个运行时事件 (operation.running, approval.requested, agent.text.delta)
   - 未知 event_type → kind:'unknown' 保留基础字段

3. **严格流解析错误** (7 tests)
   - MALFORMED_JSON, NOT_OBJECT, UNSUPPORTED_SCHEMA_VERSION
   - MISSING_EVENT_TYPE (缺失和空字符串), MISSING_OCCURRED_AT

4. **信封字段保留** (2 tests)
   - 全字段保留 (sequence, event_id, investigation_id, session_id, target_id, payload)
   - 缺失可选字段默认为 null

5. **版本兼容性断言** (6 tests)
   - 范围内通过 (含上下边界)
   - VERSION_TOO_OLD, VERSION_TOO_NEW, MISSING_PROTOCOL_VERSION

---

## Concern

1. **@hey-api/zod 插件不兼容** -- 多插件组合时静默失败。若后续需要生成的 Zod schema (而非手写)，需关注 hey-api 上游修复或降级版本。

2. **dom-shim.d.ts 为全局声明** -- `BodyInit` 声明为 `any`，仅为了让生成的 Fetch 客户端通过类型检查。长期方案是用 `@types/node` 的内置 Fetch 类型 (Node.js 22+ 已支持)，或切换到 `@hey-api/client-node`。

3. **generated/ 目录的漂移检查依赖 openapi-ts 一致性** -- 若升级 hey-api 版本导致输出格式变化，drift check 会正确失败并提示重新生成。

4. **未修改后端或 CLI 应用代码** -- 符合 brief 约束。

---

## 修复记录 (F1 + F2)

**Commit:** `aded198` `fix(protocol): align assertCompatible with generated ApiVersionView and add runtimeEventTypes drift detection`
**分支:** `feat/hard-cloud-incident`

### F1: assertCompatible 使用 generated ApiVersionView

- **文件:** `packages/protocol/src/stream.ts`
- **问题:** 手写 `ApiVersionView` 定义了 `protocol_version` 和 `min_compatible_version` 字段，与 generated 版本 (`minimum_cli_protocol_version` 等) 不一致。`assertCompatible` 读取不存在于后端响应的 `protocol_version` 字段。
- **修复:** 删除手写 interface，改为 import 生成的 `ApiVersionView` 并 re-export。`assertCompatible` 改为读取 `minimum_cli_protocol_version`，与 OpenAPI 契约一致。
- **影响:** `assertCompatible` 的行为变化 -- 之前读取 `protocol_version`(后端从未返回该字段)，现在读取 `minimum_cli_protocol_version`（后端实际返回的字段）。

### F2: runtimeEventTypes 漂移检测

- **文件:** `packages/protocol/test/contract.test.ts`
- **问题:** `runtimeEventTypes` 数组手工维护，与 generated `RuntimeEventType` 无同步机制。若后端新增事件类型而 CLI 侧遗漏更新，不会有任何警告。
- **修复:** 新增测试用例，分别从 `types.gen.ts` 源码提取 generated `RuntimeEventType` 的字符串字面量集合，从 `stream.ts` 源码提取 `runtimeEventTypes` 数组成员集合，断言两者完全一致。
- **测试结果:** 27/27 passed (含新增 1 个漂移检测用例)

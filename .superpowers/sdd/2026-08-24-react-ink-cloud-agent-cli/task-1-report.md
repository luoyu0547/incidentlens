# Task 1 报告：CLI 前置后端产品契约门禁修复

## 状态

已完成后端契约门禁修复；未修改 CLI 或 Web 产品实现。

## 改动

- 新增 `LogStreamEnvelope` 产品模型，放在 `apps/control-plane/src/incidentlens_control_plane/logs/views.py`。
  - 与 `streams/logs.py::envelope()` 的实际帧契约一致：`schema_version=1`、`event_type`、`occurred_at`，以及可选 `cursor` 和 JSON `payload`。
  - 控制帧（如 `log.subscribed`、`stream.heartbeat`、`stream.gap`、`stream.slow_consumer`）和日志帧（`log.record`）都由同一 envelope 表达。
- 修正 `scripts/export_product_contracts.py` 的 log-stream 导出候选，仅导出 `LogStreamEnvelope`，不再错误地将 `LogPage` 作为流协议 schema。
- 修正 `scripts/check_product_contracts.py` 的 secrecy 检查边界：OpenAPI 仅解析并检查响应 schema，允许请求 DTO 中合法的 `authentication_ref`，同时继续禁止响应中的敏感字段。
- 重新确定性导出并更新 checked-in contracts：
  - `packages/protocol/openapi/v1.json`
  - `packages/protocol/schema/cli-stream-v1.schema.json`
  - `packages/protocol/schema/log-stream-v1.schema.json`
- 补充契约回归测试，覆盖日志流 envelope 形状及请求认证引用允许、目标响应不暴露认证引用的语义。

## 测试命令与结果

- `.venv/bin/python scripts/check_product_contracts.py`
  - 通过：`product contracts OK (4 files, 35 operations)`
- `.venv/bin/python -m pytest tests/contracts tests/acceptance/test_product_api_foundation.py -q`
  - 通过：`8 passed, 1 warning`
- `.venv/bin/python -m pytest tests/contracts tests/acceptance/test_product_api_foundation.py tests/streams/test_cli_stream.py -q`
  - 通过：`12 passed, 1 warning`
- `.venv/bin/python -m pytest tests/api_v1/test_targets.py tests/logs tests/streams -q`
  - 通过：`81 passed, 1 warning`
- `.venv/bin/ruff check scripts/check_product_contracts.py scripts/export_product_contracts.py apps/control-plane/src/incidentlens_control_plane/logs/views.py tests/contracts/test_product_contracts.py`
  - 通过：`All checks passed!`
- `git diff --check`
  - 通过。

## 遗留 concern

- 测试环境现有 Starlette/httpx 兼容性弃用警告：`TestClient` 使用旧 httpx 适配路径；本任务未升级依赖或扩大范围。
## Fix round 2（独立审查后修复）

### 追加改动

- CLI stream schema 改为显式覆盖实际控制帧 `stream.hello`、`stream.heartbeat`、`stream.gap`、`stream.slow_consumer`，并通过 `RuntimeEventType` union 覆盖业务事件类型。
- CLI/log stream 的 `schema_version` 改为无默认值的必填字段，导出 schema 的 `required` 均包含 `schema_version`；缺失版本会被模型拒绝。
- secrecy 检查增强为解析 response-level `$ref`、component response/schema 引用及 `$ref` sibling；无法解析或出现循环/不支持引用时 fail-closed。
- 日志流 `log.record` 改为通过 `LogRecordView` allowlist 序列化，不再直接发送完整 `LogRecord.model_dump()`。
- `StreamEventEnvelope` 的业务事件构造补充显式 `schema_version=1`。
- 重新导出 checked-in contracts，并补充上述语义回归测试。

### Fix round 2 测试命令与结果

- `.venv/bin/python scripts/check_product_contracts.py`
  - 通过：`product contracts OK (4 files, 35 operations)`
- `.venv/bin/python -m pytest tests/contracts tests/acceptance/test_product_api_foundation.py tests/streams/test_cli_stream.py tests/logs -q`
  - 通过：`81 passed, 1 warning`
- `.venv/bin/ruff check apps/control-plane/src/incidentlens_control_plane/streams/types.py apps/control-plane/src/incidentlens_control_plane/streams/logs.py apps/control-plane/src/incidentlens_control_plane/api/routes/events.py scripts/check_product_contracts.py tests/contracts/test_product_contracts.py`
  - 通过：`All checks passed!`
- `git diff --check`
  - 通过。

### Fix round 2 concerns

- 仍有既有 Starlette/httpx `TestClient` 弃用警告，未在本轮升级依赖。
- CLI schema 的业务事件 union 当前由后端 `RuntimeEventType` 枚举导出；新增运行时事件类型需要通过后端模型和 deterministic exporter 重新生成 contract。

# Task 5 Report: Service Detail and Read-Only Relationships

## 改动

- 将 `apps/web/src/app/ServicePage.tsx` 从路由占位内容替换为由 `serviceQuery` 驱动的服务详情页。
- 新增服务头部、服务事实、关联问题、关联调查和稳定的 `LogViewer` 占位边界组件；详情只消费生成的 `ServiceDetailView` DTO。
- 展示服务/目标/主机、健康状态、容器、最近测试和观测时间，并为问题及调查提供只读链接。
- 待审批只显示“等待 CLI 中的操作者决策”；未添加批准、拒绝、重启、回滚、编辑或 shell 控件。
- 新增 `apps/web/tests/service.test.tsx`，覆盖安全事实、只读关系链接、CLI 决策提示、无变更控件和无伪造日志。
- 修复已有 query-key 序列化的 DTO 类型约束，并使服务查询在 jsdom 环境中不传递跨 realm 的 `AbortSignal`。

## 测试命令与输出

```text
npm run web:test -- service.test.tsx
✓ tests/service.test.tsx (4 tests) 1196ms
Test Files  1 passed (1)
Tests  4 passed (4)

npm run web:typecheck
> tsc --noEmit
```

## Concerns

- 运行测试前需要构建 protocol 包（`npm run build --workspace @incidentlens/protocol`），因为其导出指向未纳入版本控制的 `packages/protocol/dist`。
- 为避免 Task 5 引入假日志，日志区域仅是具有已声明 props 的稳定占位边界，未请求或渲染日志数据。

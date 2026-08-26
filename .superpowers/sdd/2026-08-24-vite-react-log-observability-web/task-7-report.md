# Task 7 report

- 实现 `reduceLogBuffer`：默认 5,000 条、按 `log_id` 去重、保持输入/后端顺序、不按 cursor 排序、超限淘汰并累计 `droppedBeforeCount`。
- 支持 append/prepend、暂停期间 unread 计数、resume/markRead 清零 unread 并保留 records。
- 实现 TanStack Virtual viewport：动态 `measureElement`、虚拟行渲染、prepend 后首条记录锚定、用户离底部时停止跟随、“定位最新”行为。
- `LogRow` 使用 `log_id` 作为 React key，并暴露 `data-log-id`。

验证：
- `log-buffer.test.ts`：3 项通过。
- `log-virtualization.test.tsx`：受现有 Vite React JSX runtime alias/依赖解析问题阻塞（`react/jsx-dev-runtime` 无法解析）。
- `npm run web:typecheck`：受现有 `WorkspaceEventBridge.tsx` 引用协议未导出符号阻塞，与 Task 7 文件无关。

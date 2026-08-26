# Task 6 report

## 状态
DONE

## Commit
`0d6e48c feat(web): query shareable historical logs`

## 实现
- 添加 URL 日志搜索归一化：默认 history、context 20、follow true；清理空值、校验 ISO 时间、去重 severity，并对倒序时间范围给出解释性错误。
- 添加历史日志 `useInfiniteQuery`：仅 GET 读取，分页使用后端 `next_cursor` 原样透传，保留后端顺序，不解析 cursor；过滤条件变化由 query key 隔离，AbortSignal 传入客户端。
- 添加最小 `LogViewer`、`LogToolbar`、`StreamStatus`，支持筛选、跟随状态、历史加载更多、错误保留筛选条件。
- 添加 log-search 与 log-history 测试。

## 测试
- `npm run web:typecheck`：通过。
- `npm run web:test -- log-search.test.ts log-history.test.tsx`：log-search 4 项通过；log-history 因本地协议 workspace 构建产物解析环境失败，未能执行。
- 已执行 `npm install`，产生的 `package-lock.json` 未纳入提交。

## Concerns
- 当前协议包需先构建/正确解析 workspace `@incidentlens/protocol` 后，才能运行 React history 测试；本任务未修改其他任务文件。
- `LogViewer` 的 URL 导航由调用方通过 `onSearchChange` 接入，组件本身不绑定具体 Router 路由，以避免侵入既有路由类型。

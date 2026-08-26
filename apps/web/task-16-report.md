# Task 16：浏览器端恢复场景

## 完成内容

- 将 Playwright `testDir` 修正为 `./e2e`。
- 为 Web workspace 增加 `@playwright/test` 开发依赖及锁文件记录。
- 新增确定性 HTTP/WebSocket fixture，覆盖 overview、service、issue、evidence、日志 cursor 页、只读方法捕获和 WebSocket 路由。
- 新增浏览器场景：总览到服务、URL 筛选与历史分页、流断线回补 c11–c15 / 重连 c15 与 c16 去重、gap resync、证据详情与浏览器返回、只读网络边界。

## 验证

- `npm install`：通过；npm 报告仓库现有 9 个 audit vulnerabilities（3 moderate、5 high、1 critical）。
- `npm run web:build`：TypeScript 阶段通过；Vite 阶段因 workspace 包 `@incidentlens/protocol` 的 package entry 无法解析而失败（`Failed to resolve entry for package "@incidentlens/protocol"`）。
- `npm run web:e2e`：未运行；production preview 依赖上述 build，且 Chromium 尚未安装。建议先修复 protocol workspace entry 后执行 `npx playwright install chromium && npm run web:e2e`。

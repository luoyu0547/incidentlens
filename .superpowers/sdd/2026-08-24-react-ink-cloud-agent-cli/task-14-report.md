# Task 14 Report

状态：DONE

- 合并 `feat/hard-cloud-incident`。
- 新增 node-pty PTY driver 与 deterministic fake control-plane（HTTP/WS、认证校验、取消调用记录、断线模拟）。
- 新增 startup、interaction、reconnect、approval 四组真实 built executable PTY 测试，覆盖尺寸调整、中文宽字符、输入/退出、重连去重、审批路径和输出 secret 扫描。
- 使用 `INCIDENTLENS_TOKEN` 时 CLI 采用环境 token store，避免 PTY 测试依赖本机 keyring；Authorization/token 不写入输出。

验证：
- `npm run build --workspace @incidentlens/cli` PASS
- `npm run test:pty --workspace @incidentlens/cli` PASS（4 files / 5 tests）

注意：首次本机安装 node-pty 时需执行其 native postinstall（npm install/rebuild），这是 node-pty 原生依赖要求。

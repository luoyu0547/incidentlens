# Task 3 报告：安全存储 Profile 与 Token

## 改动

- 保留既有 `ProfileConfig`、`ConfigStore`、`TokenStore` 接口及 Zod 校验。
- 强化 `FileConfigStore`：配置目录和文件使用 owner-only 权限（0700/0600），使用 sibling 临时文件、独占创建、文件 `fsync`、原子 rename，并同步父目录。
- 加载时只接受请求 profile 对应的文件，避免错误文件内容被作为其他 profile 返回。
- API URL 保存前移除用户名、密码、query、fragment，并规范化尾部路径斜线。
- Token 仍只通过 OS keyring 或只读 `INCIDENTLENS_TOKEN` 获取，配置 JSON 不保存明文 token；keyring 不可用时返回 `CredentialStoreUnavailable`，不回退明文。

## 测试

- `npm test --workspace @incidentlens/cli -- src/config/config-store.test.ts src/auth/token-store.test.ts`
  - 2 个测试文件通过，20 个测试通过。
- `npm run typecheck --workspace @incidentlens/cli`
  - 通过。
- `npm run lint`
  - 未通过：仓库现有 CLI/Web 文件存在 51 个 lint 错误（未改动文件为主）；本任务文件的 unused import 已修正，剩余错误不由本次改动引入。

## Concerns

- 当前仓库全量 lint 基线失败，主要集中在既有 CLI/Web 代码的 unused、`any`、声明合并及 Web ESLint project 配置问题；未扩大本任务范围修复。
- Task 3 的基础实现已存在于祖先提交 `366b9ed`；本提交补充其原子持久化和目录/文件权限强化。

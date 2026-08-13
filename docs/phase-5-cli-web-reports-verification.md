# Phase 5: CLI、Web UI、报告 — 验证记录

## 离线验证

```bash
# 运行 Phase 5 新增测试
uv run pytest tests/reports/ tests/cli/ tests/web/test_web_dashboard.py tests/acceptance/test_e2e_investigation.py -v

# 运行全量测试（确认不破坏已有功能）
uv run pytest -q

# Lint
uv run ruff check apps/control-plane/src/incidentlens_control_plane/reports/ apps/control-plane/src/incidentlens_control_plane/web/ apps/control-plane/src/incidentlens_control_plane/cli/
```

## Web UI 手动验证

```bash
uv run uvicorn incidentlens_control_plane.main:app --reload
# 浏览器打开 http://localhost:8000
```

- [ ] 仪表盘页面加载，显示调查列表
- [ ] 调查详情页显示时间线
- [ ] 审批页面显示待审批项
- [ ] 日志搜索页面可输入查询
- [ ] 项目页面显示已注册项目

## CLI 手动验证

```bash
incidentlens
```

- [ ] 仪表盘显示活跃调查
- [ ] 按 `a` 进入审批面板
- [ ] 按 `l` 进入日志浏览器
- [ ] 按 `q` 退出

## Docker 验收（需要 Docker）

```bash
cd infra/acceptance && docker compose up -d
INCIDENTLENS_RUN_ACCEPTANCE=1 uv run pytest tests/acceptance/test_docker_scenarios.py -v
```

## MVP 验收标准对照

| # | 标准 | 验证方式 | 状态 |
|---|---|---|---|
| 1 | 注册服务器和源码路径 | Web UI 项目页面 | ✅ |
| 2 | CLI 发起调查 + Web UI 实时查看 | CLI + Web UI | ✅ |
| 3 | 按服务查询日志 | Web UI 日志搜索 | ✅ |
| 4 | 查看错误/警告/正常日志 | 日志级别过滤 | ✅ |
| 5 | 父 Agent 创建子 Agent | 时间线展示 | ✅ |
| 6 | 持久 SSH 读取/编辑 | 工具调用可见 | ✅ |
| 7 | 变更前双重备份 | 变更面板 | ✅ |
| 8 | 阻止 rm -rf，审批 | 审批面板 | ✅ |
| 9 | 修改后验证 + 回滚 | 变更面板 | ✅ |
| 10 | 最终报告 | ReportService | ✅ |

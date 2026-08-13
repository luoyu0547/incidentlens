# Acceptance Test Environment

Docker Compose 模拟微服务环境，用于端到端验收测试。

## 启动

```bash
cd infra/acceptance
docker compose up -d
```

## 服务

| 服务 | 端口 | 说明 |
|---|---|---|
| api-gateway | 8080 | API 网关入口 |
| order-service | 5001 | 订单服务 |
| payment-service | 5002 | 支付服务 |
| inventory-service | 5003 | 库存服务 |
| postgres | 5432 | 数据库 |

## 故障注入

通过环境变量控制故障：

- `FAULT_DB_POOL=true` — order-service 数据库连接池耗尽
- `FAULT_PAYMENT_TIMEOUT=true` — 支付服务超时
- `FAULT_DEPENDENCY=true` — 下游服务不可用

修改 docker-compose.yml 中对应服务的 environment 后重启。

## 健康检查

```bash
curl http://localhost:8080/health
curl http://localhost:5001/health
```

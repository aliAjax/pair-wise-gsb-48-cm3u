# 证券结算与企业行动处理

纯Python标准库实现的证券结算与企业行动处理原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、净额结算、交收完整性、登记时点权益冻结和冲突检查。
- `src/repository.py`：SQLite建表、事务和查询。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算、失败场景和权益归属测试。

## 权益归属口径（结算指令 × 持仓 × 公司行动）

- **登记时点冻结**：每笔持仓变动（期初开仓/交收过户/人工调整）在 `holding_movements` 带生效时间，公司行动仅按 `effective_at <= record_date` 的变动汇总结算账户合格数量。
- **时点前交收才转移**：指令在登记时点前完成交收（settle 可带 `settled_at`），权益随过户转走。
- **未完成待核、不倒改**：登记时点仍未交收的指令进 `pending` 清单，权益留原持有人；时点后才交收的进 `late` 清单，已定稿权益不回改。
- **并发互斥**：公司行动计算/定稿均带 `expected_version` 乐观锁，同证券并发只放行一位，后到者收到 409。
- **失败重试不重发**：定稿可带 `idempotency_key`，重试沿用原冻结结果与已发权益。
- **持仓变动即失效**：交收过户或人工调整后，同证券未定稿草稿权益立即删除并抬升版本，必须重算；已定稿不受影响。
- **旧持仓补齐**：建公司行动时，同事务把 `recorded_at` 为空的旧持仓登记时点补成该证券首次公司行动的登记时点。

## 启动

```bash
python3 app.py --db ./data.db --port 8324
```

默认端口为`8324`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。交收动作 `settle` 的 data 可带 `settled_at`（ISO 8601）作为过户生效时间。
- `GET /api/holdings`：持仓列表，可带`instrument`、`account`、`limit`。
- `POST /api/holdings`：登记期初持仓，`{"data":{"account","instrument","quantity","recorded_at?"}}`；`recorded_at`留空表示旧持仓，由首次公司行动补齐。
- `GET /api/holdings/{id}`、`GET /api/holdings/{id}/audit`。
- `POST /api/holdings/{id}/adjust`：人工调整持仓，`{"expected_version":1,"data":{"delta":200,"reason":"..."}}`；立即失效同证券未定稿权益。
- `GET /api/corporate-actions`：公司行动列表，可带`status`。
- `POST /api/corporate-actions`：创建公司行动，`{"reference":"...","data":{"instrument","action_type":"split|dividend|merger","ratio","cash_rate","record_date"}}`。
- `GET /api/corporate-actions/{id}`、`GET /api/corporate-actions/{id}/audit`。
- `POST /api/corporate-actions/{id}/calculate`：按登记时点冻结合格数量并生成草稿权益，`{"expected_version":1}`。
- `POST /api/corporate-actions/{id}/finalize`：定稿发放，`{"expected_version":2,"idempotency_key":"可选"}`。
- `GET /api/corporate-actions/{id}/entitlements`：权益明细（含合格数量来源 `basis`）。

角色：`trader` 创建结算指令；`settlement_officer` 复核/交收/持仓维护；`corporate_actions` 公司行动与权益；`admin` 全权。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝、版本冲突、登记时点冻结/待核/迟办、并发定稿互斥、幂等重试、草稿失效重算和旧持仓补齐。

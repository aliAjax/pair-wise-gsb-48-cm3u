# 证券结算与企业行动处理

纯Python标准库实现的证券结算与企业行动处理原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、净额结算、交收完整性和公司行动调整和冲突检查。
- `src/repository.py`：SQLite建表、事务和查询。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算和失败场景测试。

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
- `POST /api/records`：创建结算指令，请求体为`{"reference":"...","data":{...}}`。指令可带`account_id`、`trade_at`、`settlement_due_at`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。交收动作可带`request_id`做幂等重试。
- `POST /api/positions`：初始持仓，`data`包含`instrument`、`account_id`、`quantity`、可选`recorded_at`；不传`recorded_at`表示旧持仓，首次公司行动创建时补齐。
- `GET /api/positions?instrument=...`：持仓列表。
- `POST /api/positions`且`{"adjust":true,"data":{...}}`：持仓调整，必须提供`delta`、`recorded_at`、`reason`、`request_id`。
- `POST /api/corporate-actions`：创建未定稿公司行动，`data`包含`instrument`、`action_type`（split/dividend/merger）、`ratio`、`record_time`、`currency`。
- `GET /api/corporate-actions`、`GET /api/corporate-actions/{id}`：公司行动列表和详情。
- `POST /api/corporate-actions/{id}/freeze`：按登记时点冻结合格数量，请求体包含`expected_version`和`data.request_id`。
- `POST /api/corporate-actions/{id}/issue`：发放冻结权益，请求体包含`expected_version`和`data.request_id`。
- `GET /api/corporate-actions/{id}/entitlements?status=...`：查看权益（provisional/frozen/issued/superseded）。
- `GET /api/corporate-actions/{id}/reviews?status=...`：查看登记时点跨期且待核的结算指令。

## 公司行动结算规则

- 交收在登记时点之前完成才写入持仓事件并参与权益；登记时点后完成的指令标记为`settled_after_record`，不转移、不倒改权益。
- 登记时点前未完成的指令进入待核；冻结和发放仅基于登记时点持仓。
- 权益在草案阶段为`provisional`，冻结后为`frozen`，发放后为`issued`；持仓数量变化会让草案权益变为`superseded`并重算。
- 公司行动和指令都使用`expected_version`做乐观并发控制；两个专员同时处理同一证券，只有一个请求能提交成功。
- 交收、持仓调整、冻结和发放使用调用方提供的`request_id`保持幂等；写入失败后用原请求重试不会重复入账或重复发放。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝和版本冲突。

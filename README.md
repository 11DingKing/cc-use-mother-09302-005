# 木偶教具库存履约

本项目维护木偶教具库存履约的领域约定、角色边界与样例数据，供后端服务、接口和自动化验证统一使用。当前契约覆盖器材管理员、活动老师、学校签收人，并明确套装库存、批次谱系、统一分录、差异对账等关键约束。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/prop_inventory/`：库存履约服务端（纯标准库，SQLite 持久化）。
- `tools/check_contract.py`：命令行摘要检查。
- `tools/run_server.py`：启动服务端。
- `tools/demo_flow.py`：端到端履约流程演示。
- `tests/`：契约完整性回归测试与服务端业务回归测试。

## 服务端

以批次和单件两种粒度记录套装组成，覆盖预约占用、分批出库、现场签收、归还差异的完整履约链路。所有状态推进都写入追加式统一分录（哈希链），可核对、可续账、可逐件追溯去向与责任环节。

### 启动

```bash
python3 tools/run_server.py --port 8000 --db data/inventory.db
# 或：PYTHONPATH=src python3 -m prop_inventory --host 127.0.0.1 --port 8000
```

默认使用内存库；`--db` 指定 SQLite 文件即可持久化（WAL 模式，支持并发读写）。

### 设计要点

- **一次性锁定**：场次确认在单个 `BEGIN IMMEDIATE` 事务内完成“校验 + 锁定”，库存不足则整体回滚，全有或全无；并发确认由写事务串行化，不会超锁或重复占用。
- **统一分录**：每件物资的每次状态推进（登记/入套/锁定/出库/签收/归还/隔离/替换/对账修复）都与状态变更同事务落一条分录；分录以哈希链串联，篡改或漏记在对账时暴露。
- **中途失败可续账**：写操作要么整体提交要么整体回滚；`POST /api/reconcile` 重放分录推导应有状态并对照账面，`repair=true` 按分录修复漂移（修复同样落分录）。
- **幂等**：所有写接口支持 `Idempotency-Key` 请求头，重复提交回放首个响应不重复落账；签收、报损天然幂等（重复签收跳过、重复报损返回 `duplicate`）。
- **去向还原**：`GET /api/items/{id}/trace` 给出批次谱系、当前位置（仓库/在途/到校/隔离区/去向不明）与逐条分录（动作、状态迁移、责任人、角色、关联单据）。

### API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/batches` | 登记批次与单件（`parent_batch_id` 记录批次谱系） |
| GET | `/api/batches/{id}` | 批次详情：单件清单 + 谱系链 |
| POST | `/api/templates` | 套装模板（批次粒度组成：品类 + 数量） |
| GET | `/api/templates/{id}` | 模板详情与已组装套装 |
| POST | `/api/kits` | 组装套装实例（单件粒度，组成须与模板一致） |
| GET | `/api/kits/{id}` | 套装详情与齐套检查 |
| POST | `/api/kits/{id}/replace` | 临时替换组件（旧件按状况离场，新件接管场次绑定） |
| POST | `/api/sessions` | 创建场次（需求：模板 + 数量） |
| GET | `/api/sessions` / `/api/sessions/{id}` | 场次列表 / 详情（锁定、出库、签收、归还、差异、未结清单） |
| POST | `/api/sessions/{id}/confirm` | 确认场次：一次性锁定足量库存（全有或全无） |
| POST | `/api/sessions/{id}/cancel` | 取消场次：释放全部占用 |
| POST | `/api/sessions/{id}/shipments` | 分批出库（整包 `kit_ids` / 散件 `item_ids` / 临时增补 `extra_item_ids`） |
| GET | `/api/shipments/{id}` | 出库单详情 |
| POST | `/api/shipments/{id}/sign` | 现场签收（逐件幂等，支持部分签收与损坏签收） |
| POST | `/api/sessions/{id}/returns` | 登记归还（部分归还、损坏入隔离、`missing` 登记缺失差异） |
| POST | `/api/sessions/{id}/close` | 结案（未清算物资阻止结案；未出库占用自动释放） |
| POST | `/api/items/{id}/damage` | 损坏隔离（已出库保持场次绑定，未出库解除绑定） |
| POST | `/api/items/{id}/resolve` | 隔离件处置：修复回库 / 报废 |
| GET | `/api/items` | 单件检索（按状态/场次/品类过滤，含位置） |
| GET | `/api/items/{id}/trace` | 单件全程追溯：批次谱系、位置、去向与责任链 |
| GET | `/api/stock` | 库存总览（按状态/品类，逐模板可锁定套装数与散件余量） |
| GET | `/api/ledger` | 统一分录查询（按物资/场次/动作过滤） |
| POST | `/api/reconcile` | 对账：校验哈希链 + 重放推导对照，`repair=true` 修复漂移 |

错误统一为 `{"ok": false, "error": {"code", "message", "details"}}`，HTTP 状态码语义：400 参数错误、404 不存在、409 状态/库存冲突、422 业务规则不满足。

### 演示

```bash
python3 tools/demo_flow.py
```

完整走一遍：批次登记（含谱系）→ 组装套装 → 场次确认锁定 → 库存不足拒绝 → 分批出库 → 重复签收 → 到校损坏隔离 → 增补出库与成员替换 → 部分归还与缺失差异 → 结案 → 追溯与对账。

## 验证

测试命令：`python3 -m unittest discover -s tests -v`

编译命令：`python3 -m compileall -q src tools tests`

命令行检查：`python3 tools/check_contract.py domain/contract.json`

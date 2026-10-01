# 木偶教具库存履约

本项目维护木偶教具库存履约的领域约定、角色边界与样例数据，供后端服务、接口和自动化验证统一使用。当前契约覆盖器材管理员、活动老师、学校签收人，并明确套装库存、批次谱系、统一分录、差异对账等关键约束。

`src/puppet_fulfillment/` 在此契约上实现了**完整服务端**：以批次和单件两种粒度记录套装组成、预约占用、分批出库、现场签收与归还差异，所有库存事实都由可核对的库存分录推进。

## 解决的问题

- 到校才发现少关键部件 → 套装组成按种类/数量锁定，签收逐件核验，缺失即登记差异并锁定责任环节。
- 取消场次占用的幕布仍显示不可借 → 取消即在同一事务内把仍在“占用”桶的物资释放回库；已出库/在场的须先完成归还对账。
- 仓库不知道东西在哪 → 每件实物都有读模型定位（状态/批次/场次/学校）与完整分录轨迹，可逐笔还原去向和责任环节。

## 核心设计

| 契约不变量 | 落地方式 |
| --- | --- |
| 套装库存 | 场次确认在**一个事务**内按全部组成一次性锁定，任一种类不足则整场回滚，绝不产生半锁定 |
| 批次谱系 | `batches → items`；入库即逐件建档（编码 `批次号-序号`），批次粒度操作也自动落到具体实物 |
| 统一分录 | 每次动作写一条 `events` + 若干有符号 `ledger_entries`（离桶 −1 / 入桶 +1）；单件账与批次汇总账由同一动作一次写就，`GET /reconcile` 交叉核对 |
| 差异对账 | 损坏隔离、丢失、临时替换均产生差异分录，可后续核销（报废/修复入库/赔偿），中途失败可用幂等键重试，账始终守恒 |

库存桶状态：`备货 → 占用 → 出库 → 使用 → 归还 → 备货`，旁路桶 `损坏 / 丢失 / 已报废`。

并发安全：每个写事务 `BEGIN IMMEDIATE` 立即持有写锁；数据库对“单件 × 活动锁定分录”建有部分唯一索引
（`ux_active_item_allocation`），并发借用同一实物必有一方失败，且不会出现双重占用。出库单的有效签收同样有唯一索引
（`ux_one_valid_receipt`）兜底重复签收。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/puppet_fulfillment/`：
  - `schema.sql` 表结构与约束/触发器；`database.py` 连接与事务；
  - `repository.py` 统一分录写入、余额与对账、单件轨迹；
  - `service.py` 领域服务（锁定/出库/签收/归还/隔离/替换/核销/对账）；
  - `api.py`、`__main__.py`：标准库 HTTP 接口与启动入口。
- `tools/check_contract.py`：契约命令行检查。
- `tools/demo_scenario.py`：完整业务场景演示（含逐步对账与单件轨迹还原）。
- `tests/`：契约回归、领域端到端、HTTP 接口、并发借用测试。

## 运行

仅依赖 Python 3.11+ 标准库（SQLite），无需安装第三方包。

```bash
# 启动服务（默认 puppet_fulfillment.db，端口 8080）
PYTHONPATH=src python3 -m puppet_fulfillment ./data/fulfillment.db 8080

# 端到端场景演示
PYTHONPATH=src python3 tools/demo_scenario.py
```

## HTTP 接口（JSON）

写请求可带 `Idempotency-Key` 头实现失败安全重试。

| 方法 & 路径 | 说明 |
| --- | --- |
| `POST /staff` `/kinds` `/sets` `/batches` | 人员、物资种类（声明 批次/单件 粒度）、套装组成、批次入库 |
| `POST /shows` | 建场次 |
| `POST /shows/{id}/reservations` | 预约套装（组成在此时快照） |
| `POST /shows/{id}/confirm` | **一次性锁定足量库存**，不足返回 `409 insufficient_stock` |
| `POST /shows/{id}/cancel` | 取消并释放仍占用的库存（已出库/在场须先对账） |
| `POST /shows/{id}/substitute` | 原物隔离后的临时替换 |
| `POST /outbounds` | 建出库单（可带分批 `plan`） |
| `POST /outbounds/{id}/ship` | 分批出库，body `{"item_codes":[...]}` |
| `POST /outbounds/{id}/receive` | 现场签收；到场损坏 `condition:"损坏"` 直接隔离，重复签收返回 `409 duplicate_receipt` |
| `POST /returns` | 部分/全部归还，`condition` 为 完好/损坏/缺失，可多次 |
| `POST /items/{code}/damage` | 使用中或在库报损隔离 |
| `POST /substitutions/{id}/swap-back` | 替换件撤回（完好入库/损坏/缺失） |
| `POST /discrepancies/{id}/resolve` | 差异核销：报废 / 修复入库 / 核销 |
| `GET /items/{code}` | 物资定位（状态、批次、场次、学校、责任环节） |
| `GET /items/{code}/history` | 沿单件分录还原完整去向 |
| `GET /shows/{id}` | 场次组成与各锁定分露出入库/差异汇总 |
| `GET /discrepancies?status=待处理` | 差异台账 |
| `GET /reconcile` | 全库统一分录对账（守恒、桶非负、双粒度一致、读模型一致） |

## 验证

```bash
# 全部测试：契约 + 领域端到端 + HTTP + 并发
PYTHONPATH=src python3 -m unittest discover -s tests -v

# 编译检查
python3 -m compileall -q src tools tests

# 契约命令行检查
python3 tools/check_contract.py domain/contract.json
```

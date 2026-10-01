-- 木偶教具库存履约：库存分录核心表结构
-- 设计要点：
--   1. 套装组成以“物资种类”为单位，种类自带 批次/单件 两种跟踪粒度。
--   2. 每个实物单位都有 item 行（批次谱系完整）；批次粒度的批量操作由系统
--      按批次自动落到具体 item，保证分录始终可逐件核对。
--   3. allocation_units = 锁定分录（预约 × 批次 × 种类 × 数量），
--      allocation_items 给出该分录覆盖的具体实物。
--   4. events + ledger_entries 是唯一的库存事实来源（统一分录、差异对账）。

PRAGMA foreign_keys = ON;

-- 人员与角色 -------------------------------------------------------------
CREATE TABLE IF NOT EXISTS staff (
    staff_id   TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    role       TEXT NOT NULL CHECK (role IN ('器材管理员','活动老师','学校签收人')),
    created_at TEXT NOT NULL
);

-- 物资种类（粒度在此声明）------------------------------------------------
CREATE TABLE IF NOT EXISTS item_kinds (
    kind_code   TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    unit        TEXT NOT NULL DEFAULT '件',
    tracking    TEXT NOT NULL DEFAULT '单件' CHECK (tracking IN ('批次','单件'))
);

-- 套装与组成（批次和单件两种粒度的组成行可以共存）--------------------------
CREATE TABLE IF NOT EXISTS sets (
    set_code   TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    version    INTEGER NOT NULL DEFAULT 1,
    active     INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS set_components (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    set_code  TEXT NOT NULL REFERENCES sets(set_code),
    kind_code TEXT NOT NULL REFERENCES item_kinds(kind_code),
    quantity  INTEGER NOT NULL CHECK (quantity > 0),
    UNIQUE (set_code, kind_code)
);

-- 批次与实物 -------------------------------------------------------------
CREATE TABLE IF NOT EXISTS batches (
    batch_no    TEXT PRIMARY KEY,
    kind_code   TEXT NOT NULL REFERENCES item_kinds(kind_code),
    total_qty   INTEGER NOT NULL CHECK (total_qty >= 0),
    supplier    TEXT,
    received_at TEXT NOT NULL,
    note        TEXT
);

CREATE TABLE IF NOT EXISTS items (
    item_code      TEXT PRIMARY KEY,
    kind_code      TEXT NOT NULL REFERENCES item_kinds(kind_code),
    batch_no       TEXT NOT NULL REFERENCES batches(batch_no),
    seq            INTEGER NOT NULL,
    current_status TEXT NOT NULL DEFAULT '备货' CHECK (current_status IN (
        '备货','占用','出库','使用','归还','损坏','丢失','已报废'
    )),
    current_holder TEXT,          -- 当前所在场次/学校
    quarantine     INTEGER NOT NULL DEFAULT 0,
    UNIQUE (batch_no, seq)
);

-- 场次与预约 -------------------------------------------------------------
CREATE TABLE IF NOT EXISTS shows (
    show_id      TEXT PRIMARY KEY,
    school       TEXT NOT NULL,
    scheduled_at TEXT,
    status       TEXT NOT NULL DEFAULT '草稿' CHECK (status IN (
        '草稿','已锁定','部分出库','已出库','已签收','部分归还','已完成','已取消'
    )),
    created_by   TEXT,
    locked_at    TEXT,
    cancelled_at TEXT,
    note         TEXT
);

CREATE TABLE IF NOT EXISTS reservations (
    reservation_id TEXT PRIMARY KEY,
    show_id        TEXT NOT NULL REFERENCES shows(show_id),
    set_code       TEXT NOT NULL REFERENCES sets(set_code),
    set_units      INTEGER NOT NULL CHECK (set_units > 0),
    status         TEXT NOT NULL DEFAULT '计划中' CHECK (status IN (
        '计划中','占用中','已出库','已归还','已取消'
    )),
    created_at     TEXT NOT NULL,
    released_at    TEXT
);

CREATE TABLE IF NOT EXISTS reservation_components (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    reservation_id TEXT NOT NULL REFERENCES reservations(reservation_id),
    kind_code      TEXT NOT NULL,
    qty_required   INTEGER NOT NULL,
    qty_locked     INTEGER NOT NULL DEFAULT 0,
    qty_out        INTEGER NOT NULL DEFAULT 0,
    qty_received   INTEGER NOT NULL DEFAULT 0,
    qty_returned   INTEGER NOT NULL DEFAULT 0,   -- 完好归还
    qty_damaged    INTEGER NOT NULL DEFAULT 0,
    qty_lost       INTEGER NOT NULL DEFAULT 0,
    UNIQUE (reservation_id, kind_code)
);

-- 锁定分录：一次场次锁定按批次拆成若干分录行 ------------------------------
CREATE TABLE IF NOT EXISTS allocation_units (
    unit_id        TEXT PRIMARY KEY,
    reservation_id TEXT NOT NULL REFERENCES reservations(reservation_id),
    show_id        TEXT NOT NULL REFERENCES shows(show_id),
    batch_no       TEXT NOT NULL REFERENCES batches(batch_no),
    kind_code      TEXT NOT NULL,
    qty            INTEGER NOT NULL CHECK (qty > 0),
    qty_out        INTEGER NOT NULL DEFAULT 0,
    qty_received   INTEGER NOT NULL DEFAULT 0,
    qty_returned   INTEGER NOT NULL DEFAULT 0,
    qty_damaged    INTEGER NOT NULL DEFAULT 0,
    qty_lost       INTEGER NOT NULL DEFAULT 0,
    status         TEXT NOT NULL DEFAULT '占用' CHECK (status IN (
        '占用','出库','使用','归还','差异','已取消'
    )),
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS allocation_items (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    unit_id   TEXT NOT NULL REFERENCES allocation_units(unit_id),
    item_code TEXT NOT NULL REFERENCES items(item_code),
    state     TEXT NOT NULL DEFAULT '占用' CHECK (state IN (
        '占用','出库','使用','归还','损坏','丢失','已取消'
    )),
    active    INTEGER NOT NULL DEFAULT 1,   -- 该件仍被此业务占用（在途/在场）时为 1
    UNIQUE (unit_id, item_code)
);

-- 同一实物同一时刻只能属于一个活动分录：并发双重借用在数据库层被拒绝
CREATE UNIQUE INDEX IF NOT EXISTS ux_active_item_allocation
    ON allocation_items(item_code) WHERE active = 1;

CREATE TRIGGER IF NOT EXISTS trg_ai_after_insert AFTER INSERT ON allocation_items
BEGIN
    UPDATE allocation_items
       SET active = CASE WHEN NEW.state IN ('占用','出库','使用') THEN 1 ELSE 0 END
     WHERE id = NEW.id;
END;

CREATE TRIGGER IF NOT EXISTS trg_ai_after_state AFTER UPDATE OF state ON allocation_items
BEGIN
    UPDATE allocation_items
       SET active = CASE WHEN NEW.state IN ('占用','出库','使用') THEN 1 ELSE 0 END
     WHERE id = NEW.id;
END;

-- 出库单 / 签收 / 归还 ----------------------------------------------------
CREATE TABLE IF NOT EXISTS outbound_orders (
    outbound_id TEXT PRIMARY KEY,
    show_id     TEXT NOT NULL REFERENCES shows(show_id),
    status      TEXT NOT NULL DEFAULT '待出库' CHECK (status IN (
        '待出库','部分出库','已出库','已签收','部分归还','已归还','已关闭'
    )),
    created_by  TEXT,
    created_at  TEXT NOT NULL,
    shipped_at  TEXT,
    received_at TEXT,
    closed_at   TEXT
);

CREATE TABLE IF NOT EXISTS outbound_lines (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    outbound_id TEXT NOT NULL REFERENCES outbound_orders(outbound_id),
    unit_id     TEXT NOT NULL REFERENCES allocation_units(unit_id),
    qty         INTEGER NOT NULL,
    shipped_qty INTEGER NOT NULL DEFAULT 0,
    UNIQUE (outbound_id, unit_id)
);

CREATE TABLE IF NOT EXISTS receipts (
    receipt_no  TEXT PRIMARY KEY,
    outbound_id TEXT NOT NULL REFERENCES outbound_orders(outbound_id),
    receiver_id TEXT REFERENCES staff(staff_id),
    signed_at   TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT '有效' CHECK (status IN ('有效','已冲销')),
    note        TEXT
);
-- 一张出库单只允许一次有效签收：重复签收在数据库层被拒绝
CREATE UNIQUE INDEX IF NOT EXISTS ux_one_valid_receipt
    ON receipts(outbound_id) WHERE status = '有效';

CREATE TABLE IF NOT EXISTS receipt_lines (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    receipt_no   TEXT NOT NULL REFERENCES receipts(receipt_no),
    unit_id      TEXT NOT NULL,
    item_code    TEXT NOT NULL,
    accepted     INTEGER NOT NULL DEFAULT 1,
    condition_on_arrival TEXT NOT NULL DEFAULT '完好'
        CHECK (condition_on_arrival IN ('完好','损坏')),
    UNIQUE (receipt_no, item_code)
);

CREATE TABLE IF NOT EXISTS returns (
    return_no   TEXT PRIMARY KEY,
    outbound_id TEXT NOT NULL REFERENCES outbound_orders(outbound_id),
    returned_at TEXT NOT NULL,
    handler_id  TEXT REFERENCES staff(staff_id),
    note        TEXT
);

CREATE TABLE IF NOT EXISTS return_lines (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    return_no TEXT NOT NULL REFERENCES returns(return_no),
    unit_id   TEXT NOT NULL,
    item_code TEXT,                       -- 缺失行没有实物
    condition TEXT NOT NULL CHECK (condition IN ('完好','损坏','缺失')),
    qty       INTEGER NOT NULL DEFAULT 1,
    UNIQUE (return_no, unit_id, item_code)
);

-- 统一分录 ---------------------------------------------------------------
-- 事件：业务动作的一次性、原子记录
CREATE TABLE IF NOT EXISTS events (
    event_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,             -- 入库/场次锁定/释放占用/出库/签收/归还/损坏隔离/丢失/临时替换/换回/核销
    stage      TEXT NOT NULL,             -- 责任环节：备货/占用/出库/使用/归还
    occurred_at TEXT NOT NULL,
    actor_id   TEXT REFERENCES staff(staff_id),
    show_id    TEXT,
    reservation_id TEXT,
    outbound_id TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}'
);

-- 库存分录：每行表示某实物在某个状态桶上的数量增减（+1 进入 / -1 离开）
CREATE TABLE IF NOT EXISTS ledger_entries (
    entry_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id   INTEGER NOT NULL REFERENCES events(event_id),
    scope      TEXT NOT NULL CHECK (scope IN ('批次','单件')),
    batch_no   TEXT NOT NULL,
    item_code  TEXT,
    kind_code  TEXT NOT NULL,
    show_id    TEXT,
    reservation_id TEXT,
    unit_id    TEXT,
    stage      TEXT NOT NULL,
    bucket     TEXT NOT NULL,             -- 备货/占用/出库/使用/归还/损坏/丢失/已报废
    amount_qty INTEGER NOT NULL,          -- 有符号数量
    note       TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_ledger_item ON ledger_entries(item_code);
CREATE INDEX IF NOT EXISTS ix_ledger_batch ON ledger_entries(batch_no);
CREATE INDEX IF NOT EXISTS ix_ledger_show ON ledger_entries(show_id);

-- 差异登记 ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS discrepancies (
    discrepancy_id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind           TEXT NOT NULL CHECK (kind IN ('损坏','丢失','多还')),
    status         TEXT NOT NULL DEFAULT '待处理'
        CHECK (status IN ('待处理','处理中','已核销')),
    scope          TEXT NOT NULL CHECK (scope IN ('批次','单件')),
    item_code      TEXT,
    batch_no       TEXT,
    kind_code      TEXT NOT NULL,
    qty            INTEGER NOT NULL DEFAULT 1,
    show_id        TEXT,
    reservation_id TEXT,
    unit_id        TEXT,
    responsible_stage TEXT NOT NULL,      -- 发现差异时物资所处环节
    opened_event_id INTEGER NOT NULL REFERENCES events(event_id),
    closed_event_id INTEGER REFERENCES events(event_id),
    opened_at  TEXT NOT NULL,
    closed_at  TEXT,
    resolution TEXT
);

-- 临时替换 ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS substitutions (
    substitution_id INTEGER PRIMARY KEY AUTOINCREMENT,
    show_id         TEXT NOT NULL,
    reservation_id  TEXT,
    original_item_code TEXT NOT NULL,
    replacement_item_code TEXT NOT NULL,
    status  TEXT NOT NULL DEFAULT '替换在用'
        CHECK (status IN ('替换在用','已换回','已核销')),
    opened_event_id INTEGER NOT NULL,
    closed_event_id INTEGER,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    note TEXT
);

-- 幂等键：中途失败后可用同一键安全重试，结果可核对 -------------------------
CREATE TABLE IF NOT EXISTS idempotency (
    idem_key    TEXT PRIMARY KEY,
    request_hash TEXT NOT NULL,
    event_id    INTEGER,
    result_json TEXT,
    created_at  TEXT NOT NULL
);

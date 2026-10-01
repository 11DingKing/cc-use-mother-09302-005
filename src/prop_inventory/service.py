"""库存履约领域服务。

设计要点：

- 每次写操作都在单个 ``BEGIN IMMEDIATE`` 事务内完成状态变更与统一分录追加，
  要么全部提交，要么整体回滚，中途失败不会留下半截状态；
- 场次确认在同一事务内“校验可用库存 + 一次性锁定”，全有或全无；
- 统一分录（entries）是追加式哈希链，reconcile 可重放对账并修复漂移，
  中途失败后能够继续对账；
- 每件物资的去向（状态→位置）与责任环节（actor/role）都落在分录上，
  trace_item 可逐件还原。
"""
from __future__ import annotations

import json

from .db import Database
from .ledger import append_entry, derive_statuses, new_id, now_iso, verify_chain
from .models import (
    ACTORS,
    AVAILABLE_STATUSES,
    CONDITION_DAMAGED,
    CONDITION_GOOD,
    CONDITIONS,
    IN_USE,
    ITEM_LOCATIONS,
    LOST,
    OPEN_BINDING_STATUSES,
    QUARANTINED,
    RESERVED,
    RETURNED,
    ROLE_ADMIN,
    ROLE_SIGNER,
    SCRAPPED,
    SHIPPED,
    STOCK,
    DomainError,
    bad_request,
    conflict,
    not_found,
    unprocessable,
)

# ---- 场次状态 ----
SESSION_DRAFT = "待确认"
SESSION_CONFIRMED = "已确认"
SESSION_SHIPPING = "出库中"
SESSION_IN_USE = "使用中"
SESSION_RETURNING = "归还中"
SESSION_CLOSED = "已完结"
SESSION_CANCELLED = "已取消"

# ---- 预约状态 ----
RESERVATION_ACTIVE = "生效"
RESERVATION_RELEASED = "已释放"
RESERVATION_FULFILLED = "已完结"

# ---- 出库单状态 ----
SHIPMENT_IN_TRANSIT = "在途"
SHIPMENT_PARTIAL = "部分签收"
SHIPMENT_SIGNED = "已签收"

#: 归还/结案时仍挂在场次上、需要清算的状态
_OUTSTANDING_STATUSES = (RESERVED, SHIPPED, IN_USE, QUARANTINED)


class InventoryService:
    """木偶教具库存履约的领域操作集合。"""

    def __init__(self, db: Database):
        self.db = db

    # ------------------------------------------------------------------
    # 基础工具
    # ------------------------------------------------------------------

    @staticmethod
    def _check_role(role: str) -> None:
        if role not in ACTORS:
            raise bad_request("unknown_role", f"未知角色：{role}（应为：{'、'.join(ACTORS)}）")

    @staticmethod
    def _get(conn, sql: str, params: tuple, what: str):
        row = conn.execute(sql, params).fetchone()
        if row is None:
            raise not_found(what, params[0] if params else "")
        return row

    def _session(self, conn, session_id: str):
        return self._get(conn, "SELECT * FROM sessions WHERE session_id=?", (session_id,), "场次")

    def _item(self, conn, item_id: str):
        return self._get(conn, "SELECT * FROM items WHERE item_id=?", (item_id,), "物资")

    def _kit(self, conn, kit_id: str):
        return self._get(conn, "SELECT * FROM kits WHERE kit_id=?", (kit_id,), "套装")

    @staticmethod
    def _alloc(conn, table: str, column: str, prefix: str) -> str:
        for _ in range(8):
            candidate = new_id(prefix)
            if conn.execute(f"SELECT 1 FROM {table} WHERE {column}=?", (candidate,)).fetchone() is None:
                return candidate
        raise DomainError("id_exhausted", "无法分配唯一编号", 500)

    def _transition(self, conn, item_id: str, *, to: str, action: str, actor: str, role: str,
                    bind="keep", entry_session: str | None = None, ref_type: str | None = None,
                    ref_id: str | None = None, detail: dict | None = None, expect: tuple | None = None):
        """单件物资状态推进：更新 items 行并追加统一分录（同事务）。

        ``bind``：``"keep"`` 保持场次绑定，``"clear"`` 解除绑定，其余值视为新的场次 id。
        """
        item = self._item(conn, item_id)
        if expect is not None and item["status"] not in expect:
            raise conflict(
                "item_state",
                f"物资 {item_id} 当前状态为「{item['status']}」，不能执行 {action}",
                {"item_id": item_id, "status": item["status"], "action": action},
            )
        if bind == "keep":
            new_binding = item["session_id"]
        elif bind == "clear":
            new_binding = None
        else:
            new_binding = bind
        conn.execute(
            "UPDATE items SET status=?, session_id=? WHERE item_id=?",
            (to, new_binding, item_id),
        )
        session_for_entry = entry_session if entry_session is not None else (item["session_id"] or new_binding)
        entry = append_entry(
            conn, item_id=item_id, action=action, from_status=item["status"], to_status=to,
            actor=actor, role=role, session_id=session_for_entry,
            ref_type=ref_type, ref_id=ref_id, detail=detail or {},
        )
        return item, entry

    # ------------------------------------------------------------------
    # 视图组装（内部，复用同一连接）
    # ------------------------------------------------------------------

    @staticmethod
    def _item_view(conn, row) -> dict:
        kit = conn.execute("SELECT kit_id FROM kit_members WHERE item_id=?", (row["item_id"],)).fetchone()
        return {
            "item_id": row["item_id"],
            "batch_id": row["batch_id"],
            "category": row["category"],
            "label": row["label"],
            "status": row["status"],
            "location": ITEM_LOCATIONS[row["status"]],
            "session_id": row["session_id"],
            "kit_id": kit["kit_id"] if kit else None,
        }

    def _kit_members(self, conn, kit_id: str) -> list:
        return conn.execute(
            """SELECT i.item_id, i.category, i.label, i.status, i.session_id
               FROM kit_members km JOIN items i ON i.item_id = km.item_id
               WHERE km.kit_id = ? ORDER BY i.item_id""",
            (kit_id,),
        ).fetchall()

    def _kit_health(self, conn, kit_id: str) -> dict:
        """齐套检查：按模板逐品类核对在位且未隔离/丢失/报废的成员。"""
        kit = self._kit(conn, kit_id)
        parts = conn.execute(
            "SELECT category, quantity FROM set_template_parts WHERE template_id=? ORDER BY category",
            (kit["template_id"],),
        ).fetchall()
        members = self._kit_members(conn, kit_id)
        shortages = []
        for part in parts:
            same = [m for m in members if m["category"] == part["category"]]
            good = [m for m in same if m["status"] not in (QUARANTINED, LOST, SCRAPPED)]
            if len(good) < part["quantity"]:
                shortages.append({
                    "category": part["category"],
                    "required": part["quantity"],
                    "available": len(good),
                    "unavailable_items": [
                        {"item_id": m["item_id"], "status": m["status"]}
                        for m in same if m["status"] in (QUARANTINED, LOST, SCRAPPED)
                    ],
                })
        return {"healthy": not shortages, "shortages": shortages}

    @staticmethod
    def _kit_derived_status(members) -> str:
        statuses = {m["status"] for m in members}
        if IN_USE in statuses:
            return "使用"
        if SHIPPED in statuses:
            return "出库"
        if RESERVED in statuses:
            return "占用"
        return "可用"

    def _kit_view(self, conn, kit_id: str) -> dict:
        kit = self._kit(conn, kit_id)
        members = self._kit_members(conn, kit_id)
        reservation = conn.execute(
            "SELECT reservation_id, session_id, status FROM reservations WHERE kit_id=? AND status=?",
            (kit_id, RESERVATION_ACTIVE),
        ).fetchone()
        return {
            "kit_id": kit["kit_id"],
            "template_id": kit["template_id"],
            "name": kit["name"],
            "status": self._kit_derived_status(members),
            "health": self._kit_health(conn, kit_id),
            "reservation": dict(reservation) if reservation else None,
            "members": [
                {
                    "item_id": m["item_id"],
                    "category": m["category"],
                    "label": m["label"],
                    "status": m["status"],
                    "location": ITEM_LOCATIONS[m["status"]],
                    "session_id": m["session_id"],
                }
                for m in members
            ],
        }

    def _shipment_view(self, conn, shipment_id: str) -> dict:
        shp = self._get(conn, "SELECT * FROM shipments WHERE shipment_id=?", (shipment_id,), "出库单")
        items = conn.execute(
            """SELECT si.item_id, si.signed, si.signed_by, si.signed_at, i.category, i.label, i.status
               FROM shipment_items si JOIN items i ON i.item_id = si.item_id
               WHERE si.shipment_id=? ORDER BY si.item_id""",
            (shipment_id,),
        ).fetchall()
        return {
            "shipment_id": shp["shipment_id"],
            "session_id": shp["session_id"],
            "seq": shp["seq"],
            "status": shp["status"],
            "shipped_by": shp["shipped_by"],
            "shipped_at": shp["shipped_at"],
            "items": [
                {
                    "item_id": it["item_id"],
                    "category": it["category"],
                    "label": it["label"],
                    "status": it["status"],
                    "signed": bool(it["signed"]),
                    "signed_by": it["signed_by"],
                    "signed_at": it["signed_at"],
                }
                for it in items
            ],
        }

    def _session_view(self, conn, session_id: str) -> dict:
        s = self._session(conn, session_id)
        requirements = conn.execute(
            """SELECT r.template_id, t.name AS template_name, r.quantity
               FROM session_requirements r JOIN set_templates t ON t.template_id = r.template_id
               WHERE r.session_id=? ORDER BY r.template_id""",
            (session_id,),
        ).fetchall()
        reservations = conn.execute(
            """SELECT r.reservation_id, r.kit_id, k.name AS kit_name, r.status, r.created_at
               FROM reservations r JOIN kits k ON k.kit_id = r.kit_id
               WHERE r.session_id=? ORDER BY r.created_at, r.reservation_id""",
            (session_id,),
        ).fetchall()
        active_kit_ids = [r["kit_id"] for r in reservations if r["status"] == RESERVATION_ACTIVE]
        kits = [self._kit_view(conn, kit_id) for kit_id in active_kit_ids]
        shipments = conn.execute(
            "SELECT shipment_id FROM shipments WHERE session_id=? ORDER BY seq", (session_id,)
        ).fetchall()
        returns = conn.execute(
            "SELECT * FROM returns WHERE session_id=? ORDER BY received_at, return_id", (session_id,)
        ).fetchall()
        return_views = []
        for ret in returns:
            items = conn.execute(
                """SELECT ri.item_id, ri.condition, i.category, i.label, i.status
                   FROM return_items ri JOIN items i ON i.item_id = ri.item_id
                   WHERE ri.return_id=? ORDER BY ri.item_id""",
                (ret["return_id"],),
            ).fetchall()
            return_views.append({
                "return_id": ret["return_id"],
                "received_by": ret["received_by"],
                "received_at": ret["received_at"],
                "items": [dict(it) for it in items],
            })
        # 绑定在场次上但未随套的物资（临时增补、被替换离场但仍需清算的）
        loose = conn.execute(
            """SELECT * FROM items i
               WHERE i.session_id=?
                 AND NOT EXISTS (SELECT 1 FROM kit_members km WHERE km.item_id = i.item_id)
               ORDER BY i.item_id""",
            (session_id,),
        ).fetchall()
        outstanding = conn.execute(
            f"""SELECT * FROM items WHERE session_id=?
                AND status IN ({','.join('?' * len(_OUTSTANDING_STATUSES))}) ORDER BY item_id""",
            (session_id, *_OUTSTANDING_STATUSES),
        ).fetchall()
        missing = conn.execute(
            """SELECT ri.item_id, i.label, i.category, r.return_id, r.received_at
               FROM return_items ri JOIN returns r ON r.return_id = ri.return_id
               JOIN items i ON i.item_id = ri.item_id
               WHERE r.session_id=? AND ri.condition='缺失' ORDER BY ri.item_id""",
            (session_id,),
        ).fetchall()
        damaged = conn.execute(
            """SELECT ri.item_id, i.label, i.category, r.return_id, r.received_at
               FROM return_items ri JOIN returns r ON r.return_id = ri.return_id
               JOIN items i ON i.item_id = ri.item_id
               WHERE r.session_id=? AND ri.condition='损坏' ORDER BY ri.item_id""",
            (session_id,),
        ).fetchall()
        returned_good = conn.execute(
            """SELECT COUNT(*) AS n FROM return_items ri JOIN returns r ON r.return_id = ri.return_id
               WHERE r.session_id=? AND ri.condition='完好'""",
            (session_id,),
        ).fetchone()["n"]
        return {
            "session_id": s["session_id"],
            "school": s["school"],
            "teacher": s["teacher"],
            "planned_at": s["planned_at"],
            "status": s["status"],
            "created_at": s["created_at"],
            "requirements": [dict(r) for r in requirements],
            "reservations": [dict(r) for r in reservations],
            "kits": kits,
            "loose_items": [self._item_view(conn, row) for row in loose],
            "shipments": [self._shipment_view(conn, row["shipment_id"]) for row in shipments],
            "returns": return_views,
            "outstanding": [
                {"item_id": row["item_id"], "status": row["status"], "location": ITEM_LOCATIONS[row["status"]]}
                for row in outstanding
            ],
            "discrepancies": {
                "missing": [dict(r) for r in missing],
                "damaged": [dict(r) for r in damaged],
            },
            "summary": {
                "active_reservations": len(active_kit_ids),
                "outstanding": len(outstanding),
                "returned_good": returned_good,
                "damaged": len(damaged),
                "missing": len(missing),
            },
        }

    # ------------------------------------------------------------------
    # 批次与单件（批次粒度建档）
    # ------------------------------------------------------------------

    def register_batch(self, *, name, items, actor, role=ROLE_ADMIN, source="",
                       parent_batch_id=None, note="", batch_id=None):
        """登记批次及其单件物资；parent_batch_id 记录批次谱系。"""
        self._check_role(role)
        if not name or not str(name).strip():
            raise bad_request("invalid_batch", "批次名称不能为空")
        if not items:
            raise bad_request("invalid_batch", "批次必须包含至少一件物资")
        with self.db.write_tx() as conn:
            if parent_batch_id is not None:
                self._get(conn, "SELECT batch_id FROM batches WHERE batch_id=?", (parent_batch_id,), "父批次")
            bid = batch_id or self._alloc(conn, "batches", "batch_id", "BAT")
            if conn.execute("SELECT 1 FROM batches WHERE batch_id=?", (bid,)).fetchone():
                raise conflict("duplicate_batch", f"批次已存在：{bid}")
            now = now_iso()
            conn.execute(
                "INSERT INTO batches(batch_id, name, source, parent_batch_id, note, created_by, created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (bid, name, source, parent_batch_id, note, actor, now),
            )
            created, seen = [], set()
            for spec in items:
                item_id = spec.get("item_id") or self._alloc(conn, "items", "item_id", "ITM")
                if item_id in seen:
                    raise bad_request("duplicate_item", f"批次内单件编号重复：{item_id}")
                seen.add(item_id)
                category = (spec.get("category") or "").strip()
                label = (spec.get("label") or "").strip()
                if not category or not label:
                    raise bad_request("invalid_item", "每件物资必须提供 category 与 label")
                if conn.execute("SELECT 1 FROM items WHERE item_id=?", (item_id,)).fetchone():
                    raise conflict("duplicate_item", f"物资编号已存在：{item_id}")
                conn.execute(
                    "INSERT INTO items(item_id, batch_id, category, label, status, session_id, created_at)"
                    " VALUES (?,?,?,?,?,NULL,?)",
                    (item_id, bid, category, label, STOCK, now),
                )
                append_entry(
                    conn, item_id=item_id, action="register", from_status=None, to_status=STOCK,
                    actor=actor, role=role, ref_type="batch", ref_id=bid,
                    detail={"batch": bid, "category": category, "label": label},
                )
                created.append({"item_id": item_id, "category": category, "label": label, "status": STOCK})
            return {
                "batch_id": bid, "name": name, "source": source,
                "parent_batch_id": parent_batch_id, "item_count": len(created), "items": created,
            }

    def get_batch(self, batch_id: str) -> dict:
        with self.db.read_tx() as conn:
            batch = self._get(conn, "SELECT * FROM batches WHERE batch_id=?", (batch_id,), "批次")
            items = conn.execute(
                "SELECT * FROM items WHERE batch_id=? ORDER BY item_id", (batch_id,)
            ).fetchall()
            lineage, seen, current = [], {batch_id}, batch["parent_batch_id"]
            while current and current not in seen:
                seen.add(current)
                row = conn.execute("SELECT * FROM batches WHERE batch_id=?", (current,)).fetchone()
                if row is None:
                    break
                lineage.append({
                    "batch_id": row["batch_id"], "name": row["name"],
                    "source": row["source"], "created_at": row["created_at"],
                })
                current = row["parent_batch_id"]
            return {
                "batch_id": batch["batch_id"],
                "name": batch["name"],
                "source": batch["source"],
                "parent_batch_id": batch["parent_batch_id"],
                "note": batch["note"],
                "created_by": batch["created_by"],
                "created_at": batch["created_at"],
                "lineage": lineage,
                "items": [self._item_view(conn, row) for row in items],
            }

    # ------------------------------------------------------------------
    # 套装模板与实例（批次粒度组成 → 单件粒度组装）
    # ------------------------------------------------------------------

    def create_template(self, *, name, parts, template_id=None):
        if not name or not str(name).strip():
            raise bad_request("invalid_template", "模板名称不能为空")
        if not parts:
            raise bad_request("invalid_template", "模板必须包含组成部件")
        normalized = {}
        for part in parts:
            category = (part.get("category") or "").strip()
            quantity = part.get("quantity")
            if not category:
                raise bad_request("invalid_template", "部件缺少 category")
            if not isinstance(quantity, int) or quantity <= 0:
                raise bad_request("invalid_template", f"部件 {category} 的 quantity 必须是正整数")
            if category in normalized:
                raise bad_request("invalid_template", f"部件品类重复：{category}")
            normalized[category] = quantity
        with self.db.write_tx() as conn:
            tid = template_id or self._alloc(conn, "set_templates", "template_id", "TPL")
            if conn.execute("SELECT 1 FROM set_templates WHERE template_id=?", (tid,)).fetchone():
                raise conflict("duplicate_template", f"模板已存在：{tid}")
            conn.execute(
                "INSERT INTO set_templates(template_id, name, created_at) VALUES (?,?,?)",
                (tid, name, now_iso()),
            )
            for category, quantity in normalized.items():
                conn.execute(
                    "INSERT INTO set_template_parts(template_id, category, quantity) VALUES (?,?,?)",
                    (tid, category, quantity),
                )
            return {"template_id": tid, "name": name, "parts": [
                {"category": c, "quantity": q} for c, q in sorted(normalized.items())
            ]}

    def get_template(self, template_id: str) -> dict:
        with self.db.read_tx() as conn:
            tpl = self._get(conn, "SELECT * FROM set_templates WHERE template_id=?", (template_id,), "套装模板")
            parts = conn.execute(
                "SELECT category, quantity FROM set_template_parts WHERE template_id=? ORDER BY category",
                (template_id,),
            ).fetchall()
            kits = conn.execute(
                "SELECT kit_id, name FROM kits WHERE template_id=? ORDER BY kit_id", (template_id,)
            ).fetchall()
            return {
                "template_id": tpl["template_id"],
                "name": tpl["name"],
                "parts": [dict(p) for p in parts],
                "kits": [dict(k) for k in kits],
            }

    def assemble_kit(self, *, template_id, name, item_ids, actor, role=ROLE_ADMIN, kit_id=None):
        """按模板组装套装实例（单件粒度），组成必须与模板完全一致。"""
        self._check_role(role)
        if not item_ids:
            raise bad_request("invalid_kit", "套装必须包含单件")
        if len(set(item_ids)) != len(item_ids):
            raise bad_request("invalid_kit", "单件编号重复")
        with self.db.write_tx() as conn:
            tpl = self._get(conn, "SELECT * FROM set_templates WHERE template_id=?", (template_id,), "套装模板")
            parts = {
                row["category"]: row["quantity"]
                for row in conn.execute(
                    "SELECT category, quantity FROM set_template_parts WHERE template_id=?", (template_id,)
                )
            }
            items = [self._item(conn, iid) for iid in item_ids]
            for item in items:
                if item["status"] not in AVAILABLE_STATUSES or item["session_id"] is not None:
                    raise conflict(
                        "item_unavailable",
                        f"物资 {item['item_id']} 当前状态为「{item['status']}」，不能入套",
                        {"item_id": item["item_id"], "status": item["status"]},
                    )
                if conn.execute("SELECT 1 FROM kit_members WHERE item_id=?", (item["item_id"],)).fetchone():
                    raise conflict("already_in_kit", f"物资 {item['item_id']} 已在其他套装中", {"item_id": item["item_id"]})
            provided: dict[str, int] = {}
            for item in items:
                provided[item["category"]] = provided.get(item["category"], 0) + 1
            if provided != parts:
                raise unprocessable(
                    "composition_mismatch",
                    "套装组成与模板不符",
                    {"required": parts, "provided": provided},
                )
            kid = kit_id or self._alloc(conn, "kits", "kit_id", "KIT")
            if conn.execute("SELECT 1 FROM kits WHERE kit_id=?", (kid,)).fetchone():
                raise conflict("duplicate_kit", f"套装已存在：{kid}")
            conn.execute(
                "INSERT INTO kits(kit_id, template_id, name, created_at) VALUES (?,?,?,?)",
                (kid, template_id, name, now_iso()),
            )
            for item in items:
                conn.execute(
                    "INSERT INTO kit_members(kit_id, item_id, category) VALUES (?,?,?)",
                    (kid, item["item_id"], item["category"]),
                )
                append_entry(
                    conn, item_id=item["item_id"], action="assemble",
                    from_status=item["status"], to_status=item["status"],
                    actor=actor, role=role, ref_type="kit", ref_id=kid,
                    detail={"kit_id": kid, "kit_name": name, "template_id": template_id},
                )
            return self._kit_view(conn, kid)

    def get_kit(self, kit_id: str) -> dict:
        with self.db.read_tx() as conn:
            return self._kit_view(conn, kit_id)

    def replace_kit_item(self, kit_id, *, old_item_id, new_item_id, old_condition,
                         reason="", actor, role=ROLE_ADMIN):
        """临时替换套装组件：旧件按状况离场，新件接管其场次绑定（如有）。"""
        self._check_role(role)
        if old_condition not in CONDITIONS:
            raise bad_request("invalid_condition", f"old_condition 应为：{'、'.join(CONDITIONS)}")
        if old_item_id == new_item_id:
            raise bad_request("invalid_replace", "新旧单件不能相同")
        with self.db.write_tx() as conn:
            self._kit(conn, kit_id)
            membership = conn.execute(
                "SELECT * FROM kit_members WHERE kit_id=? AND item_id=?", (kit_id, old_item_id)
            ).fetchone()
            if membership is None:
                raise not_found("套装组件", f"{kit_id}/{old_item_id}")
            old = self._item(conn, old_item_id)
            new = self._item(conn, new_item_id)
            if new["category"] != membership["category"]:
                raise unprocessable(
                    "category_mismatch",
                    f"替换件品类 {new['category']} 与套件槽位 {membership['category']} 不符",
                    {"slot_category": membership["category"], "new_category": new["category"]},
                )
            if conn.execute("SELECT 1 FROM kit_members WHERE item_id=?", (new_item_id,)).fetchone():
                raise conflict("already_in_kit", f"物资 {new_item_id} 已在其他套装中", {"item_id": new_item_id})
            reservation = conn.execute(
                "SELECT * FROM reservations WHERE kit_id=? AND status=?", (kit_id, RESERVATION_ACTIVE)
            ).fetchone()
            session_id = reservation["session_id"] if reservation else None
            # 替换件准入：未绑定的在库件（锁定后随套出库），或已绑定本场次的增补件（在途替换）
            already_bound = (
                session_id is not None
                and new["session_id"] == session_id
                and new["status"] in OPEN_BINDING_STATUSES
            )
            if not already_bound and (new["status"] not in AVAILABLE_STATUSES or new["session_id"] is not None):
                raise conflict(
                    "item_unavailable",
                    f"替换件 {new_item_id} 当前状态为「{new['status']}」，不能入套",
                    {"item_id": new_item_id, "status": new["status"]},
                )
            # 旧件是否随本场次出过库：出过库的保持绑定，随归还流程清算
            shipped_before = session_id and conn.execute(
                """SELECT 1 FROM shipment_items si
                   JOIN shipments sh ON sh.shipment_id = si.shipment_id
                   WHERE si.item_id=? AND sh.session_id=? LIMIT 1""",
                (old_item_id, session_id),
            ).fetchone()
            keep_binding = bool(shipped_before) and old["status"] in OPEN_BINDING_STATUSES + (QUARANTINED,)
            if old["status"] in (QUARANTINED, LOST, SCRAPPED):
                old_to = old["status"]  # 终态不覆盖，后续走 resolve 流程
            elif old_condition == CONDITION_DAMAGED:
                old_to = QUARANTINED
            else:
                old_to = old["status"] if keep_binding else STOCK
            self._transition(
                conn, old_item_id, to=old_to, action="replace_out", actor=actor, role=role,
                bind="keep" if keep_binding else "clear", entry_session=session_id,
                ref_type="kit", ref_id=kit_id,
                detail={"reason": reason, "condition": old_condition, "replacement": new_item_id},
            )
            conn.execute("DELETE FROM kit_members WHERE kit_id=? AND item_id=?", (kit_id, old_item_id))
            conn.execute(
                "INSERT INTO kit_members(kit_id, item_id, category) VALUES (?,?,?)",
                (kit_id, new_item_id, membership["category"]),
            )
            if already_bound:
                # 增补件已在场次流程中（占用/出库/使用），保持其状态与绑定
                self._transition(
                    conn, new_item_id, to=new["status"], action="replace_in", actor=actor, role=role,
                    bind="keep", entry_session=session_id, ref_type="kit", ref_id=kit_id,
                    detail={"replaces": old_item_id},
                )
                new_status = new["status"]
            elif session_id:
                self._transition(
                    conn, new_item_id, to=RESERVED, action="replace_in", actor=actor, role=role,
                    bind=session_id, entry_session=session_id, ref_type="kit", ref_id=kit_id,
                    detail={"replaces": old_item_id},
                )
                new_status = RESERVED
            else:
                self._transition(
                    conn, new_item_id, to=new["status"], action="replace_in", actor=actor, role=role,
                    bind="keep", ref_type="kit", ref_id=kit_id,
                    detail={"replaces": old_item_id},
                )
                new_status = new["status"]
            return {
                "kit_id": kit_id,
                "session_id": session_id,
                "old_item": {"item_id": old_item_id, "status": old_to, "kept_binding": keep_binding},
                "new_item": {"item_id": new_item_id, "status": new_status},
                "health": self._kit_health(conn, kit_id),
            }

    # ------------------------------------------------------------------
    # 场次：创建、一次性锁定、取消
    # ------------------------------------------------------------------

    def create_session(self, *, school, teacher, planned_at, requirements, session_id=None):
        for field, value in (("school", school), ("teacher", teacher), ("planned_at", planned_at)):
            if not value or not str(value).strip():
                raise bad_request("invalid_session", f"场次缺少字段：{field}")
        if not requirements:
            raise bad_request("invalid_session", "场次必须包含套装需求")
        with self.db.write_tx() as conn:
            sid = session_id or self._alloc(conn, "sessions", "session_id", "SES")
            if conn.execute("SELECT 1 FROM sessions WHERE session_id=?", (sid,)).fetchone():
                raise conflict("duplicate_session", f"场次已存在：{sid}")
            seen = set()
            for req in requirements:
                tid = req.get("template_id")
                quantity = req.get("quantity")
                self._get(conn, "SELECT template_id FROM set_templates WHERE template_id=?", (tid,), "套装模板")
                if not isinstance(quantity, int) or quantity <= 0:
                    raise bad_request("invalid_session", f"模板 {tid} 的 quantity 必须是正整数")
                if tid in seen:
                    raise bad_request("invalid_session", f"需求中模板重复：{tid}")
                seen.add(tid)
            conn.execute(
                "INSERT INTO sessions(session_id, school, teacher, planned_at, status, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (sid, school, teacher, planned_at, SESSION_DRAFT, now_iso()),
            )
            for req in requirements:
                conn.execute(
                    "INSERT INTO session_requirements(session_id, template_id, quantity) VALUES (?,?,?)",
                    (sid, req["template_id"], req["quantity"]),
                )
            return self._session_view(conn, sid)

    def list_sessions(self) -> dict:
        with self.db.read_tx() as conn:
            rows = conn.execute(
                "SELECT session_id, school, teacher, planned_at, status, created_at FROM sessions"
                " ORDER BY created_at, session_id"
            ).fetchall()
            return {"sessions": [dict(r) for r in rows]}

    def get_session(self, session_id: str) -> dict:
        with self.db.read_tx() as conn:
            return self._session_view(conn, session_id)

    def _available_kits(self, conn, template_id: str) -> list[str]:
        """可锁定的套装：全部成员在库（备货/归还）且未绑定任何场次。"""
        rows = conn.execute(
            """SELECT k.kit_id FROM kits k
               WHERE k.template_id = ?
                 AND EXISTS (SELECT 1 FROM kit_members km0 WHERE km0.kit_id = k.kit_id)
                 AND NOT EXISTS (
                       SELECT 1 FROM kit_members km JOIN items i ON i.item_id = km.item_id
                       WHERE km.kit_id = k.kit_id
                         AND (i.status NOT IN ('备货','归还') OR i.session_id IS NOT NULL))
               ORDER BY k.kit_id""",
            (template_id,),
        ).fetchall()
        return [r["kit_id"] for r in rows]

    def confirm_session(self, session_id, *, actor, role=ROLE_ADMIN):
        """确认场次：同一事务内校验并一次性锁定足量库存，全有或全无。"""
        self._check_role(role)
        with self.db.write_tx() as conn:
            s = self._session(conn, session_id)
            if s["status"] != SESSION_DRAFT:
                raise conflict(
                    "session_state",
                    f"场次状态为「{s['status']}」，不能确认",
                    {"session_id": session_id, "status": s["status"]},
                )
            requirements = conn.execute(
                """SELECT r.template_id, t.name AS template_name, r.quantity
                   FROM session_requirements r JOIN set_templates t ON t.template_id = r.template_id
                   WHERE r.session_id=? ORDER BY r.template_id""",
                (session_id,),
            ).fetchall()
            plan, shortages, chosen = [], [], set()
            for req in requirements:
                candidates = [k for k in self._available_kits(conn, req["template_id"]) if k not in chosen]
                picked = candidates[: req["quantity"]]
                if len(picked) < req["quantity"]:
                    shortages.append({
                        "template_id": req["template_id"],
                        "template_name": req["template_name"],
                        "required": req["quantity"],
                        "available": len(candidates),
                    })
                else:
                    chosen.update(picked)
                    plan.append((req, picked))
            if shortages:
                raise conflict(
                    "insufficient_stock",
                    "库存不足，场次确认失败（未锁定任何物资）",
                    {"shortages": shortages},
                )
            locked = []
            for req, kit_ids in plan:
                for kit_id in kit_ids:
                    reservation_id = self._alloc(conn, "reservations", "reservation_id", "RSV")
                    conn.execute(
                        "INSERT INTO reservations(reservation_id, session_id, kit_id, status, created_at)"
                        " VALUES (?,?,?,?,?)",
                        (reservation_id, session_id, kit_id, RESERVATION_ACTIVE, now_iso()),
                    )
                    members = self._kit_members(conn, kit_id)
                    for member in members:
                        self._transition(
                            conn, member["item_id"], to=RESERVED, action="reserve",
                            actor=actor, role=role, bind=session_id, entry_session=session_id,
                            ref_type="reservation", ref_id=reservation_id,
                            detail={"kit_id": kit_id, "template_id": req["template_id"]},
                            expect=AVAILABLE_STATUSES,
                        )
                    locked.append({
                        "kit_id": kit_id,
                        "template_id": req["template_id"],
                        "reservation_id": reservation_id,
                        "items": [m["item_id"] for m in members],
                    })
            conn.execute(
                "UPDATE sessions SET status=? WHERE session_id=?", (SESSION_CONFIRMED, session_id)
            )
            return {
                "session_id": session_id,
                "status": SESSION_CONFIRMED,
                "locked_kits": locked,
                "locked_items": sum(len(k["items"]) for k in locked),
            }

    def cancel_session(self, session_id, *, actor, role=ROLE_ADMIN):
        """取消场次：释放全部占用，物资回到可预约状态。"""
        self._check_role(role)
        with self.db.write_tx() as conn:
            s = self._session(conn, session_id)
            if s["status"] not in (SESSION_DRAFT, SESSION_CONFIRMED):
                raise conflict(
                    "session_state",
                    f"场次状态为「{s['status']}」，不能取消",
                    {"session_id": session_id, "status": s["status"]},
                )
            if conn.execute("SELECT 1 FROM shipments WHERE session_id=? LIMIT 1", (session_id,)).fetchone():
                raise conflict("session_shipped", "场次已出库，不能取消，请走归还流程", {"session_id": session_id})
            released = []
            bound = conn.execute(
                "SELECT item_id FROM items WHERE session_id=? AND status=? ORDER BY item_id",
                (session_id, RESERVED),
            ).fetchall()
            for row in bound:
                self._transition(
                    conn, row["item_id"], to=STOCK, action="release", actor=actor, role=role,
                    bind="clear", entry_session=session_id,
                    detail={"reason": "场次取消，释放占用"},
                )
                released.append(row["item_id"])
            conn.execute(
                "UPDATE reservations SET status=? WHERE session_id=? AND status=?",
                (RESERVATION_RELEASED, session_id, RESERVATION_ACTIVE),
            )
            conn.execute(
                "UPDATE sessions SET status=? WHERE session_id=?", (SESSION_CANCELLED, session_id)
            )
            return {
                "session_id": session_id,
                "status": SESSION_CANCELLED,
                "released_items": released,
            }

    # ------------------------------------------------------------------
    # 分批出库与现场签收
    # ------------------------------------------------------------------

    def create_shipment(self, session_id, *, actor, role=ROLE_ADMIN,
                        kit_ids=(), item_ids=(), extra_item_ids=()):
        """分批出库：整包（kit_ids）、散件（item_ids）或临时增补（extra_item_ids）。"""
        self._check_role(role)
        with self.db.write_tx() as conn:
            s = self._session(conn, session_id)
            if s["status"] not in (SESSION_CONFIRMED, SESSION_SHIPPING, SESSION_IN_USE):
                raise conflict(
                    "session_state",
                    f"场次状态为「{s['status']}」，不能出库",
                    {"session_id": session_id, "status": s["status"]},
                )
            targets: list[tuple[str, bool]] = []
            seen: set[str] = set()

            def add(iid: str, extra: bool) -> None:
                if iid in seen:
                    raise bad_request("duplicate_item", f"出库单内单件重复：{iid}")
                seen.add(iid)
                targets.append((iid, extra))

            for kit_id in kit_ids or ():
                self._kit(conn, kit_id)
                reservation = conn.execute(
                    "SELECT 1 FROM reservations WHERE kit_id=? AND session_id=? AND status=?",
                    (kit_id, session_id, RESERVATION_ACTIVE),
                ).fetchone()
                if reservation is None:
                    raise conflict("kit_not_reserved", f"套装 {kit_id} 未锁定给本场次", {"kit_id": kit_id})
                members = self._kit_members(conn, kit_id)
                blocked = [
                    {"item_id": m["item_id"], "status": m["status"]}
                    for m in members if not (m["status"] == RESERVED and m["session_id"] == session_id)
                ]
                if blocked:
                    raise conflict(
                        "kit_not_ready",
                        f"套装 {kit_id} 存在不可出库组件",
                        {"kit_id": kit_id, "blocked": blocked},
                    )
                for member in members:
                    add(member["item_id"], False)
            for iid in item_ids or ():
                item = self._item(conn, iid)
                if not (item["status"] == RESERVED and item["session_id"] == session_id):
                    raise conflict(
                        "item_not_reserved",
                        f"物资 {iid} 未被本场次锁定（当前状态「{item['status']}」）",
                        {"item_id": iid, "status": item["status"]},
                    )
                add(iid, False)
            for iid in extra_item_ids or ():
                item = self._item(conn, iid)
                if item["status"] not in AVAILABLE_STATUSES or item["session_id"] is not None:
                    raise conflict(
                        "item_unavailable",
                        f"增补件 {iid} 当前状态为「{item['status']}」，不能出库",
                        {"item_id": iid, "status": item["status"]},
                    )
                add(iid, True)
            if not targets:
                raise bad_request("empty_shipment", "出库单不能为空")
            shipment_id = self._alloc(conn, "shipments", "shipment_id", "SHP")
            seq = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS seq FROM shipments WHERE session_id=?", (session_id,)
            ).fetchone()["seq"]
            conn.execute(
                "INSERT INTO shipments(shipment_id, session_id, seq, status, shipped_by, shipped_at)"
                " VALUES (?,?,?,?,?,?)",
                (shipment_id, session_id, seq, SHIPMENT_IN_TRANSIT, actor, now_iso()),
            )
            for iid, extra in targets:
                if extra:
                    self._transition(
                        conn, iid, to=RESERVED, action="reserve", actor=actor, role=role,
                        bind=session_id, entry_session=session_id,
                        ref_type="shipment", ref_id=shipment_id,
                        detail={"extra": True, "reason": "临时增补出库"},
                    )
                self._transition(
                    conn, iid, to=SHIPPED, action="ship", actor=actor, role=role,
                    bind="keep", entry_session=session_id,
                    ref_type="shipment", ref_id=shipment_id,
                    detail={"seq": seq, "extra": extra},
                )
                conn.execute(
                    "INSERT INTO shipment_items(shipment_id, item_id, signed) VALUES (?,?,0)",
                    (shipment_id, iid),
                )
            conn.execute(
                "UPDATE sessions SET status=? WHERE session_id=?", (SESSION_SHIPPING, session_id)
            )
            return self._shipment_view(conn, shipment_id)

    def get_shipment(self, shipment_id: str) -> dict:
        with self.db.read_tx() as conn:
            return self._shipment_view(conn, shipment_id)

    @staticmethod
    def _normalize_condition_items(items) -> list[dict]:
        normalized = []
        for spec in items:
            if isinstance(spec, str):
                normalized.append({"item_id": spec, "condition": CONDITION_GOOD})
            elif isinstance(spec, dict) and spec.get("item_id"):
                condition = spec.get("condition", CONDITION_GOOD)
                if condition not in CONDITIONS:
                    raise bad_request("invalid_condition", f"condition 应为：{'、'.join(CONDITIONS)}")
                normalized.append({"item_id": spec["item_id"], "condition": condition})
            else:
                raise bad_request("invalid_item_spec", "单件条目应为编号字符串或 {item_id, condition} 对象")
        return normalized

    def sign_shipment(self, shipment_id, *, signed_by, items=None, role=ROLE_SIGNER):
        """现场签收：逐件落账；重复签收幂等跳过，不产生重复分录。"""
        self._check_role(role)
        if not signed_by or not str(signed_by).strip():
            raise bad_request("invalid_sign", "签收人不能为空")
        with self.db.write_tx() as conn:
            shp = self._get(conn, "SELECT * FROM shipments WHERE shipment_id=?", (shipment_id,), "出库单")
            session_id = shp["session_id"]
            remaining_before = conn.execute(
                "SELECT COUNT(*) AS n FROM shipment_items WHERE shipment_id=? AND signed=0",
                (shipment_id,),
            ).fetchone()["n"]
            if items is None:
                targets = [
                    {"item_id": row["item_id"], "condition": CONDITION_GOOD}
                    for row in conn.execute(
                        "SELECT item_id FROM shipment_items WHERE shipment_id=? AND signed=0 ORDER BY item_id",
                        (shipment_id,),
                    )
                ]
            else:
                targets = self._normalize_condition_items(items)
            signed_now, already_signed = [], []
            for target in targets:
                iid, condition = target["item_id"], target["condition"]
                si = conn.execute(
                    "SELECT * FROM shipment_items WHERE shipment_id=? AND item_id=?", (shipment_id, iid)
                ).fetchone()
                if si is None:
                    raise bad_request("not_in_shipment", f"物资 {iid} 不在出库单 {shipment_id} 中")
                if si["signed"]:
                    already_signed.append(iid)  # 重复签收：幂等跳过
                    continue
                item = self._item(conn, iid)
                if item["status"] != SHIPPED:
                    raise conflict(
                        "item_state",
                        f"物资 {iid} 当前状态为「{item['status']}」，不能签收",
                        {"item_id": iid, "status": item["status"]},
                    )
                to = IN_USE if condition == CONDITION_GOOD else QUARANTINED
                self._transition(
                    conn, iid, to=to,
                    action="sign" if condition == CONDITION_GOOD else "sign_damage",
                    actor=signed_by, role=role, bind="keep", entry_session=session_id,
                    ref_type="shipment", ref_id=shipment_id,
                    detail={"condition": condition},
                )
                conn.execute(
                    "UPDATE shipment_items SET signed=1, signed_by=?, signed_at=?"
                    " WHERE shipment_id=? AND item_id=?",
                    (signed_by, now_iso(), shipment_id, iid),
                )
                signed_now.append({"item_id": iid, "condition": condition, "status": to})
            remaining = conn.execute(
                "SELECT COUNT(*) AS n FROM shipment_items WHERE shipment_id=? AND signed=0",
                (shipment_id,),
            ).fetchone()["n"]
            new_status = SHIPMENT_SIGNED if remaining == 0 else SHIPMENT_PARTIAL
            conn.execute("UPDATE shipments SET status=? WHERE shipment_id=?", (new_status, shipment_id))
            unsigned_total = conn.execute(
                """SELECT COUNT(*) AS n FROM shipment_items si
                   JOIN shipments sh ON sh.shipment_id = si.shipment_id
                   WHERE sh.session_id=? AND si.signed=0""",
                (session_id,),
            ).fetchone()["n"]
            if unsigned_total == 0:
                conn.execute(
                    "UPDATE sessions SET status=? WHERE session_id=? AND status=?",
                    (SESSION_IN_USE, session_id, SESSION_SHIPPING),
                )
            return {
                "shipment_id": shipment_id,
                "session_id": session_id,
                "status": new_status,
                "signed": signed_now,
                "already_signed": already_signed,
                "duplicate": not signed_now and remaining_before == 0,
            }

    # ------------------------------------------------------------------
    # 归还、差异与结案
    # ------------------------------------------------------------------

    def register_return(self, session_id, *, received_by, items=(), missing=(), role=ROLE_ADMIN):
        """登记归还：支持部分归还；损坏件入隔离，missing 登记为缺失差异。"""
        self._check_role(role)
        if not received_by or not str(received_by).strip():
            raise bad_request("invalid_return", "接收人不能为空")
        returned_specs = self._normalize_condition_items(items or ())
        missing_ids = list(missing or ())
        if not returned_specs and not missing_ids:
            raise bad_request("empty_return", "归还单不能为空")
        with self.db.write_tx() as conn:
            s = self._session(conn, session_id)
            if s["status"] not in (SESSION_SHIPPING, SESSION_IN_USE, SESSION_RETURNING):
                raise conflict(
                    "session_state",
                    f"场次状态为「{s['status']}」，不能登记归还",
                    {"session_id": session_id, "status": s["status"]},
                )
            return_id = self._alloc(conn, "returns", "return_id", "RET")
            conn.execute(
                "INSERT INTO returns(return_id, session_id, received_by, received_at) VALUES (?,?,?,?)",
                (return_id, session_id, received_by, now_iso()),
            )
            returned, missed = [], []
            for spec in returned_specs:
                iid, condition = spec["item_id"], spec["condition"]
                item = self._item(conn, iid)
                if item["session_id"] != session_id:
                    raise conflict("not_bound", f"物资 {iid} 未绑定本场次", {"item_id": iid})
                if item["status"] not in (SHIPPED, IN_USE, QUARANTINED):
                    raise conflict(
                        "item_state",
                        f"物资 {iid} 当前状态为「{item['status']}」，不能归还",
                        {"item_id": iid, "status": item["status"]},
                    )
                to = RETURNED if condition == CONDITION_GOOD else QUARANTINED
                self._transition(
                    conn, iid, to=to,
                    action="return_good" if condition == CONDITION_GOOD else "return_damaged",
                    actor=received_by, role=role, bind="clear", entry_session=session_id,
                    ref_type="return", ref_id=return_id,
                    detail={"condition": condition},
                )
                conn.execute(
                    "INSERT INTO return_items(return_id, item_id, condition) VALUES (?,?,?)",
                    (return_id, iid, condition),
                )
                returned.append({"item_id": iid, "condition": condition, "status": to})
            for iid in missing_ids:
                item = self._item(conn, iid)
                if item["session_id"] != session_id:
                    raise conflict("not_bound", f"物资 {iid} 未绑定本场次", {"item_id": iid})
                if item["status"] not in (SHIPPED, IN_USE, QUARANTINED):
                    raise conflict(
                        "item_state",
                        f"物资 {iid} 当前状态为「{item['status']}」，不能登记缺失",
                        {"item_id": iid, "status": item["status"]},
                    )
                self._transition(
                    conn, iid, to=LOST, action="mark_missing",
                    actor=received_by, role=role, bind="clear", entry_session=session_id,
                    ref_type="return", ref_id=return_id,
                    detail={"reason": "归还缺失登记"},
                )
                conn.execute(
                    "INSERT INTO return_items(return_id, item_id, condition) VALUES (?,?,?)",
                    (return_id, iid, "缺失"),
                )
                missed.append(iid)
            conn.execute(
                "UPDATE sessions SET status=? WHERE session_id=?", (SESSION_RETURNING, session_id)
            )
            return {
                "return_id": return_id,
                "session_id": session_id,
                "returned": returned,
                "missing": missed,
            }

    def close_session(self, session_id, *, actor, role=ROLE_ADMIN):
        """结案：在途/使用/隔离中的物资必须先归还或登记缺失；未出库的占用自动释放。"""
        self._check_role(role)
        with self.db.write_tx() as conn:
            s = self._session(conn, session_id)
            if s["status"] not in (SESSION_CONFIRMED, SESSION_SHIPPING, SESSION_IN_USE, SESSION_RETURNING):
                raise conflict(
                    "session_state",
                    f"场次状态为「{s['status']}」，不能结案",
                    {"session_id": session_id, "status": s["status"]},
                )
            outstanding = conn.execute(
                """SELECT item_id, status FROM items WHERE session_id=? AND status IN (?,?,?)
                   ORDER BY item_id""",
                (session_id, SHIPPED, IN_USE, QUARANTINED),
            ).fetchall()
            if outstanding:
                raise conflict(
                    "close_blocked",
                    "仍有物资未归还或未登记缺失，不能结案",
                    {"outstanding": [
                        {"item_id": r["item_id"], "status": r["status"],
                         "location": ITEM_LOCATIONS[r["status"]]}
                        for r in outstanding
                    ]},
                )
            released = []
            for row in conn.execute(
                "SELECT item_id FROM items WHERE session_id=? AND status=? ORDER BY item_id",
                (session_id, RESERVED),
            ).fetchall():
                self._transition(
                    conn, row["item_id"], to=STOCK, action="release", actor=actor, role=role,
                    bind="clear", entry_session=session_id,
                    detail={"reason": "结案释放未出库占用"},
                )
                released.append(row["item_id"])
            for reservation in conn.execute(
                "SELECT * FROM reservations WHERE session_id=? AND status=?", (session_id, RESERVATION_ACTIVE)
            ).fetchall():
                shipped = conn.execute(
                    """SELECT 1 FROM shipment_items si
                       JOIN shipments sh ON sh.shipment_id = si.shipment_id
                       JOIN kit_members km ON km.item_id = si.item_id
                       WHERE sh.session_id=? AND km.kit_id=? LIMIT 1""",
                    (session_id, reservation["kit_id"]),
                ).fetchone()
                conn.execute(
                    "UPDATE reservations SET status=? WHERE reservation_id=?",
                    (RESERVATION_FULFILLED if shipped else RESERVATION_RELEASED, reservation["reservation_id"]),
                )
            conn.execute(
                "UPDATE sessions SET status=? WHERE session_id=?", (SESSION_CLOSED, session_id)
            )
            return {
                "session_id": session_id,
                "status": SESSION_CLOSED,
                "released_items": released,
            }

    # ------------------------------------------------------------------
    # 损坏隔离与处置
    # ------------------------------------------------------------------

    def report_damage(self, item_id, *, reason, actor, role):
        """损坏隔离：已出库的保持场次绑定待归还清算，未出库的解除绑定。"""
        self._check_role(role)
        if not reason or not str(reason).strip():
            raise bad_request("invalid_damage", "损坏原因不能为空")
        with self.db.write_tx() as conn:
            item = self._item(conn, item_id)
            if item["status"] == QUARANTINED:
                return {
                    "item_id": item_id,
                    "status": QUARANTINED,
                    "affected_session": item["session_id"],
                    "duplicate": True,
                }
            if item["status"] in (LOST, SCRAPPED):
                raise conflict(
                    "item_state",
                    f"物资 {item_id} 当前状态为「{item['status']}」，不能报损",
                    {"item_id": item_id, "status": item["status"]},
                )
            keep_binding = item["status"] in (SHIPPED, IN_USE)
            affected_session = item["session_id"]
            self._transition(
                conn, item_id, to=QUARANTINED, action="damage", actor=actor, role=role,
                bind="keep" if keep_binding else "clear", entry_session=affected_session,
                detail={"reason": reason},
            )
            kit = conn.execute("SELECT kit_id FROM kit_members WHERE item_id=?", (item_id,)).fetchone()
            return {
                "item_id": item_id,
                "status": QUARANTINED,
                "affected_session": affected_session,
                "kit_id": kit["kit_id"] if kit else None,
                "duplicate": False,
            }

    def resolve_item(self, item_id, *, outcome, actor, role=ROLE_ADMIN):
        """隔离件处置：修复回库（备货）或报废。"""
        self._check_role(role)
        if outcome not in ("修复", "报废"):
            raise bad_request("invalid_outcome", "outcome 应为：修复、报废")
        with self.db.write_tx() as conn:
            item = self._item(conn, item_id)
            if item["status"] != QUARANTINED:
                raise conflict(
                    "item_state",
                    f"物资 {item_id} 当前状态为「{item['status']}」，不在隔离中",
                    {"item_id": item_id, "status": item["status"]},
                )
            if item["session_id"] is not None:
                raise conflict(
                    "item_bound",
                    f"物资 {item_id} 仍绑定场次 {item['session_id']}，请先完成归还清算",
                    {"item_id": item_id, "session_id": item["session_id"]},
                )
            to = STOCK if outcome == "修复" else SCRAPPED
            self._transition(
                conn, item_id, to=to,
                action="resolve_repair" if outcome == "修复" else "scrap",
                actor=actor, role=role, bind="clear",
                detail={"outcome": outcome},
            )
            return {"item_id": item_id, "status": to}

    # ------------------------------------------------------------------
    # 查询：库存、追溯、分录
    # ------------------------------------------------------------------

    def list_items(self, *, status=None, session_id=None, category=None) -> dict:
        clauses, params = [], []
        if status:
            clauses.append("status=?")
            params.append(status)
        if session_id:
            clauses.append("session_id=?")
            params.append(session_id)
        if category:
            clauses.append("category=?")
            params.append(category)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self.db.read_tx() as conn:
            rows = conn.execute(f"SELECT * FROM items{where} ORDER BY item_id", params).fetchall()
            return {"items": [self._item_view(conn, row) for row in rows]}

    def get_stock(self) -> dict:
        """库存总览：按状态/品类统计，逐模板给出可锁定套装数与散件余量。"""
        with self.db.read_tx() as conn:
            by_status = {
                row["status"]: row["n"]
                for row in conn.execute("SELECT status, COUNT(*) AS n FROM items GROUP BY status")
            }
            by_category: dict[str, dict] = {}
            for row in conn.execute("SELECT category, status, COUNT(*) AS n FROM items GROUP BY category, status"):
                bucket = by_category.setdefault(row["category"], {"total": 0})
                bucket["total"] += row["n"]
                bucket[row["status"]] = row["n"]
            templates = []
            for tpl in conn.execute("SELECT * FROM set_templates ORDER BY template_id").fetchall():
                parts = conn.execute(
                    "SELECT category, quantity FROM set_template_parts WHERE template_id=? ORDER BY category",
                    (tpl["template_id"],),
                ).fetchall()
                kits_total = conn.execute(
                    "SELECT COUNT(*) AS n FROM kits WHERE template_id=?", (tpl["template_id"],)
                ).fetchone()["n"]
                templates.append({
                    "template_id": tpl["template_id"],
                    "name": tpl["name"],
                    "parts": [dict(p) for p in parts],
                    "kits_total": kits_total,
                    "kits_ready": len(self._available_kits(conn, tpl["template_id"])),
                })
            loose = conn.execute(
                """SELECT i.category, COUNT(*) AS n FROM items i
                   WHERE i.status IN ('备货','归还') AND i.session_id IS NULL
                     AND NOT EXISTS (SELECT 1 FROM kit_members km WHERE km.item_id = i.item_id)
                   GROUP BY i.category"""
            ).fetchall()
            kits_ready_total = conn.execute(
                """SELECT COUNT(*) AS n FROM kits k
                   WHERE EXISTS (SELECT 1 FROM kit_members km0 WHERE km0.kit_id = k.kit_id)
                     AND NOT EXISTS (
                           SELECT 1 FROM kit_members km JOIN items i ON i.item_id = km.item_id
                           WHERE km.kit_id = k.kit_id
                             AND (i.status NOT IN ('备货','归还') OR i.session_id IS NOT NULL))"""
            ).fetchone()["n"]
            return {
                "by_status": by_status,
                "by_category": by_category,
                "kits_total": conn.execute("SELECT COUNT(*) AS n FROM kits").fetchone()["n"],
                "kits_ready": kits_ready_total,
                "templates": templates,
                "loose_available_by_category": {row["category"]: row["n"] for row in loose},
            }

    def trace_item(self, item_id: str) -> dict:
        """单件全程追溯：批次谱系、当前所在、逐条分录还原去向与责任环节。"""
        with self.db.read_tx() as conn:
            item = self._item(conn, item_id)
            batch = self._get(conn, "SELECT * FROM batches WHERE batch_id=?", (item["batch_id"],), "批次")
            lineage, seen, current = [], {batch["batch_id"]}, batch["parent_batch_id"]
            while current and current not in seen:
                seen.add(current)
                row = conn.execute("SELECT * FROM batches WHERE batch_id=?", (current,)).fetchone()
                if row is None:
                    break
                lineage.append({"batch_id": row["batch_id"], "name": row["name"], "source": row["source"]})
                current = row["parent_batch_id"]
            entries = conn.execute(
                "SELECT * FROM entries WHERE item_id=? ORDER BY seq", (item_id,)
            ).fetchall()
            session = None
            if item["session_id"]:
                srow = conn.execute(
                    "SELECT session_id, school, status FROM sessions WHERE session_id=?",
                    (item["session_id"],),
                ).fetchone()
                session = dict(srow) if srow else {"session_id": item["session_id"]}
            return {
                **self._item_view(conn, item),
                "batch": {
                    "batch_id": batch["batch_id"],
                    "name": batch["name"],
                    "source": batch["source"],
                    "lineage": lineage,
                },
                "current_session": session,
                "entries": [
                    {
                        "seq": e["seq"],
                        "entry_id": e["entry_id"],
                        "action": e["action"],
                        "from_status": e["from_status"],
                        "to_status": e["to_status"],
                        "actor": e["actor"],
                        "role": e["role"],
                        "session_id": e["session_id"],
                        "ref_type": e["ref_type"],
                        "ref_id": e["ref_id"],
                        "detail": json.loads(e["detail"]),
                        "hash": e["hash"],
                        "created_at": e["created_at"],
                    }
                    for e in entries
                ],
            }

    def query_ledger(self, *, item_id=None, session_id=None, action=None, limit=200) -> dict:
        clauses, params = [], []
        if item_id:
            clauses.append("item_id=?")
            params.append(item_id)
        if session_id:
            clauses.append("session_id=?")
            params.append(session_id)
        if action:
            clauses.append("action=?")
            params.append(action)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        limit = max(1, min(int(limit), 1000))
        with self.db.read_tx() as conn:
            rows = conn.execute(
                f"SELECT * FROM entries{where} ORDER BY seq LIMIT ?", (*params, limit)
            ).fetchall()
            return {
                "entries": [
                    {
                        "seq": e["seq"],
                        "entry_id": e["entry_id"],
                        "item_id": e["item_id"],
                        "action": e["action"],
                        "from_status": e["from_status"],
                        "to_status": e["to_status"],
                        "actor": e["actor"],
                        "role": e["role"],
                        "session_id": e["session_id"],
                        "ref_type": e["ref_type"],
                        "ref_id": e["ref_id"],
                        "detail": json.loads(e["detail"]),
                        "hash": e["hash"],
                        "created_at": e["created_at"],
                    }
                    for e in rows
                ]
            }

    # ------------------------------------------------------------------
    # 对账与幂等
    # ------------------------------------------------------------------

    def reconcile(self, *, repair=False, actor="system"):
        """对账：校验分录哈希链，重放分录推导状态并与当前账面对照。

        ``repair=True`` 时按分录把漂移的物资状态修复到账（同样落分录）。
        """
        with self.db.read_tx() as conn:
            chain_problems = verify_chain(conn)
            derived = derive_statuses(conn)
            stored = {
                row["item_id"]: row["status"]
                for row in conn.execute("SELECT item_id, status FROM items")
            }
            drift = [
                {"item_id": iid, "stored": status, "derived": derived[iid]}
                for iid, status in sorted(stored.items())
                if iid in derived and derived[iid] != status
            ]
            orphans = [
                dict(row)
                for row in conn.execute(
                    """SELECT i.item_id, i.status, i.session_id FROM items i
                       JOIN sessions s ON s.session_id = i.session_id
                       WHERE s.status IN (?,?) AND i.status IN (?,?,?)
                       ORDER BY i.item_id""",
                    (SESSION_CANCELLED, SESSION_CLOSED, RESERVED, SHIPPED, IN_USE),
                )
            ]
            checked_entries = conn.execute("SELECT COUNT(*) AS n FROM entries").fetchone()["n"]
        repaired = []
        if repair and drift:
            with self.db.write_tx() as conn:
                for item_drift in drift:
                    item = self._item(conn, item_drift["item_id"])
                    if item["status"] == item_drift["derived"]:
                        continue
                    conn.execute(
                        "UPDATE items SET status=? WHERE item_id=?",
                        (item_drift["derived"], item["item_id"]),
                    )
                    append_entry(
                        conn, item_id=item["item_id"], action="reconcile_repair",
                        from_status=item["status"], to_status=item_drift["derived"],
                        actor=actor, role=ROLE_ADMIN,
                        detail={"reason": "对账修复：账面状态与分录推导不一致"},
                    )
                    repaired.append(item_drift)
        return {
            "chain_ok": not chain_problems,
            "chain_problems": chain_problems,
            "checked_entries": checked_entries,
            "checked_items": len(stored),
            "drift": drift,
            "repaired": repaired,
            "orphan_bindings": orphans,
        }

    def run_idempotent(self, key, endpoint, request_body, fn, success_status):
        """幂等执行：同一 key 重复提交直接回放首个响应，不重复落账。

        检查、业务执行与回执落库在同一写事务内，中途失败可安全重试。
        """
        canonical = json.dumps(request_body, ensure_ascii=False, sort_keys=True)
        with self.db.write_tx() as conn:
            row = conn.execute(
                "SELECT * FROM operations WHERE idempotency_key=?", (key,)
            ).fetchone()
            if row is not None:
                if row["request_json"] != canonical:
                    raise conflict(
                        "idempotency_mismatch",
                        "同一幂等键提交了不同的请求体",
                        {"idempotency_key": key},
                    )
                return json.loads(row["response_json"]), row["status_code"], True
            data = fn()
            conn.execute(
                "INSERT INTO operations(idempotency_key, endpoint, request_json, response_json, status_code, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (key, endpoint, canonical, json.dumps(data, ensure_ascii=False), success_status, now_iso()),
            )
            return data, success_status, False

"""木偶教具库存履约：端到端领域测试。"""
from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from puppet_fulfillment import Database, FulfillmentService
from puppet_fulfillment.errors import (
    Conflict,
    DuplicateReceipt,
    IllegalTransition,
    InsufficientStock,
    NotFound,
)


def bootstrap() -> FulfillmentService:
    """构造标准场景：2 个木偶头（单件粒度）、1 块幕布（批次粒度）组成一套。"""
    s = FulfillmentService(":memory:")
    s.register_staff("MGR", "王管理", "器材管理员")
    s.register_staff("TCH", "陈老师", "活动老师")
    s.register_staff("SCH", "李签收", "学校签收人")
    s.define_kind("HEAD", "木偶头", tracking="单件")
    s.define_kind("CURTAIN", "幕布", tracking="批次")
    s.define_set("STD", "标准木偶套装", {"HEAD": 2, "CURTAIN": 1})
    s.receive_batch("BH1", "HEAD", 3)
    s.receive_batch("BC1", "CURTAIN", 2)
    return s


def setup_show(s: FulfillmentService, sid: str = "S1", rsv: str = "R1") -> dict:
    s.create_show(sid, "阳光小学")
    s.add_reservation(rsv, sid, "STD", 1)
    return s.confirm_show(sid)


class MasterDataTest(unittest.TestCase):
    def test_batch_receive_creates_item_lineage_and_dual_ledger(self) -> None:
        s = bootstrap()
        loc = s.locate_item("BH1-0002")
        self.assertEqual(loc["batch_no"], "BH1")
        self.assertEqual(loc["current_status"], "备货")
        rec = s.reconcile()
        self.assertTrue(rec["ok"], rec["problems"])
        self.assertEqual(rec["checked_items"], 5)
        self.assertEqual(rec["checked_batches"], 2)


class ConfirmLockTest(unittest.TestCase):
    def test_confirm_locks_all_or_nothing(self) -> None:
        s = bootstrap()
        s.create_show("S1", "一小")
        s.add_reservation("R1", "S1", "STD", 1)   # 需要 2 头 + 1 幕布
        s.create_show("S2", "二小")
        s.add_reservation("R2", "S2", "STD", 1)
        locked1 = s.confirm_show("S1")
        self.assertEqual(locked1["status"], "已锁定")
        # 只剩 1 头 < 2，确认必须整体失败
        with self.assertRaises(InsufficientStock) as ctx:
            s.confirm_show("S2")
        self.assertEqual(ctx.exception.details["shortages"][0]["kind_code"], "HEAD")
        # 回滚：场次仍是草稿、未产生任何半锁定
        self.assertEqual(s.show_overview("S2")["status"], "草稿")
        self.assertEqual(s.show_overview("S2")["allocation_units"], [])
        # 幕布仍有 1 件可借（未被失败的确认吞掉）
        self.assertEqual(s.locate_item("BC1-0002")["current_status"], "备货")
        self.assertTrue(s.reconcile()["ok"])

    def test_cancel_releases_locked_stock(self) -> None:
        s = bootstrap()
        setup_show(s, "S1")
        self.assertEqual(s.locate_item("BC1-0001")["current_status"], "占用")
        result = s.cancel_show("S1")
        self.assertEqual(result["released_items"], 3)
        # 取消场次占用的幕布必须重新可借（问题场景的核心修复）
        self.assertEqual(s.locate_item("BC1-0001")["current_status"], "备货")
        self.assertIsNone(s.locate_item("BC1-0001")["holder_show"])
        self.assertTrue(s.reconcile()["ok"])

    def test_cancel_refused_after_shipment(self) -> None:
        s = bootstrap()
        lock = setup_show(s, "S1")
        s.create_outbound("OB1", "S1")
        s.ship_items("OB1", [lock["locked_units"][0]["items"][0]])
        with self.assertRaises(IllegalTransition):
            s.cancel_show("S1")


class OutboundReceiveTest(unittest.TestCase):
    def test_split_shipment_partial_states(self) -> None:
        s = bootstrap()
        lock = setup_show(s, "S1")
        s.create_outbound("OB1", "S1")
        heads = [c for u in lock["locked_units"] for c in u["items"] if c.startswith("BH1")]
        curtains = [c for u in lock["locked_units"] for c in u["items"] if c.startswith("BC1")]
        # 第一批：1 个头
        r1 = s.ship_items("OB1", [heads[0]])
        self.assertEqual(r1["remaining_locked"], 2)
        self.assertEqual(s.show_overview("S1")["status"], "部分出库")
        # 第二批：剩余 1 头 + 幕布
        s.ship_items("OB1", [heads[1]] + curtains)
        self.assertEqual(s.show_overview("S1")["status"], "已出库")

    def test_ship_requires_lock_and_no_double_ship(self) -> None:
        s = bootstrap()
        setup_show(s, "S1")
        s.create_outbound("OB1", "S1")
        with self.assertRaises(IllegalTransition):
            s.ship_items("OB1", ["BH1-0003"])  # 未锁定给该场次
        with self.assertRaises(NotFound):
            s.ship_items("OB1", ["NOPE-1"])

    def test_receive_flow_and_duplicate_blocked(self) -> None:
        s = bootstrap()
        lock = setup_show(s, "S1")
        s.create_outbound("OB1", "S1")
        codes = [c for u in lock["locked_units"] for c in u["items"]]
        s.ship_items("OB1", codes)
        lines = [{"item_code": c, "condition": "完好"} for c in codes]
        rec = s.receive("OB1", "SCH", lines, receipt_no="RC1")
        self.assertEqual(rec["accepted_count"], 3)
        self.assertEqual(s.show_overview("S1")["status"], "已签收")
        with self.assertRaises(DuplicateReceipt):
            s.receive("OB1", "SCH", lines, receipt_no="RC2")
        self.assertTrue(s.reconcile()["ok"])

    def test_damage_found_on_arrival_is_isolated_with_transport_stage(self) -> None:
        s = bootstrap()
        lock = setup_show(s, "S1")
        s.create_outbound("OB1", "S1")
        codes = [c for u in lock["locked_units"] for c in u["items"]]
        s.ship_items("OB1", codes)
        lines = [
            {"item_code": "BH1-0001", "condition": "损坏"},  # 到场即坏，运输环节责任
            {"item_code": "BH1-0002", "condition": "完好"},
            {"item_code": "BC1-0001", "condition": "完好"},
        ]
        rec = s.receive("OB1", "SCH", lines, receipt_no="RC1")
        self.assertEqual(rec["damaged_on_arrival"], ["BH1-0001"])
        loc = s.locate_item("BH1-0001")
        self.assertTrue(loc["quarantine"])
        self.assertEqual(loc["current_status"], "损坏")
        self.assertEqual(loc["responsible_stage"], "差异")
        discs = s.list_discrepancies(status="待处理")
        self.assertEqual(len(discs), 1)
        self.assertEqual(discs[0]["kind"], "损坏")
        self.assertEqual(discs[0]["responsible_stage"], "出库")  # 责任锁定在运输环节
        self.assertTrue(s.reconcile()["ok"])


class ReturnDiscrepancyTest(unittest.TestCase):
    def _delivered(self, s: FulfillmentService, damaged_arrival: str | None = None):
        lock = setup_show(s, "S1")
        s.create_outbound("OB1", "S1")
        codes = [c for u in lock["locked_units"] for c in u["items"]]
        s.ship_items("OB1", codes)
        lines = []
        for c in codes:
            lines.append({"item_code": c,
                          "condition": "损坏" if c == damaged_arrival else "完好"})
        s.receive("OB1", "SCH", lines, receipt_no="RC1")
        good = [c for c in codes if c != damaged_arrival]
        return lock, codes, good

    def test_partial_then_full_return(self) -> None:
        s = bootstrap()
        _, codes, good = self._delivered(s)
        r1 = s.return_items("RT1", "OB1", "MGR", [{"item_code": good[0], "condition": "完好"}])
        self.assertEqual(r1["good"], [good[0]])
        self.assertEqual(s.show_overview("S1")["status"], "部分归还")
        # 已归还的件重新可借
        self.assertEqual(s.locate_item(good[0])["current_status"], "备货")
        # 不能重复归还同一件
        with self.assertRaises(IllegalTransition):
            s.return_items("RT2", "OB1", "MGR", [{"item_code": good[0], "condition": "完好"}])
        rest = good[1:]
        r2 = s.return_items("RT3", "OB1", "MGR",
                            [{"item_code": c, "condition": "完好"} for c in rest])
        self.assertEqual(len(r2["good"]), 2)
        self.assertEqual(s.show_overview("S1")["status"], "已完成")
        self.assertTrue(s.reconcile()["ok"])

    def test_return_damage_and_missing_open_discrepancies(self) -> None:
        s = bootstrap()
        _, codes, good = self._delivered(s)
        heads = [c for c in good if c.startswith("BH1")]
        curtain = next(c for c in good if c.startswith("BC1"))
        s.return_items("RT1", "OB1", "MGR", [
            {"item_code": heads[0], "condition": "完好"},
            {"item_code": heads[1], "condition": "损坏"},
            {"item_code": curtain, "condition": "缺失"},
        ])
        self.assertEqual(s.locate_item(heads[1])["current_status"], "损坏")
        self.assertTrue(s.locate_item(heads[1])["quarantine"])
        self.assertEqual(s.locate_item(curtain)["current_status"], "丢失")
        kinds = {d["kind"] for d in s.list_discrepancies()}
        self.assertEqual(kinds, {"损坏", "丢失"})
        # 修复一件入库、报废/核销其余，差异全部可结案
        dmg = [d for d in s.list_discrepancies() if d["kind"] == "损坏"][0]
        s.resolve_discrepancy(dmg["discrepancy_id"], "道具组修复", action="修复入库")
        self.assertEqual(s.locate_item(heads[1])["current_status"], "备货")
        lost = [d for d in s.list_discrepancies() if d["kind"] == "丢失"][0]
        s.resolve_discrepancy(lost["discrepancy_id"], "学校赔偿", action="核销")
        self.assertEqual(s.locate_item(curtain)["current_status"], "已报废")
        self.assertEqual(s.list_discrepancies(status="待处理"), [])
        self.assertTrue(s.reconcile()["ok"])

    def test_in_use_damage_then_substitute_and_swap_back(self) -> None:
        s = bootstrap()
        _, _, good = self._delivered(s)
        head = next(c for c in good if c.startswith("BH1"))  # 取一个木偶头
        # 在校使用中损坏一个木偶头（第三件原本在库，可作备件）
        s.report_damage(head, note="操纵杆断裂")
        self.assertEqual(s.locate_item(head)["current_status"], "损坏")
        sub = s.substitute("S1", head, "BH1-0003", note="临时调拨备件")
        self.assertEqual(s.locate_item("BH1-0003")["current_status"], "使用")
        self.assertEqual(s.locate_item("BH1-0003")["holder_show"], "S1")
        # 备件不能被另一场次重复借用
        s.create_show("S9", "九小")
        s.add_reservation("R9", "S9", "STD", 1)
        with self.assertRaises(InsufficientStock):
            s.confirm_show("S9")
        # 撤回备件完好入库
        back = s.swap_back(sub["substitution_id"], condition="完好")
        self.assertEqual(back["status"], "已换回")
        self.assertEqual(s.locate_item("BH1-0003")["current_status"], "备货")
        self.assertTrue(s.reconcile()["ok"])

    def test_substitute_requires_isolated_original(self) -> None:
        s = bootstrap()
        _, _, good = self._delivered(s)
        head = next(c for c in good if c.startswith("BH1"))
        with self.assertRaises(IllegalTransition):
            s.substitute("S1", head, "BH1-0003")  # 原物没坏，不许替换


class IdempotencyTest(unittest.TestCase):
    def test_same_key_replays_result(self) -> None:
        s = bootstrap()
        setup_show(s, "S1")
        s.create_outbound("OB1", "S1")
        r1 = s.ship_items("OB1", ["BH1-0001"], idem_key="ship-1")
        r2 = s.ship_items("OB1", ["BH1-0001"], idem_key="ship-1")
        self.assertEqual(r1["event_id"], r2["event_id"])
        self.assertEqual(r1["shipped_count"], r2["shipped_count"])
        # 只出库一次
        with self.assertRaises(IllegalTransition):
            s.ship_items("OB1", ["BH1-0001"], idem_key="ship-2")

    def test_same_key_different_payload_rejected(self) -> None:
        s = bootstrap()
        setup_show(s, "S1")
        s.create_outbound("OB1", "S1")
        s.ship_items("OB1", ["BH1-0001"], idem_key="k")
        with self.assertRaises(Conflict):
            s.ship_items("OB1", ["BH1-0002"], idem_key="k")


class TraceabilityTest(unittest.TestCase):
    def test_item_history_reconstructs_journey(self) -> None:
        s = bootstrap()
        _, codes, _ = ReturnDiscrepancyTest()._delivered(s)
        s.return_items("RT1", "OB1", "MGR",
                       [{"item_code": c, "condition": "完好"} for c in codes])
        hist = s.item_history(codes[0])
        buckets = [(e["bucket"], e["amount_qty"]) for e in hist["timeline"]]
        # 备货→占用→出库→使用→归还→备货，且责任环节齐备
        self.assertIn(("备货", 1), buckets)
        self.assertIn(("占用", 1), buckets)
        self.assertIn(("出库", 1), buckets)
        self.assertIn(("使用", 1), buckets)
        self.assertIn(("归还", 1), buckets)
        stages = {e["stage"] for e in hist["timeline"]}
        self.assertEqual(stages, {"备货", "占用", "出库", "使用", "归还"})
        # 每条带场次的分录都能回溯到 S1 与具体事件；入库分录无场次属正常
        for e in hist["timeline"]:
            self.assertIsNotNone(e["event_id"])
            if e["show_id"] is not None:
                self.assertEqual(e["show_id"], "S1")
        self.assertTrue(any(e["show_id"] == "S1" for e in hist["timeline"]))


class ConcurrentBorrowTest(unittest.TestCase):
    def test_parallel_confirms_do_not_double_allocate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "conc.db")
            db = Database(db_path)

            def make() -> FulfillmentService:
                return FulfillmentService(Database(db_path))

            seed = make()
            seed.register_staff("MGR", "王管理", "器材管理员")
            seed.register_staff("SCH", "李签收", "学校签收人")
            seed.define_kind("HEAD", "木偶头", tracking="单件")
            seed.define_kind("CURTAIN", "幕布", tracking="批次")
            seed.define_set("STD", "套装", {"HEAD": 2, "CURTAIN": 1})
            seed.receive_batch("BH1", "HEAD", 2)   # 恰好只够一场
            seed.receive_batch("BC1", "CURTAIN", 1)
            seed.create_show("S1", "一小")
            seed.add_reservation("R1", "S1", "STD", 1)
            seed.create_show("S2", "二小")
            seed.add_reservation("R2", "S2", "STD", 1)

            results: dict[str, object] = {}
            barrier = threading.Barrier(2)

            def worker(sid: str) -> None:
                svc = make()
                barrier.wait()
                try:
                    results[sid] = svc.confirm_show(sid)
                except (InsufficientStock, Conflict) as exc:  # 串行化后必有一个失败
                    results[sid] = exc

            t1 = threading.Thread(target=worker, args=("S1",))
            t2 = threading.Thread(target=worker, args=("S2",))
            t1.start(); t2.start(); t1.join(); t2.join()

            ok = [k for k, v in results.items() if not isinstance(v, Exception)]
            bad = [k for k, v in results.items() if isinstance(v, Exception)]
            self.assertEqual(len(ok), 1, results)
            self.assertEqual(len(bad), 1)
            checker = make()
            self.assertEqual(checker.show_overview(ok[0])["status"], "已锁定")
            self.assertTrue(checker.reconcile()["ok"])
            # 失败方重试仍失败（库存确实被赢家持有）
            with self.assertRaises((InsufficientStock, Conflict)):
                make().confirm_show(bad[0])


if __name__ == "__main__":
    unittest.main()

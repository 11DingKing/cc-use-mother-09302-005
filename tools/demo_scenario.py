"""端到端场景演示：还原“少关键部件 / 取消场次仍占幕布 / 东西去向不明”的处置。

运行：PYTHONPATH=src python3 tools/demo_scenario.py
每一步后都打印全库统一分录对账结果，最后沿单件分录还原去向与责任环节。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from puppet_fulfillment import FulfillmentService  # noqa: E402
from puppet_fulfillment.errors import DomainError  # noqa: E402


def step(title: str) -> None:
    print("\n" + "=" * 68)
    print(title)
    print("=" * 68)


def check(svc: FulfillmentService) -> None:
    rec = svc.reconcile()
    print(f"[对账] ok={rec['ok']} 核对实物 {rec['checked_items']} 件 / 批次 {rec['checked_batches']} 个")
    if rec["problems"]:
        print(json.dumps(rec["problems"], ensure_ascii=False, indent=2))


def main() -> None:
    s = FulfillmentService(":memory:")

    step("0. 主数据：套装=2 木偶头(单件粒度)+1 幕布(批次粒度)；两批次入库")
    s.register_staff("MGR", "器材管理员", "器材管理员")
    s.register_staff("SCH", "学校签收人", "学校签收人")
    s.define_kind("HEAD", "木偶头", tracking="单件")
    s.define_kind("CURTAIN", "幕布", tracking="批次")
    s.define_set("STD", "标准木偶套装", {"HEAD": 2, "CURTAIN": 1})
    s.receive_batch("BH2609", "HEAD", 3, supplier="温州偶具厂")
    s.receive_batch("BC2609", "CURTAIN", 2, supplier="绍兴布坊")
    check(s)

    step("1. 场次 S-取消 先占用，随后取消：幕布必须立即释放可借")
    s.create_show("S-取消", "育才小学")
    s.add_reservation("R0", "S-取消", "STD", 1)
    s.confirm_show("S-取消")
    print("取消前幕布状态：", s.locate_item("BC2609-0001")["current_status"])
    s.cancel_show("S-取消")
    print("取消后幕布状态：", s.locate_item("BC2609-0001")["current_status"])
    check(s)

    step("2. 场次 S-巡演 确认：一次性锁定足量库存（不足则整场失败）")
    s.create_show("S-巡演", "阳光小学")
    s.add_reservation("R1", "S-巡演", "STD", 1)
    lock = s.confirm_show("S-巡演", idem_key="confirm-tour")
    for u in lock["locked_units"]:
        print(f"  锁定分录 {u['unit_id']}: 批次 {u['batch_no']} {u['kind_code']} x{u['qty']} -> {u['items']}")
    check(s)

    step("3. 分批出库（先发一个头），场次=部分出库；再发齐")
    s.create_outbound("OB1", "S-巡演", created_by="MGR")
    heads = [c for u in lock["locked_units"] for c in u["items"] if c.startswith("BH")]
    rest = [c for u in lock["locked_units"] for c in u["items"] if not c.startswith("BH")]
    print("第一批：", s.ship_items("OB1", [heads[0]])["remaining_locked"], "件仍在库待出")
    s.ship_items("OB1", [heads[1]] + rest)
    print("场次状态：", s.show_overview("S-巡演")["status"])
    check(s)

    step("4. 现场签收：1 个头到场即损坏→立即隔离，差异责任记在出库（运输）环节")
    codes = heads + rest
    rec = s.receive("OB1", "SCH", [
        {"item_code": heads[0], "condition": "损坏"},
        {"item_code": heads[1], "condition": "完好"},
        {"item_code": rest[0], "condition": "完好"},
    ], receipt_no="RC-1")
    print("签收完好：", rec["accepted"], " 到场损坏：", rec["damaged_on_arrival"])
    try:
        s.receive("OB1", "SCH", [{"item_code": c, "condition": "完好"} for c in codes],
                  receipt_no="RC-2")
    except DomainError as e:
        print("重复签收被拒绝：", e.code)
    check(s)

    step("5. 临时替换：坏头隔离后，用库存备件 BH2609-0003 补到现场")
    sub = s.substitute("S-巡演", heads[0], "BH2609-0003", note="巡演备件")
    print("替换件：", sub["replacement"], " 分录：", sub["unit_id"])
    check(s)

    step("6. 部分归还：好头与幕布归还；幕布登记缺失（少关键部件）→差异")
    r = s.return_items("RT1", "OB1", "MGR", [
        {"item_code": heads[1], "condition": "完好"},
        {"item_code": rest[0], "condition": "缺失"},
    ])
    print("完好：", r["good"], " 损坏：", r["damaged"], " 缺失：", r["missing"])
    print("场次状态：", s.show_overview("S-巡演")["status"])
    check(s)

    step("7. 差异对账结案：坏头报废；幕布丢失核销赔偿；备件完好换回")
    for d in s.list_discrepancies(status="待处理"):
        action = "报废" if d["kind"] == "损坏" else "核销"
        s.resolve_discrepancy(d["discrepancy_id"],
                              "运输包装不善，责任在出库环节" if d["responsible_stage"] == "出库"
                              else "学校赔偿", action=action)
    s.swap_back(sub["substitution_id"], condition="完好")
    print("未结差异：", s.list_discrepancies(status="待处理"))
    print("场次状态：", s.show_overview("S-巡演")["status"])
    check(s)

    step("8. 沿单件分录还原去向：问题幕布 BC2609-0001 的完整轨迹")
    hist = s.item_history("BC2609-0001")
    print("当前：", hist["location"]["current_status"], " 责任环节：", hist["location"]["responsible_stage"])
    for e in hist["timeline"]:
        sign = "+" if e["amount_qty"] > 0 else ""
        print(f"  #{e['entry_id']:<3} {e['event_type']:<4} 环节={e['stage']:<2} "
              f"{e['bucket']} {sign}{e['amount_qty']} 场次={e['show_id'] or '-'} 备注={e['note'] or ''}")


if __name__ == "__main__":
    main()

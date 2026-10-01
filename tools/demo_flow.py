#!/usr/bin/env python3
"""端到端演示：从批次登记到归还对账的完整履约流程。

模拟故障场景：巡演团队到校发现木偶损坏、取消场次占用不释放、
归还出现缺失差异——全部通过统一分录推进并可对账追溯。

运行：python3 tools/demo_flow.py
"""
from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from prop_inventory.api import create_server


def call(port, method, path, body=None, headers=None, expect=None):
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", method=method,
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(request) as response:
            status, payload = response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        status, payload = exc.code, json.loads(exc.read().decode("utf-8"))
    if expect is not None and status != expect:
        raise SystemExit(f"!! {method} {path} 期望 {expect} 实际 {status}: {json.dumps(payload, ensure_ascii=False)}")
    return status, payload


def show(step, payload):
    print(f"\n== {step}")
    print(json.dumps(payload, ensure_ascii=False, indent=2)[:1200])


def main() -> None:
    server, _ = create_server("127.0.0.1", 0, ":memory:")
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        print("== 0. 服务已启动（内存库，演示结束即销毁）")

        # 1. 器材管理员登记批次（含谱系）与套装
        _, batch = call(port, "POST", "/api/batches", {
            "batch_id": "BAT-2026-A", "name": "2026 巡演主批次", "source": "采购", "actor": "王库管",
            "items": [
                {"item_id": "MB-01", "category": "幕布", "label": "金丝幕布"},
                {"item_id": "MB-02", "category": "幕布", "label": "金丝幕布"},
                {"item_id": "OU-01", "category": "木偶", "label": "布袋木偶-生"},
                {"item_id": "OU-02", "category": "木偶", "label": "布袋木偶-旦"},
                {"item_id": "OU-03", "category": "木偶", "label": "布袋木偶-净"},
                {"item_id": "OU-04", "category": "木偶", "label": "布袋木偶-丑"},
            ]}, expect=201)
        _, spare = call(port, "POST", "/api/batches", {
            "batch_id": "BAT-2026-A1", "name": "备件补充批", "source": "调拨",
            "parent_batch_id": "BAT-2026-A", "actor": "王库管",
            "items": [{"item_id": "OU-09", "category": "木偶", "label": "备用木偶"}]}, expect=201)
        show("1. 批次登记（BAT-2026-A1 谱系挂在 BAT-2026-A 下）", spare["data"])

        call(port, "POST", "/api/templates", {
            "template_id": "TPL-TOUR", "name": "木偶巡演套装",
            "parts": [{"category": "幕布", "quantity": 1}, {"category": "木偶", "quantity": 2}]}, expect=201)
        call(port, "POST", "/api/kits", {"kit_id": "KIT-01", "template_id": "TPL-TOUR",
             "name": "巡演一号套", "item_ids": ["MB-01", "OU-01", "OU-02"], "actor": "王库管"}, expect=201)
        call(port, "POST", "/api/kits", {"kit_id": "KIT-02", "template_id": "TPL-TOUR",
             "name": "巡演二号套", "item_ids": ["MB-02", "OU-03", "OU-04"], "actor": "王库管"}, expect=201)
        print("\n== 2. 模板 TPL-TOUR（幕布x1+木偶x2），组装 KIT-01 / KIT-02")

        # 2. 场次确认：一次性锁定；库存不足全有或全无
        call(port, "POST", "/api/sessions", {
            "session_id": "SES-YANGGUANG", "school": "阳光小学", "teacher": "李老师",
            "planned_at": "2026-10-12", "requirements": [{"template_id": "TPL-TOUR", "quantity": 2}]}, expect=201)
        _, locked = call(port, "POST", "/api/sessions/SES-YANGGUANG/confirm", {"actor": "王库管"}, expect=200)
        show("3. 阳光小学场次确认：一次性锁定 2 套", locked["data"])
        call(port, "POST", "/api/sessions", {
            "session_id": "SES-XIWANG", "school": "希望小学", "teacher": "陈老师",
            "planned_at": "2026-10-13", "requirements": [{"template_id": "TPL-TOUR", "quantity": 1}]}, expect=201)
        status, denied = call(port, "POST", "/api/sessions/SES-XIWANG/confirm", {"actor": "王库管"})
        show(f"4. 希望小学确认被拒（HTTP {status}，全有或全无，未锁定任何物资）", denied["error"])

        # 3. 分批出库 + 现场签收（含重复签收）
        _, shp1 = call(port, "POST", "/api/sessions/SES-YANGGUANG/shipments",
                       {"actor": "王库管", "kit_ids": ["KIT-01"]}, expect=201)
        _, shp2 = call(port, "POST", "/api/sessions/SES-YANGGUANG/shipments",
                       {"actor": "王库管", "item_ids": ["MB-02", "OU-03"]}, expect=201)
        _, shp3 = call(port, "POST", "/api/sessions/SES-YANGGUANG/shipments",
                       {"actor": "王库管", "item_ids": ["OU-04"]}, expect=201)
        print(f"\n== 5. 分批出库：{shp1['data']['shipment_id']}（KIT-01 整包）、"
              f"{shp2['data']['shipment_id']}、{shp3['data']['shipment_id']}（KIT-02 拆两批）")
        shp1_id = shp1["data"]["shipment_id"]
        call(port, "POST", f"/api/shipments/{shp1_id}/sign", {"signed_by": "周校长"}, expect=200)
        _, dup = call(port, "POST", f"/api/shipments/{shp1_id}/sign", {"signed_by": "周校长"}, expect=200)
        show("6. KIT-01 签收；网络重试导致重复签收 → 幂等，不重复落账", dup["data"])
        call(port, "POST", f"/api/shipments/{shp2['data']['shipment_id']}/sign", {"signed_by": "周校长"}, expect=200)
        call(port, "POST", f"/api/shipments/{shp3['data']['shipment_id']}/sign", {"signed_by": "周校长"}, expect=200)

        # 4. 到校发现木偶损坏：隔离 → 增补出库 → 成员替换
        _, dmg = call(port, "POST", "/api/items/OU-03/damage",
                      {"reason": "演出中操控杆断裂", "actor": "李老师", "role": "活动老师"}, expect=200)
        show("7. 到校发现 OU-03 损坏：隔离，仍挂场次待清算", dmg["data"])
        _, extra = call(port, "POST", "/api/sessions/SES-YANGGUANG/shipments",
                        {"actor": "王库管", "extra_item_ids": ["OU-09"]}, expect=201)
        call(port, "POST", f"/api/shipments/{extra['data']['shipment_id']}/sign",
             {"signed_by": "周校长"}, expect=200)
        _, swap = call(port, "POST", "/api/kits/KIT-02/replace", {
            "old_item_id": "OU-03", "new_item_id": "OU-09",
            "old_condition": "损坏", "reason": "现场替换", "actor": "王库管"}, expect=200)
        show("8. 备件 OU-09 增补出库签收后入套，KIT-02 恢复齐套", swap["data"])

        # 5. 部分归还 + 缺失差异 + 结案
        call(port, "POST", "/api/sessions/SES-YANGGUANG/returns", {
            "received_by": "王库管",
            "items": [{"item_id": i, "condition": "完好"} for i in ("MB-01", "OU-01", "OU-02", "MB-02", "OU-09")]}, expect=201)
        status, blocked = call(port, "POST", "/api/sessions/SES-YANGGUANG/close", {"actor": "王库管"})
        show(f"9. 部分归还后直接结案被拒（HTTP {status}，尚有物资未清算）", blocked["error"])
        call(port, "POST", "/api/sessions/SES-YANGGUANG/returns", {
            "received_by": "王库管",
            "items": [{"item_id": "OU-03", "condition": "损坏"}], "missing": ["OU-04"]}, expect=201)
        _, closed = call(port, "POST", "/api/sessions/SES-YANGGUANG/close", {"actor": "王库管"}, expect=200)
        show("10. 坏件归还入隔离、OU-04 登记缺失差异后结案", closed["data"])

        # 6. 取消场次释放占用（修复“取消后幕布仍不可借”）
        _, stock_before = call(port, "GET", "/api/stock", expect=200)
        print(f"\n== 11. 结幕后库存：{json.dumps(stock_before['data']['by_status'], ensure_ascii=False)}")
        _, trace = call(port, "GET", "/api/items/OU-04/trace", expect=200)
        show("12. 沿 OU-04 分录还原去向与责任环节", [
            {"seq": e["seq"], "action": e["action"], "to": e["to_status"], "actor": e["actor"], "role": e["role"]}
            for e in trace["data"]["entries"]
        ])
        _, report = call(port, "POST", "/api/reconcile", {}, expect=200)
        show("13. 对账：分录哈希链完整、账实无漂移", report["data"])
        print("\n演示完成：全部环节通过统一分录推进，可核对、可续账、可逐件追溯。")
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()

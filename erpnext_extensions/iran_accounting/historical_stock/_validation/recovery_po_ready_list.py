from __future__ import annotations
import json
def run():
    from erpnext_extensions.iran_accounting.stock_posting_order.scanner import run_full_history_scan
    from erpnext_extensions.iran_accounting.historical_stock.planner import attach_plan, READY_STATUSES
    scan = run_full_history_scan(company="اسپاد فارمد دارو")
    ready=[]
    for raw in scan.get("rows") or []:
        r=attach_plan(dict(raw))
        if r.get("eligible") and str(r.get("planner_status") or "") in READY_STATUSES and int(r.get("sql_updates") or 0)>0:
            ready.append({
                "out": r.get("outbound_document"),
                "in": r.get("inbound_document"),
                "item": r.get("item"),
                "ps": r.get("planner_status"),
                "cur": r.get("current_outbound_time"),
                "prop": r.get("proposed_outbound_time"),
            })
    print(json.dumps({"n": len(ready), "ready": ready}, ensure_ascii=False, indent=2, default=str))
    return ready


from __future__ import annotations
import json
from erpnext_extensions.iran_accounting.historical_stock._validation.v5217_warehouse_proofs import _escalation_rows
from erpnext_extensions.iran_accounting.historical_stock.warehouse_engine.planner import plan_warehouse_repair

def run():
    rows=_escalation_rows()
    r=rows[0]
    p=plan_warehouse_repair(r)
    v=p.get("warehouse_validation") or {}
    a=p.get("warehouse_analysis") or {}
    s=p.get("warehouse_simulation") or {}
    out={
      "item": r.get("item"),
      "status": p.get("planner_status"),
      "warehouse_still_negative": a.get("warehouse_still_negative"),
      "warehouse_qty": a.get("warehouse_qty"),
      "batch_qty_ok": a.get("batch_qty_ok"),
      "sim": s,
      "checks": v.get("checks"),
    }
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str)[:5000])
    return out

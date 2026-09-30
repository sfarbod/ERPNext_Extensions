
# Copyright (c) 2026, ERPNext Extensions contributors
"""Wave Bin sync from last SLE for REPLAY_REQUIRED pairs only."""
from __future__ import annotations
import json
from pathlib import Path
import frappe
from erpnext_extensions.iran_accounting.historical_stock.sle_bin import scan_sle_bin
from erpnext_extensions.iran_accounting.stock_posting_order.replay import _bin_from_last_sle

OUT = Path("/workspace/development/frappe-bench/sites/development.localhost/private/files/hr_correction_20260930")

def _broken():
    scan = scan_sle_bin(company="اسپاد فارمد دارو", limit=5000)
    return [r for r in (scan.get("bin_mismatches") or []) if r.get("status") == "REPLAY_REQUIRED"]

def wave(n: int = 1, *, dry_run: bool = True) -> dict:
    rows = _broken()
    target = rows[: int(n)]
    before = len(rows)
    results = []
    for r in target:
        item, wh = r["item"], r["warehouse"]
        before_bin = frappe.db.get_value("Bin", {"item_code": item, "warehouse": wh},
            ["actual_qty", "stock_value", "valuation_rate"], as_dict=True) or {}
        entry = {"item": item, "warehouse": wh, "before": before_bin,
                 "sle_qty": r.get("sle_qty"), "sle_value": r.get("sle_value"),
                 "last_voucher": r.get("last_voucher")}
        if not dry_run:
            _bin_from_last_sle(item, wh)
            after = frappe.db.get_value("Bin", {"item_code": item, "warehouse": wh},
                ["actual_qty", "stock_value", "valuation_rate"], as_dict=True) or {}
            entry["after"] = after
            entry["ok"] = abs(float(after.get("actual_qty") or 0) - float(r.get("sle_qty") or 0)) <= 0.0001 \
                and abs(float(after.get("stock_value") or 0) - float(r.get("sle_value") or 0)) <= 1
        results.append(entry)
    if not dry_run:
        frappe.db.commit()
    after_n = len(_broken()) if not dry_run else before
    out = {"dry_run": dry_run, "wave_n": n, "before_broken": before, "after_broken": after_n,
           "results": results}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"bin_wave_{n}_{'dry' if dry_run else 'apply'}.json").write_text(
        json.dumps(out, indent=2, default=str, ensure_ascii=False))
    return {"dry_run": dry_run, "wave_n": n, "before_broken": before, "after_broken": after_n,
            "applied": len(results), "ok": sum(1 for e in results if e.get("ok")) if not dry_run else None}

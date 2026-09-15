# Copyright (c) 2026 — live apply one stuck circular root
from __future__ import annotations

import json
import traceback


def run(voucher="MAT-STE-2026-25023-1"):
	from erpnext_extensions.iran_accounting.historical_stock.planner import READY_STATUSES
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import scan_wrong_rates
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate_engine.apply import (
		apply_wrong_rate_root,
	)

	company = "اسپاد فارمد دارو"
	scan = scan_wrong_rates(company=company, limit=4000)
	rows = [r for r in (scan.get("rows") or []) if r.get("voucher") == voucher]
	if not rows:
		print(json.dumps({"error": "missing", "voucher": voucher}))
		return {"error": "missing"}
	r = rows[0]
	meta = {
		"voucher": voucher,
		"ps": r.get("planner_status"),
		"dependency": r.get("dependency"),
		"blocked_because": r.get("blocked_because"),
		"eligible": r.get("eligible"),
		"status": r.get("status"),
		"source": r.get("source"),
		"expected": r.get("expected") or r.get("proposed_rate"),
		"patient_zero": r.get("patient_zero"),
	}
	try:
		out = apply_wrong_rate_root(r, dry_run=False)
		meta["apply"] = {
			k: out.get(k)
			for k in (
				"ok",
				"aborted",
				"reason",
				"after_rate",
				"expected",
				"svd_residual",
				"error",
			)
		}
		if out.get("out"):
			inner = out["out"]
			meta["inner"] = {
				"aborted": inner.get("aborted"),
				"reason": inner.get("reason"),
				"blocked": [
					{"error": b.get("error"), "status": b.get("status")}
					for b in (inner.get("blocked") or [])[:3]
				],
				"applied_n": len(inner.get("applied") or []),
			}
		meta["apply_keys"] = sorted(out.keys())
	except Exception as e:
		meta["exc"] = f"{type(e).__name__}: {e}"
		meta["tb"] = traceback.format_exc()[-2000:]
	print(json.dumps(meta, ensure_ascii=False, indent=2, default=str)[:8000])
	return meta

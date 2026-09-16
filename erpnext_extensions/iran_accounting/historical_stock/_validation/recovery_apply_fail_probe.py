# Copyright (c) 2026 — diagnose stuck READY Wrong Rate apply failures
from __future__ import annotations

import json
import traceback


def run():
	from erpnext_extensions.iran_accounting.historical_stock.planner import READY_STATUSES
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import scan_wrong_rates
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate_engine.apply import (
		apply_wrong_rate_root,
	)

	company = "اسپاد فارمد دارو"
	scan = scan_wrong_rates(company=company, limit=4000)
	ready = [
		r
		for r in scan.get("rows") or []
		if str(r.get("planner_status") or "") in READY_STATUSES
		and r.get("eligible")
		and int(r.get("sql_updates") or 0) > 0
	]
	targets = ["MAT-STE-2026-30879", "MAT-STE-2026-25023-1", "MAT-STE-2026-25280"]
	out = {"n_ready": len(ready), "probes": []}
	for v in targets:
		rows = [r for r in ready if r.get("voucher") == v]
		if not rows:
			rows = [r for r in (scan.get("rows") or []) if r.get("voucher") == v][:1]
		if not rows:
			out["probes"].append({"voucher": v, "error": "not_in_scan"})
			continue
		r = dict(rows[0])
		probe = {
			"voucher": v,
			"ps": r.get("planner_status"),
			"source": r.get("source") or r.get("source_of_truth"),
			"expected": r.get("expected") or r.get("proposed_rate"),
			"current": r.get("current") or r.get("current_rate"),
			"flags": r.get("flags"),
			"status": r.get("status"),
			"eligible": r.get("eligible"),
			"patient_zero": r.get("patient_zero"),
		}
		try:
			res = apply_wrong_rate_root(r, dry_run=True)
			probe["dry"] = {
				k: res.get(k)
				for k in (
					"ok",
					"reason",
					"error",
					"message",
					"status",
					"after_rate",
					"expected",
					"svd_residual",
					"refused",
					"residual_not_cleared",
				)
				if k in res or res.get(k) is not None
			}
			probe["dry_keys"] = sorted(res.keys()) if isinstance(res, dict) else type(res).__name__
			probe["dry_full"] = {k: res.get(k) for k in list(res.keys())[:30]} if isinstance(res, dict) else str(res)[:500]
		except Exception as e:
			probe["exc"] = f"{type(e).__name__}: {e}"
			probe["tb"] = traceback.format_exc()[-1500:]
		out["probes"].append(probe)
	# Also list remaining READY vouchers
	out["ready_vouchers"] = sorted({r.get("voucher") for r in ready})
	print(json.dumps(out, ensure_ascii=False, indent=2, default=str)[:12000])
	return out

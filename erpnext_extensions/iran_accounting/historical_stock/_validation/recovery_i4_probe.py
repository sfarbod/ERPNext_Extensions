# Copyright (c) 2026 — probe READY_I4 on sle_bin vs i4 scanner
from __future__ import annotations

import json


def run():
	from erpnext_extensions.iran_accounting.historical_stock.sle_bin import scan_sle_bin
	from erpnext_extensions.iran_accounting.historical_stock.i4_repair import scan_i4_leftover, repair_i4_selected
	from frappe.utils import nowdate

	company = "اسپاد فارمد دارو"
	sle = scan_sle_bin(company=company, limit=2000)
	ready_i4 = [
		r
		for r in (sle.get("rows") or [])
		if str(r.get("planner_status") or "") == "READY_I4" and int(r.get("sql_updates") or 0) > 0
	]
	i4 = scan_i4_leftover(company=company, from_date="2026-03-21", to_date=str(nowdate()), limit=5000)
	i4_ready = [
		r
		for r in (i4.get("rows") or [])
		if r.get("i4_status") == "READY_I4" or str(r.get("planner_status") or "") == "READY_I4"
	]

	sample = [
		{
			"voucher": r.get("voucher"),
			"item": r.get("item") or r.get("item_code"),
			"warehouse": r.get("warehouse"),
			"status": r.get("status"),
			"planner_status": r.get("planner_status"),
			"sql": r.get("sql_updates"),
			"reason": (r.get("reason") or "")[:120],
			"repair_class": r.get("repair_class"),
		}
		for r in ready_i4[:15]
	]

	# Dry-run first READY_I4 if any
	proof = None
	if ready_i4:
		one = ready_i4[0]
		try:
			dry = repair_i4_selected([one], dry_run=True)
			proof = {"voucher": one.get("voucher"), "dry_ok": not dry.get("aborted"), "dry": {k: dry.get(k) for k in ("aborted", "blocked", "applied", "count") if k in dry or True}}
			# trim
			proof["dry"] = {
				"aborted": dry.get("aborted"),
				"n_applied": len(dry.get("applied") or []),
				"n_blocked": len(dry.get("blocked") or []),
				"blocked0": (dry.get("blocked") or [None])[0],
			}
		except Exception as exc:
			proof = {"error": str(exc), "voucher": one.get("voucher")}

	out = {
		"sle_ready_i4": len(ready_i4),
		"i4_scan_ready": len(i4_ready),
		"i4_by_status": i4.get("by_status"),
		"sample": sample,
		"proof": proof,
	}
	print(json.dumps(out, ensure_ascii=False, indent=2, default=str)[:8000])
	return out

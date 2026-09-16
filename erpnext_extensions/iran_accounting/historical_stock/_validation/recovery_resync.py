# Copyright (c) 2026 — post-campaign deep inventory
from __future__ import annotations

import json
from collections import Counter


def run():
	from erpnext_extensions.iran_accounting.historical_stock.planner import READY_STATUSES
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import scan_wrong_rates
	from erpnext_extensions.iran_accounting.historical_stock.failed_riv import scan_failed_riv
	from erpnext_extensions.iran_accounting.historical_stock.gl_integrity import scan_gl_integrity
	from erpnext_extensions.iran_accounting.historical_stock.zero_rate import scan_zero_rate_rows
	from erpnext_extensions.iran_accounting.historical_stock.i4_repair import scan_i4_leftover
	from erpnext_extensions.iran_accounting.historical_stock.sle_bin import scan_sle_bin
	from erpnext_extensions.iran_accounting.stock_posting_order.scanner import run_full_history_scan
	from frappe.utils import nowdate

	company = "اسپاد فارمد دارو"

	wrong = scan_wrong_rates(company=company, limit=5000)
	zero = scan_zero_rate_rows(company=company)
	riv = scan_failed_riv(company=company, limit=2000)
	gl = scan_gl_integrity(company=company, limit=500)
	i4 = scan_i4_leftover(company=company, from_date="2026-03-21", to_date=str(nowdate()), limit=5000)
	sle = scan_sle_bin(company=company, limit=2000)
	posting = run_full_history_scan(company=company)

	def ready(rows):
		return [
			r
			for r in (rows or [])
			if (
				r.get("planner_status") in READY_STATUSES
				or str(r.get("planner_status") or "").startswith("READY")
			)
			and int(r.get("sql_updates") or 0) > 0
			and r.get("eligible")
		]

	wr_ps = Counter(str(r.get("planner_status") or "") for r in (wrong.get("rows") or []))
	wr_ready = ready(wrong.get("rows"))
	zr_ready = ready(zero.get("rows"))
	po_ready = ready(posting.get("rows"))
	gl_ready = [
		r
		for r in ready(gl.get("rows"))
		if not r.get("sle_poisoned")
	]
	# G2 with non-empty expected map?
	import frappe
	from erpnext.accounts.general_ledger import toggle_debit_credit_if_negative

	g2_viable = []
	for r in (gl.get("rows") or []):
		if r.get("gl_class") != "G2_MISSING" or not r.get("eligible"):
			continue
		try:
			se = frappe.get_doc("Stock Entry", r["voucher"])
			exp = toggle_debit_credit_if_negative(se.get_gl_entries(se.get_inventory_account_map()))
			if exp:
				g2_viable.append(r["voucher"])
		except Exception:
			pass

	riv_safe = [r for r in (riv.get("rows") or []) if r.get("riv_status") == "SAFE_TO_RETRY"]
	i4_ready = [r for r in (i4.get("rows") or []) if r.get("i4_status") == "READY_I4" or (r.get("eligible") and str(r.get("planner_status") or "").startswith("READY"))]

	# Why WR not READY — waiting buckets
	waiting_wr = [r for r in (wrong.get("rows") or []) if "WAITING" in str(r.get("planner_status") or "")]
	manual_wr = [
		r
		for r in (wrong.get("rows") or [])
		if str(r.get("planner_status") or "") in ("RATE_MANUAL", "MANUAL", "RATE_AMBIGUOUS", "AMBIGUOUS", "NO_REPAIR_PATH")
	]

	out = {
		"wrong_rate": {
			"scanned": wrong.get("count"),
			"by_planner": dict(wr_ps),
			"ready": len(wr_ready),
			"waiting": len(waiting_wr),
			"manual_ambiguous": len(manual_wr),
			"by_flag": wrong.get("by_flag"),
			"by_confidence": wrong.get("by_confidence"),
		},
		"zero_rate_ready": len(zr_ready),
		"posting_ready": len(po_ready),
		"i4": {"by_status": i4.get("by_status"), "ready": len(i4_ready), "count": i4.get("count")},
		"gl": {
			"by_class": gl.get("by_class"),
			"ready_any": len(gl_ready),
			"g2_with_gl_map": len(g2_viable),
			"g2_sample": g2_viable[:8],
		},
		"riv": {"by_status": riv.get("by_status"), "safe": len(riv_safe)},
		"sle_bin": {"count": sle.get("count"), "by_status": sle.get("by_status")},
		"auto_repairable_now": {
			"wrong_rate": len(wr_ready),
			"zero_rate": len(zr_ready),
			"posting": len(po_ready),
			"i4": len(i4_ready),
			"gl": len(gl_ready),
			"g2_viable": len(g2_viable),
			"riv_safe": len(riv_safe),
		},
	}
	print(json.dumps(out, ensure_ascii=False, indent=2, default=str)[:10000])
	return out

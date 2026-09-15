# Copyright (c) 2026 — Data recovery: Master Repair Plan inventory
from __future__ import annotations

import json


def run():
	from erpnext_extensions.iran_accounting.historical_stock.master_plan import build_master_repair_plan
	from erpnext_extensions.iran_accounting.historical_stock.planner import READY_STATUSES
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import scan_wrong_rates
	from erpnext_extensions.iran_accounting.historical_stock.failed_riv import scan_failed_riv
	from erpnext_extensions.iran_accounting.historical_stock.gl_integrity import scan_gl_integrity
	from erpnext_extensions.iran_accounting.historical_stock.zero_rate import scan_zero_rate_rows
	from erpnext_extensions.iran_accounting.historical_stock.i4_repair import scan_i4_leftover
	from erpnext_extensions.iran_accounting.historical_stock.campaign_clusters import (
		SAFE_GROUP,
		cluster_independent_roots,
	)
	from frappe.utils import nowdate

	company = "اسپاد فارمد دارو"
	plan = build_master_repair_plan(company=company)

	# Fresh topic scans for READY queues
	wrong = scan_wrong_rates(company=company, limit=2000)
	zero = scan_zero_rate_rows(company=company)
	riv = scan_failed_riv(company=company, limit=500)
	gl = scan_gl_integrity(company=company, limit=300)
	i4 = scan_i4_leftover(company=company, from_date="2026-03-21", to_date=str(nowdate()), limit=5000)

	def ready_rows(rows):
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

	wr_ready = ready_rows(wrong.get("rows"))
	zr_ready = ready_rows(zero.get("rows"))
	gl_ready = [
		r
		for r in ready_rows(gl.get("rows"))
		if not r.get("sle_poisoned")
		and str(r.get("gl_class") or "")
		in ("G1_BALANCED_BUT_ECONOMICALLY_WRONG", "G3_UNBALANCED")
	]
	riv_safe = [r for r in (riv.get("rows") or []) if r.get("riv_status") == "SAFE_TO_RETRY" and r.get("eligible")]
	i4_ready = [r for r in (i4.get("rows") or []) if r.get("i4_status") == "READY_I4" or (r.get("eligible") and str(r.get("planner_status") or "").startswith("READY"))]

	# Cluster Wrong Rate READY
	for r in wr_ready:
		r.setdefault("topic", "WRONG_RATE")
	wr_clusters = cluster_independent_roots(wr_ready, max_cluster=10) if wr_ready else {}

	out = {
		"company": company,
		"master_plan_classes": [
			{
				"class": c.get("repair_class") or c.get("class"),
				"priority": c.get("priority"),
				"total": c.get("total") or c.get("count"),
				"ready": c.get("ready"),
				"waiting": c.get("waiting"),
				"manual": c.get("manual"),
				"ambiguous": c.get("ambiguous"),
				"promotion": c.get("promotion_status") or c.get("status"),
			}
			for c in (plan.get("classes") or plan.get("repair_classes") or [])
		],
		"queues": {
			"wrong_rate_ready": len(wr_ready),
			"zero_rate_ready": len(zr_ready),
			"i4_ready": len(i4_ready),
			"gl_ready_g1_g3": len(gl_ready),
			"riv_safe": len(riv_safe),
		},
		"wrong_rate_cluster": {
			"independent": (wr_clusters or {}).get("independent_root_count"),
			"recommended": {
				"class": ((wr_clusters or {}).get("recommended_first_group") or {}).get("group_class"),
				"n": ((wr_clusters or {}).get("recommended_first_group") or {}).get("n_roots"),
				"vouchers": ((wr_clusters or {}).get("recommended_first_group") or {}).get("repair_order"),
			},
		},
		"sample_wr_ready": [
			{
				"voucher": r.get("voucher"),
				"item": r.get("item"),
				"warehouse": r.get("warehouse"),
				"sql": r.get("sql_updates"),
				"source": r.get("source") or r.get("source_of_truth"),
				"current": r.get("current") or r.get("current_rate"),
				"expected": r.get("expected") or r.get("proposed_rate"),
			}
			for r in sorted(wr_ready, key=lambda x: int(x.get("sql_updates") or 99))[:12]
		],
		"riv_by_status": riv.get("by_status"),
		"gl_by_class": gl.get("by_class"),
		"i4_by_status": i4.get("by_status"),
	}
	print(json.dumps(out, ensure_ascii=False, indent=2, default=str)[:12000])
	return out

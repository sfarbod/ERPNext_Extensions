# Copyright (c) 2026, ERPNext Extensions contributors
"""Phase 4 remaining-root probe (read-only)."""

from __future__ import annotations

import json
from collections import Counter

COMPANY = "اسپاد فارمد دارو"


def run():
	from erpnext_extensions.iran_accounting.historical_stock.zero_rate import scan_zero_rate_rows
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import scan_wrong_rates
	from erpnext_extensions.iran_accounting.historical_stock.planner import attach_plan, READY_STATUSES
	from erpnext_extensions.iran_accounting.historical_stock.root_graph import build_zero_wrong_root_graph
	from erpnext_extensions.iran_accounting.historical_stock.kpi_buckets import count_wrong_rate_buckets
	from erpnext_extensions.iran_accounting.historical_stock.i1_repair import scan_i1_negative_rate
	from erpnext_extensions.iran_accounting.historical_stock.failed_riv import scan_failed_riv
	import frappe

	z = scan_zero_rate_rows(company=COMPANY, limit=8000)
	zrows = [attach_plan(dict(r)) for r in (z.get("rows") or [])]
	recon = [r for r in zrows if r.get("status") == "RECONSTRUCTABLE" or r.get("eligible")]
	ready = [
		r
		for r in recon
		if str(r.get("planner_status") or "").startswith("READY")
		or r.get("planner_status") in READY_STATUSES
	]
	# Why canary filters them out
	blocked_by_pz = []
	for r in recon:
		pz = r.get("patient_zero")
		pz_v = pz.get("voucher_no") if isinstance(pz, dict) else pz
		if pz_v and pz_v != r.get("voucher"):
			blocked_by_pz.append(r)

	w = scan_wrong_rates(company=COMPANY, limit=4000)
	wrows = [attach_plan(dict(r)) for r in (w.get("rows") or [])]
	wb = count_wrong_rate_buckets(wrows)
	wr_ready = [
		r
		for r in wrows
		if (r.get("eligible") or str(r.get("planner_status") or "").startswith("READY"))
		and int(r.get("sql_updates") or 0) > 0
	]
	g = build_zero_wrong_root_graph(zrows, wrows)
	i1 = scan_i1_negative_rate(company=COMPANY, limit=200)
	riv = scan_failed_riv(limit=2000)

	out = {
		"zero": {
			"raw": z.get("raw_count"),
			"actionable": z.get("actionable_count"),
			"recon": z.get("reconstructable_count"),
			"waiting": z.get("waiting_upstream_count"),
			"by_status": dict(Counter(r.get("status") for r in zrows)),
			"by_purpose": dict(Counter(r.get("purpose") for r in zrows)),
			"by_kpi": dict(Counter(r.get("kpi_bucket") for r in zrows)),
			"recon_n": len(recon),
			"ready_n": len(ready),
			"recon_planner": dict(Counter(str(r.get("planner_status")) for r in recon)),
			"recon_blocked_by_foreign_pz": len(blocked_by_pz),
			"ready_sample": [
				{
					"v": r.get("voucher"),
					"p": r.get("purpose"),
					"item": r.get("item"),
					"rate": r.get("proposed_rate"),
					"src": r.get("source_of_truth"),
					"ps": r.get("planner_status"),
				}
				for r in ready[:15]
			],
			"recon_nonready_sample": [
				{
					"v": r.get("voucher"),
					"p": r.get("purpose"),
					"ps": r.get("planner_status"),
					"msg": (r.get("message") or r.get("blocked_because") or "")[:160],
					"pz": (r.get("patient_zero") or {}).get("voucher_no")
					if isinstance(r.get("patient_zero"), dict)
					else r.get("patient_zero"),
				}
				for r in recon
				if r not in ready
			][:15],
		},
		"wrong": {
			"buckets": wb,
			"ready_n": len(wr_ready),
			"matched_but_corrupt": sum(
				1
				for r in wrows
				if r.get("flag") == "MATCHED_BUT_CORRUPT"
				or "MATCHED_BUT_CORRUPT" in (r.get("flags") or [])
			),
			"ready_sample": [
				{
					"v": r.get("voucher"),
					"p": r.get("purpose"),
					"item": r.get("item") or r.get("item_code"),
					"ps": r.get("planner_status"),
					"flag": r.get("flag"),
					"conf": r.get("confidence"),
				}
				for r in wr_ready[:12]
			],
		},
		"graph": {k: g[k] for k in g if not isinstance(g[k], (list, dict))},
		"i1": i1.get("count"),
		"failed_riv": {
			"raw": riv.get("raw_count") or riv.get("count"),
			"actionable": riv.get("actionable_count"),
		},
		"neg_valuation": frappe.db.sql(
			"SELECT COUNT(*) FROM `tabStock Ledger Entry` WHERE is_cancelled=0 AND valuation_rate < -0.0001"
		)[0][0],
		"blockers": dict(
			frappe.db.sql(
				"""
				SELECT lane, COUNT(*) FROM `tabHistorical Repair Blocker`
				WHERE status!='RESOLVED' GROUP BY lane
				"""
			)
		)
		if frappe.db.exists("DocType", "Historical Repair Blocker")
		else {},
		"mr_allow_zero": frappe.db.sql(
			"""
			SELECT COUNT(*) FROM `tabStock Entry Detail` sed
			JOIN `tabStock Entry` se ON se.name=sed.parent
			WHERE se.docstatus=1 AND se.purpose='Material Receipt' AND se.company=%s
			  AND ABS(sed.qty)>0.0001 AND ABS(IFNULL(sed.basic_rate,0))<0.0001
			  AND IFNULL(sed.allow_zero_valuation_rate,0)=1
			""",
			COMPANY,
		)[0][0],
	}
	print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
	return out

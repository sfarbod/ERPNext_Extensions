# Copyright (c) 2026, ERPNext Extensions contributors
"""Post-shortage-fix impact snapshot (read-only)."""

from __future__ import annotations

from collections import Counter

import frappe


def run(company=None):
	from erpnext_extensions.iran_accounting.stock_posting_order.scanner import run_full_history_scan
	from erpnext_extensions.iran_accounting.historical_stock.master_plan import build_master_repair_plan

	company = company or "اسپاد فارمد دارو"
	po = run_full_history_scan(company=company, include_no_repair=True, include_likely=True)
	rows = po.get("rows") or []
	by_opt = Counter()
	by_conf = Counter()
	for r in rows:
		opt = r.get("optimizer_status") or r.get("status") or "?"
		by_opt[opt] += 1
		by_conf[r.get("confidence") or "?"] += 1

	actionable_opts = {
		"REPAIRABLE_SECONDS",
		"ELIGIBLE",
		"CROSS_TIME_REPAIRABLE",
		"SAME_TIME_REPAIRABLE",
		"MULTI_MOVE_REPAIRABLE",
		"MANUAL_APPROVAL",
	}
	actionable = sum(
		1
		for r in rows
		if (r.get("optimizer_status") or r.get("status")) in actionable_opts or r.get("eligible")
	)

	plan = build_master_repair_plan(company=company)
	rg = plan.get("root_cause_graph") or {}
	disc = plan.get("discovery") or {}

	# Family tallies from root graph rows if present
	family = Counter()
	findings = 0
	roots = set()
	for src in (rg.get("roots"), rg.get("nodes"), plan.get("rows"), disc.get("ready_campaigns")):
		if not src:
			continue
		for r in src if isinstance(src, list) else []:
			if not isinstance(r, dict):
				continue
			fam = (
				r.get("family")
				or r.get("issue_family")
				or r.get("kpi_bucket")
				or r.get("class")
				or r.get("topic")
				or "?"
			)
			family[str(fam)] += int(r.get("finding_count") or r.get("findings") or 1)
			findings += int(r.get("finding_count") or r.get("findings") or 1)
			rk = r.get("root") or r.get("patient_zero") or r.get("voucher_no") or r.get("dependency_root")
			if rk:
				roots.add(str(rk))

	safety = frappe.db.sql(
		"""
		SELECT
		  SUM(CASE WHEN valuation_rate < 0 THEN 1 ELSE 0 END) neg_val,
		  SUM(CASE WHEN incoming_rate < 0 THEN 1 ELSE 0 END) neg_in,
		  SUM(CASE WHEN actual_qty > 0 AND valuation_rate < 0 THEN 1 ELSE 0 END) neg_fg
		FROM `tabStock Ledger Entry`
		WHERE is_cancelled=0 AND company=%s
		""",
		(company,),
		as_dict=True,
	)[0]
	i1 = frappe.db.sql(
		"""
		SELECT COUNT(*) c FROM `tabStock Ledger Entry`
		WHERE is_cancelled=0 AND company=%s
		  AND ABS(actual_qty) < 0.0001 AND ABS(stock_value) > 0.5
		""",
		(company,),
		as_dict=True,
	)[0].c
	riv = frappe.db.count("Repost Item Valuation", {"status": ("in", ["Queued", "In Progress"])})

	return {
		"po": {
			"raw": len(rows),
			"actionable": actionable,
			"by_optimizer_status": dict(by_opt),
			"by_confidence": dict(by_conf),
			"REAL_STOCK_SHORTAGE": by_opt.get("REAL_STOCK_SHORTAGE", 0),
			"EXACT_eligible": sum(1 for r in rows if r.get("confidence") == "EXACT" and r.get("eligible")),
			"LIKELY": by_conf.get("LIKELY", 0),
			"AMBIGUOUS": by_opt.get("AMBIGUOUS_DEPENDENCY", 0),
			"WAITING": by_opt.get("LATER_INBOUND_UNRELATED", 0),
			"TOOL_LIMIT": by_opt.get("CROSS_ITEM_CONFLICT", 0)
			+ by_opt.get("MIDNIGHT_REVIEW", 0)
			+ by_opt.get("VALUATION_POISON_DEPENDENCY", 0),
			"summary": {
				k: v for k, v in (po.get("summary") or {}).items() if k != "seconds_distribution"
			},
		},
		"patient_zero": {
			"plan_patient_zero_count": plan.get("patient_zero_count") or disc.get("patient_zero_count"),
			"rg_keys": list(rg.keys())[:40] if isinstance(rg, dict) else [],
			"family_tally": dict(family),
			"tallied_findings": findings,
			"tallied_roots": len(roots),
			"plan_top_keys": list(plan.keys())[:40],
		},
		"blockers": {
			"open_shortage": frappe.db.count(
				"Historical Repair Blocker",
				{
					"issue_type": "HISTORICAL_NEGATIVE_STOCK",
					"status": "OPEN",
					"lane": "USER_ACTION_REQUIRED",
				},
			),
			"open_user_action": frappe.db.count(
				"Historical Repair Blocker",
				{"lane": "USER_ACTION_REQUIRED", "status": "OPEN"},
			),
			"resolved_shortage": frappe.db.count(
				"Historical Repair Blocker",
				{"issue_type": "HISTORICAL_NEGATIVE_STOCK", "status": "RESOLVED"},
			),
		},
		"case_10510117": frappe.db.sql(
			"""
			SELECT name, status, lane, issue_type
			FROM `tabHistorical Repair Blocker`
			WHERE item_code=%s AND voucher_no=%s
			ORDER BY modified DESC LIMIT 3
			""",
			("10510117", "MAT-STE-2026-24520"),
			as_dict=True,
		),
		"safety": {
			"i1": i1,
			"neg_valuation": safety.neg_val,
			"neg_incoming": safety.neg_in,
			"neg_fg": safety.neg_fg,
			"active_riv": riv,
		},
	}

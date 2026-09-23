# Copyright (c) 2026, ERPNext Extensions contributors
"""Phase 5B — transfer/manufacture root preview + PO dry/apply consistency."""

from __future__ import annotations

import json
from collections import Counter

COMPANY = "اسپاد فارمد دارو"


def run():
	import frappe
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import scan_wrong_rates
	from erpnext_extensions.iran_accounting.historical_stock.manual_reason import (
		MANUAL_MANUFACTURE_DEPENDENCY,
		MANUAL_TRANSFER_PROPAGATION,
		classify_wrong_manual_reason,
		summarize_manual_groups,
	)
	from erpnext_extensions.iran_accounting.historical_stock.transfer_valuation import (
		collapse_transfer_roots,
		reconstruct_transfer_valuation,
	)
	from erpnext_extensions.iran_accounting.stock_posting_order.scanner import run_full_history_scan
	from erpnext_extensions.iran_accounting.historical_stock.planner import attach_plan
	from erpnext_extensions.iran_accounting.stock_posting_order.repair import (
		dry_run_selected,
		preflight_apply_eligibility,
	)

	# --- Wrong Rate transfer/manufacture collapse ---
	wr = scan_wrong_rates(company=COMPANY, limit=6000)
	rows = wr.get("rows") or []
	manual = []
	transfer_rows = []
	mfg_rows = []
	class_counts = Counter()
	for r in rows:
		purpose = r.get("purpose") or ""
		ps = str(r.get("planner_status") or r.get("status") or "")
		if purpose in (
			"Material Transfer",
			"Material Transfer for Manufacture",
			"Send to Subcontractor",
		):
			if not r.get("transfer_reconstruction"):
				r["transfer_reconstruction"] = reconstruct_transfer_valuation(r)
			transfer_rows.append(r)
			class_counts[r["transfer_reconstruction"].get("classification") or "?"] += 1
		if purpose == "Manufacture":
			mfg_rows.append(r)
		if (
			not r.get("eligible")
			and ps
			in (
				"MANUAL_REVIEW",
				"RATE_MANUAL",
				"MANUAL",
				"DEPENDENCY_REPAIR_REQUIRED",
				"WAITING_UPSTREAM",
			)
			or r.get("manual_reason")
			or (not r.get("eligible") and r.get("confidence") in ("AMBIGUOUS", "MANUAL"))
		):
			classify_wrong_manual_reason(r)
			manual.append(r)

	xfer_collapse = collapse_transfer_roots(transfer_rows)
	mfg_by_status = Counter(str(r.get("status")) for r in mfg_rows)
	mfg_eligible = sum(1 for r in mfg_rows if r.get("eligible"))
	mfg_waiting = sum(
		1
		for r in mfg_rows
		if (r.get("input_health") or {}).get("status") == "poisoned"
		or r.get("status") == "DEPENDENCY_REPAIR_REQUIRED"
	)
	mfg_roots = set()
	for r in mfg_rows:
		pz = r.get("patient_zero") or {}
		mfg_roots.add((pz.get("voucher_no") if isinstance(pz, dict) else None) or r.get("voucher"))

	manual_summary = summarize_manual_groups(manual)

	# --- PO dry/apply consistency on former READY ---
	po = run_full_history_scan(company=COMPANY)
	ready = []
	for raw in po.get("rows") or []:
		r = attach_plan(dict(raw))
		if str(r.get("planner_status") or "").startswith("READY") and int(r.get("sql_updates") or 0) > 0:
			ready.append(r)
	po_cases = []
	for r in ready:
		pre = preflight_apply_eligibility(r)
		dry = dry_run_selected([dict(r)])
		po_cases.append(
			{
				"out": r.get("outbound_document"),
				"item": r.get("item"),
				"ps": r.get("planner_status"),
				"preflight_ok": pre.get("ok"),
				"preflight_reason": pre.get("reason"),
				"dry_applied": len(dry.get("applied") or []),
				"dry_blocked": len(dry.get("blocked") or []),
				"dry_error": (dry.get("blocked") or [{}])[0].get("error") if dry.get("blocked") else None,
			}
		)

	# Shortage metrics
	shortage_findings = 0
	iw = set()
	ibw = set()
	roots = set()
	for raw in po.get("rows") or []:
		opt = str(raw.get("optimizer_status") or raw.get("status") or "")
		if opt not in ("REAL_STOCK_SHORTAGE", "INSUFFICIENT_STOCK"):
			continue
		shortage_findings += 1
		item = raw.get("item")
		wh = raw.get("warehouse")
		batch = raw.get("batch") or ""
		out_vn = raw.get("outbound_document")
		roots.add((item, wh, batch, out_vn))
		if item and wh:
			iw.add((item, wh))
			ibw.add((item, batch, wh))

	out = {
		"wrong": {
			"raw": wr.get("count") or len(rows),
			"actionable": sum(1 for r in rows if r.get("actionable") is not False),
			"eligible": sum(1 for r in rows if r.get("eligible")),
			"matched_but_corrupt": sum(1 for r in rows if r.get("matched_but_corrupt")),
		},
		"transfer": {
			"findings": len(transfer_rows),
			"by_classification": dict(class_counts),
			"collapse": {
				"root_chains": xfer_collapse.get("root_chains"),
				"unique_item_warehouse": xfer_collapse.get("unique_item_warehouse"),
				"earliest_roots": (xfer_collapse.get("earliest_roots") or [])[:15],
			},
		},
		"manufacture": {
			"findings": len(mfg_rows),
			"by_status": dict(mfg_by_status),
			"eligible": mfg_eligible,
			"waiting_upstream": mfg_waiting,
			"root_chains": len(mfg_roots),
		},
		"manual": manual_summary,
		"po_ready_preflight": {"ready_planner_n": len(ready), "cases": po_cases},
		"shortage": {
			"affected_findings": shortage_findings,
			"user_root_blockers": len(roots),
			"unique_item_warehouse": len(iw),
			"unique_item_batch_warehouse": len(ibw),
		},
	}
	print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
	return out

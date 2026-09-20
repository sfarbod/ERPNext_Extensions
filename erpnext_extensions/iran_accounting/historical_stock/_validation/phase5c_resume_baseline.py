# Copyright (c) 2026, ERPNext Extensions contributors
"""Phase 5C resume — fresh baseline after shortage/opening-state fix (read-only).

No stock repairs. May sync blockers + save metrics snapshot.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from time import perf_counter

COMPANY = "اسپاد فارمد دارو"


def _neg():
	import frappe

	return {
		"neg_valuation": frappe.db.sql(
			"SELECT COUNT(*) FROM `tabStock Ledger Entry` WHERE is_cancelled=0 AND valuation_rate < -0.0001"
		)[0][0],
		"neg_incoming": frappe.db.sql(
			"SELECT COUNT(*) FROM `tabStock Ledger Entry` WHERE is_cancelled=0 AND incoming_rate < -0.0001"
		)[0][0],
		"neg_fg": frappe.db.sql(
			"""
			SELECT COUNT(*) FROM `tabStock Ledger Entry` sle
			JOIN `tabStock Entry` se ON se.name=sle.voucher_no
			WHERE sle.is_cancelled=0 AND se.purpose='Manufacture'
			  AND sle.actual_qty>0
			  AND (sle.valuation_rate < -0.0001 OR sle.incoming_rate < -0.0001)
			"""
		)[0][0],
	}


def _blockers():
	import frappe

	open_user = frappe.db.sql(
		"""
		SELECT COUNT(*) c, COUNT(DISTINCT CONCAT(IFNULL(item_code,''),'|',IFNULL(warehouse,''),'|',IFNULL(voucher_no,''))) roots
		FROM `tabHistorical Repair Blocker`
		WHERE status='OPEN' AND lane='USER_ACTION_REQUIRED'
		""",
		as_dict=True,
	)[0]
	open_tool = frappe.db.sql(
		"""
		SELECT COUNT(*) c, COUNT(DISTINCT CONCAT(IFNULL(item_code,''),'|',IFNULL(warehouse,''),'|',IFNULL(voucher_no,''))) roots
		FROM `tabHistorical Repair Blocker`
		WHERE status='OPEN' AND lane='TOOL_LIMIT'
		""",
		as_dict=True,
	)[0]
	shortage = frappe.db.sql(
		"""
		SELECT COUNT(*) c,
		       COUNT(DISTINCT CONCAT(IFNULL(item_code,''),'|',IFNULL(warehouse,''),'|',IFNULL(voucher_no,''))) roots
		FROM `tabHistorical Repair Blocker`
		WHERE status='OPEN' AND lane='USER_ACTION_REQUIRED'
		  AND issue_type='HISTORICAL_NEGATIVE_STOCK'
		""",
		as_dict=True,
	)[0]
	resolved_hns = frappe.db.count(
		"Historical Repair Blocker",
		{"issue_type": "HISTORICAL_NEGATIVE_STOCK", "status": "RESOLVED"},
	)
	return {
		"user_findings": open_user.c,
		"user_roots": open_user.roots,
		"tool_findings": open_tool.c,
		"tool_roots": open_tool.roots,
		"shortage_findings": shortage.c,
		"shortage_roots": shortage.roots,
		"resolved_shortage": resolved_hns,
	}


def run(*, sync_blockers: int = 1, save_snapshot: int = 1, wrong_limit: int = 6000):
	import frappe
	from frappe.utils import nowdate
	from erpnext_extensions.iran_accounting.historical_stock.metrics_snapshot import (
		save_metrics_snapshot,
		worker_queue_status,
	)
	from erpnext_extensions.iran_accounting.historical_stock.i1_repair import scan_i1_negative_rate
	from erpnext_extensions.iran_accounting.historical_stock.i4_repair import scan_i4_leftover
	from erpnext_extensions.iran_accounting.historical_stock.zero_rate import scan_zero_rate_rows
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import scan_wrong_rates
	from erpnext_extensions.iran_accounting.historical_stock.planner import attach_plan
	from erpnext_extensions.iran_accounting.historical_stock.kpi_buckets import (
		count_wrong_rate_buckets,
	)
	from erpnext_extensions.iran_accounting.historical_stock.failed_riv import scan_failed_riv
	from erpnext_extensions.iran_accounting.historical_stock.master_plan import build_master_repair_plan
	from erpnext_extensions.iran_accounting.stock_posting_order.scanner import run_full_history_scan
	from erpnext_extensions.iran_accounting.historical_stock.transfer_valuation import (
		EXACT,
		RECONSTRUCTABLE,
		collapse_transfer_roots,
		reconstruct_transfer_valuation,
	)

	t0 = perf_counter()
	out = {
		"phase": "PHASE_5C_RESUME_BASELINE",
		"company": COMPANY,
		"version": "5.3.0",
		"head_note": "after af1d80b shortage opening-state fix",
		"worker": worker_queue_status("long"),
		"active_riv": frappe.db.sql(
			"""
			SELECT COUNT(*) FROM `tabRepost Item Valuation`
			WHERE docstatus=1 AND status IN ('Queued','In Progress')
			"""
		)[0][0],
		"neg": _neg(),
		"blockers_before_sync": _blockers(),
	}

	# ---- I1 ----
	i1 = scan_i1_negative_rate(company=COMPANY, limit=2000)
	out["i1"] = {"count": i1.get("count") or len(i1.get("rows") or []), "eligible": i1.get("eligible")}

	# ---- I4 ----
	i4 = scan_i4_leftover(company=COMPANY, from_date="2026-03-21", to_date=str(nowdate()), limit=5000)
	i4_rows = i4.get("rows") or []
	i4_ids = {(r.get("item_code") or r.get("item"), r.get("warehouse")) for r in i4_rows}
	out["i4"] = {
		"findings": len(i4_rows),
		"root_identities": len(i4_ids),
		"by_status": dict(Counter(str(r.get("status") or r.get("i4_status") or "?") for r in i4_rows)),
	}

	# ---- Posting Order ----
	po = run_full_history_scan(company=COMPANY, include_no_repair=True, include_likely=True)
	po_rows = po.get("rows") or []
	by_opt = Counter()
	by_conf = Counter()
	actionable_opts = {
		"REPAIRABLE_SECONDS",
		"ELIGIBLE",
		"CROSS_TIME_REPAIRABLE",
		"SAME_TIME_REPAIRABLE",
		"MULTI_MOVE_REPAIRABLE",
		"MANUAL_APPROVAL",
	}
	actionable = 0
	exact_elig = 0
	for r in po_rows:
		opt = r.get("optimizer_status") or r.get("status") or "?"
		by_opt[opt] += 1
		by_conf[r.get("confidence") or "?"] += 1
		if opt in actionable_opts or r.get("eligible"):
			actionable += 1
		if r.get("confidence") == "EXACT" and r.get("eligible"):
			exact_elig += 1
	out["posting_order"] = {
		"raw": len(po_rows),
		"actionable": actionable,
		"EXACT_eligible": exact_elig,
		"LIKELY": by_conf.get("LIKELY", 0),
		"AMBIGUOUS": by_opt.get("AMBIGUOUS_DEPENDENCY", 0),
		"WAITING": by_opt.get("LATER_INBOUND_UNRELATED", 0),
		"REAL_STOCK_SHORTAGE": by_opt.get("REAL_STOCK_SHORTAGE", 0),
		"TOOL_LIMIT": by_opt.get("CROSS_ITEM_CONFLICT", 0)
		+ by_opt.get("MIDNIGHT_REVIEW", 0)
		+ by_opt.get("VALUATION_POISON_DEPENDENCY", 0),
		"by_optimizer_status": dict(by_opt),
		"by_confidence": dict(by_conf),
	}

	# ---- Wrong / Transfer ----
	w = scan_wrong_rates(company=COMPANY, limit=int(wrong_limit))
	wrows = [attach_plan(dict(r)) for r in (w.get("rows") or [])]
	wb = count_wrong_rate_buckets(wrows)
	out["wrong_rate"] = wb
	out["wrong_rate"]["raw"] = len(wrows)

	TRANSFER = {
		"Material Transfer",
		"Material Transfer for Manufacture",
		"Send to Subcontractor",
	}
	xfer_cls = Counter()
	xfer_cands = []
	for r in wrows:
		if (r.get("purpose") or "") not in TRANSFER:
			continue
		tr = r.get("transfer_reconstruction")
		if not tr:
			tr = reconstruct_transfer_valuation(r)
			r["transfer_reconstruction"] = tr
		cls = (tr or {}).get("classification") or "?"
		xfer_cls[cls] += 1
		if cls in (EXACT, RECONSTRUCTABLE):
			xfer_cands.append(r)
	collapse = collapse_transfer_roots(xfer_cands) if xfer_cands else {}
	out["transfer"] = {
		"findings": sum(xfer_cls.values()),
		"by_classification": dict(xfer_cls),
		"exact_recon_findings": len(xfer_cands),
		"root_chains": collapse.get("root_count") or collapse.get("roots") or len(
			{(r.get("transfer_reconstruction") or {}).get("root_voucher") or r.get("voucher") for r in xfer_cands}
		),
		"collapse": {
			k: collapse.get(k)
			for k in ("root_count", "finding_count", "by_classification", "exact", "reconstructable")
			if k in collapse
		}
		if isinstance(collapse, dict)
		else collapse,
	}

	# ---- Zero ----
	z = scan_zero_rate_rows(company=COMPANY, limit=8000)
	zrows = z.get("rows") or []
	z_by = Counter(str(r.get("status") or r.get("zero_class") or r.get("kpi_bucket") or "?") for r in zrows)
	mr = sum(
		1
		for r in zrows
		if "MATERIAL_RECEIPT" in str(r.get("status") or "")
		or "MATERIAL_RECEIPT" in str(r.get("zero_class") or "")
		or "MATERIAL_RECEIPT" in str(r.get("kpi_bucket") or "")
	)
	out["zero_rate"] = {
		"raw": z.get("raw_count") or z.get("count") or len(zrows),
		"reconstructable": z.get("reconstructable_count"),
		"waiting": z.get("waiting_upstream_count"),
		"by_status": dict(z_by),
		"material_receipt_user_review": mr,
	}

	# ---- Manufacture (from wrong/zero rows with purpose) ----
	mfg_cls = Counter()
	mfg_rows = []
	for r in wrows:
		if (r.get("purpose") or "") != "Manufacture":
			continue
		mfg_rows.append(r)
		ps = str(r.get("planner_status") or r.get("status") or "?")
		mfg_cls[ps] += 1
	# also manufacture module if available
	try:
		from erpnext_extensions.iran_accounting.historical_stock.manufacture import (
			scan_manufacture_rows,
		)

		ms = scan_manufacture_rows(company=COMPANY, limit=3000)
		out["manufacture_scan"] = {
			"raw": ms.get("count") or len(ms.get("rows") or []),
			"by_status": dict(
				Counter(str(r.get("status") or r.get("classification") or "?") for r in (ms.get("rows") or []))
			),
			"exact": ms.get("exact_count"),
			"reconstructable": ms.get("reconstructable_count"),
			"waiting": ms.get("waiting_count"),
		}
	except Exception as exc:
		out["manufacture_scan"] = {"error": str(exc), "from_wrong_purpose": dict(mfg_cls), "n": len(mfg_rows)}

	# ---- Failed RIV / Bin / GL (lightweight) ----
	fr = scan_failed_riv(limit=2000)
	out["failed_riv"] = {
		"raw": fr.get("raw_count") or fr.get("count") or len(fr.get("rows") or []),
		"actionable": fr.get("actionable_count") or fr.get("actionable"),
		"by_status": fr.get("by_status") or fr.get("by_classification"),
	}
	try:
		from erpnext_extensions.iran_accounting.historical_stock.sle_bin import scan_sle_bin

		sb = scan_sle_bin(company=COMPANY, limit=2000)
		out["broken_bin"] = {"count": sb.get("count") or len(sb.get("rows") or []), "by_class": sb.get("by_class")}
	except Exception as exc:
		out["broken_bin"] = {"error": str(exc)}
	try:
		from erpnext_extensions.iran_accounting.historical_stock.gl_integrity import scan_gl_integrity

		gl = scan_gl_integrity(company=COMPANY, limit=500)
		out["broken_gl"] = {"count": gl.get("count") or len(gl.get("rows") or []), "by_class": gl.get("by_class")}
	except Exception as exc:
		out["broken_gl"] = {"error": str(exc)}

	# ---- Master plan / root graph / PZ ----
	plan = build_master_repair_plan(company=COMPANY)
	rg = plan.get("root_cause_graph") or {}
	rg_classes = {}
	pz_findings = 0
	pz_roots = 0
	for name, blob in (rg.get("classes") or {}).items():
		if not isinstance(blob, dict):
			continue
		rg_classes[name] = {
			"total": blob.get("total"),
			"root_count": blob.get("root_count"),
			"downstream_count": blob.get("downstream_count"),
		}
		pz_findings += int(blob.get("total") or 0)
		pz_roots += int(blob.get("root_count") or 0)
	out["root_graph"] = {
		"edge_count": rg.get("edge_count"),
		"cycle_count": rg.get("cycle_count"),
		"classes": rg_classes,
		"tallied_findings": pz_findings,
		"tallied_roots": pz_roots,
	}
	out["integrity_score"] = (plan.get("safety") or {}).get("integrity_score") or plan.get(
		"integrity_score"
	)
	# dashboard integrity from last scan if present
	try:
		from erpnext_extensions.iran_accounting.historical_stock.scan import run_full_integrity_scan

		# Use cached dashboard pieces without manufacture loops — too heavy; skip full here
		pass
	except Exception:
		pass

	# ---- Blocker sync ----
	if int(sync_blockers):
		from erpnext_extensions.iran_accounting.historical_stock.blockers import scan_and_sync_blockers

		sync = scan_and_sync_blockers(company=COMPANY, include_tool_limits=True, limit=500)
		out["blocker_sync"] = {
			"created": sync.get("created"),
			"updated": sync.get("updated"),
			"user_action_required": sync.get("user_action_required"),
			"tool_limit": sync.get("tool_limit"),
			"shortage_summary": sync.get("shortage_summary"),
			"by_issue_type": sync.get("by_issue_type"),
		}
	out["blockers_after"] = _blockers()

	# Case check
	out["case_10510117"] = frappe.db.sql(
		"""
		SELECT name, status, lane, issue_type FROM `tabHistorical Repair Blocker`
		WHERE item_code=%s AND voucher_no=%s ORDER BY modified DESC LIMIT 2
		""",
		("10510117", "MAT-STE-2026-24520"),
		as_dict=True,
	)

	out["elapsed_seconds"] = round(perf_counter() - t0, 2)

	if int(save_snapshot):
		try:
			save_metrics_snapshot(
				company=COMPANY,
				dashboard={
					"phase": "PHASE_5C_RESUME_BASELINE",
					"posting_order": out["posting_order"],
					"wrong_rate": out["wrong_rate"],
					"zero_rate": out["zero_rate"],
					"transfer": out["transfer"],
					"i1": out["i1"],
					"i4": out["i4"],
					"blockers": out["blockers_after"],
					"neg": out["neg"],
				},
				timing={"elapsed_seconds": out["elapsed_seconds"]},
				source="phase5c_resume_baseline",
			)
			out["snapshot_saved"] = True
		except Exception as exc:
			out["snapshot_saved"] = False
			out["snapshot_error"] = str(exc)

	print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
	return out

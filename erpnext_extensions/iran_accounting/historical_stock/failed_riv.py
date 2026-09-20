# Copyright (c) 2026, ERPNext Extensions contributors
"""Map Failed Repost Item Valuation to upstream repair dependencies (Phase 2)."""

from __future__ import annotations

from collections import defaultdict

import frappe
from frappe.utils import cint

from erpnext_extensions.iran_accounting.historical_stock import (
	CONFIDENCE_EXACT,
	G0_HEALTHY,
	G3_UNBALANCED,
	G4_POISONED_SLE,
	RIV_DEADLOCK,
	RIV_NEGATIVE_STOCK,
	RIV_PERMANENTLY_UNSAFE,
	RIV_RAW_MATERIAL_COST,
	RIV_SAFE_TO_RETRY,
	RIV_TIMEOUT,
	RIV_UNKNOWN,
	RIV_VALUATION_INTEGRITY,
	RIV_WAITING_GL,
	RIV_WAITING_PATIENT_ZERO,
	RIV_WAITING_RATE,
	RIV_WAITING_REPLAY,
	RIV_WAITING_SLE,
	SLE_HEALTHY,
	SLE_PATIENT_ZERO_REQUIRED,
	SLE_POISONED_CHAIN,
)
from erpnext_extensions.iran_accounting.historical_stock.gl_integrity import classify_stock_entry_gl
from erpnext_extensions.iran_accounting.historical_stock.sle_bin import classify_identity


def scan_failed_riv(
	limit=400,
	item_code=None,
	warehouse=None,
	voucher=None,
	from_date=None,
	to_date=None,
	company=None,
) -> dict:
	conds = ["status='Failed'", "docstatus=1"]
	args: list = []
	if item_code:
		conds.append("item_code=%s")
		args.append(item_code)
	if warehouse:
		conds.append("warehouse=%s")
		args.append(warehouse)
	if voucher:
		conds.append("voucher_no=%s")
		args.append(voucher)
	if company:
		conds.append("company=%s")
		args.append(company)
	if from_date:
		conds.append("posting_date>=%s")
		args.append(from_date)
	if to_date:
		conds.append("posting_date<=%s")
		args.append(to_date)
	docs = frappe.db.sql(
		f"""
		SELECT name, item_code, warehouse, voucher_no, voucher_type, posting_date,
		       status, error_log, based_on, modified, company
		FROM `tabRepost Item Valuation`
		WHERE {" AND ".join(conds)}
		ORDER BY modified DESC
		LIMIT {int(limit)}
		""",
		args,
		as_dict=True,
	)
	cache = {}
	rows = [classify_failed_riv(d, _cache=cache) for d in docs]
	from erpnext_extensions.iran_accounting.historical_stock.planner import stamp_scan_result

	return stamp_scan_result({"count": len(rows), "rows": rows, "by_status": _count(rows, "riv_status")})


def classify_failed_riv(doc, _cache=None) -> dict:
	"""Classify one Failed RIV with Phase 2 dependency statuses."""
	item = getattr(doc, "item_code", None) or (doc.get("item_code") if isinstance(doc, dict) else None)
	warehouse = getattr(doc, "warehouse", None) or (doc.get("warehouse") if isinstance(doc, dict) else None)
	err = getattr(doc, "error_log", None) or (doc.get("error_log") if isinstance(doc, dict) else None) or ""
	voucher = getattr(doc, "voucher_no", None) or (doc.get("voucher_no") if isinstance(doc, dict) else None)
	name = getattr(doc, "name", None) or (doc.get("name") if isinstance(doc, dict) else None)
	posting_date = getattr(doc, "posting_date", None) or (doc.get("posting_date") if isinstance(doc, dict) else None)

	ck = (item, warehouse)
	if _cache is not None and ck in _cache:
		sle_state = _cache[ck]
	else:
		sle_state = classify_identity(item, warehouse) if item and warehouse else SLE_HEALTHY
		if _cache is not None:
			_cache[ck] = sle_state

	zero_dep = _has_zero_rate_dependency(item, warehouse)
	wrong_dep = _has_wrong_rate_dependency(item, warehouse)
	err_l = err or ""
	gl_class = G0_HEALTHY
	if voucher:
		try:
			gl_class = classify_stock_entry_gl(voucher).get("gl_class") or G0_HEALTHY
		except Exception:
			gl_class = G0_HEALTHY

	deadlock = "Deadlock found" in err_l
	timeout = any(t in err_l for t in ("Lock wait timeout", "QueryTimeout", "Lost connection", "Unable to acquire"))
	negative = "The stock for the item" in err_l or "NegativeStock" in err_l or "negative stock" in err_l.lower()
	raw_mat = "Get Raw Materials Cost from Consumption Entry" in err_l
	valuation = "I1" in err_l or "I4" in err_l or "Stock valuation integrity" in err_l
	# I1 (negative incoming rate) is repairable from its own Manufacture document — name that
	# root so the planner can route WAITING_I1 instead of parking the RIV as MANUAL forever.
	i1_root = None
	if valuation and ("(I1)" in err_l or "negative on an incoming movement" in err_l):
		i1_root = _lookup_i1_root(item, warehouse, voucher, err_l)

	patient_zero = None
	if sle_state == SLE_PATIENT_ZERO_REQUIRED:
		patient_zero = _lookup_patient_zero(item, warehouse)

	# Priority: permanent/unsafe classes → waiting upstream → transient retry
	if valuation:
		riv_status = RIV_VALUATION_INTEGRITY
	elif raw_mat:
		riv_status = RIV_RAW_MATERIAL_COST
	elif negative:
		riv_status = RIV_NEGATIVE_STOCK
	elif sle_state == SLE_PATIENT_ZERO_REQUIRED or (patient_zero and patient_zero != voucher):
		riv_status = RIV_WAITING_PATIENT_ZERO
	elif zero_dep or wrong_dep:
		riv_status = RIV_WAITING_RATE
	elif sle_state in (SLE_POISONED_CHAIN,):
		riv_status = RIV_WAITING_SLE
	elif gl_class in (G3_UNBALANCED, G4_POISONED_SLE):
		riv_status = RIV_WAITING_GL
	elif sle_state != SLE_HEALTHY:
		riv_status = RIV_WAITING_REPLAY
	elif deadlock:
		riv_status = RIV_DEADLOCK  # still retryable when chain healthy
	elif timeout:
		riv_status = RIV_TIMEOUT
	elif sle_state == SLE_HEALTHY and not zero_dep and not wrong_dep and gl_class == G0_HEALTHY:
		# Transient or unknown after healthy chain → SAFE only for deadlock/timeout/empty
		if deadlock or timeout or not err_l.strip():
			riv_status = RIV_SAFE_TO_RETRY
		elif any(t in err_l for t in ("Deadlock", "Lock wait", "QueryTimeout", "Lost connection", "Unable to acquire")):
			riv_status = RIV_SAFE_TO_RETRY
		else:
			# Healthy chain but unexplained failure — UNKNOWN (not auto-retry)
			riv_status = RIV_UNKNOWN
	else:
		riv_status = RIV_PERMANENTLY_UNSAFE

	# Deadlock/Timeout on healthy chain are SAFE_TO_RETRY
	if riv_status in (RIV_DEADLOCK, RIV_TIMEOUT) and sle_state == SLE_HEALTHY and not zero_dep and not wrong_dep:
		riv_status = RIV_SAFE_TO_RETRY

	eligible = riv_status == RIV_SAFE_TO_RETRY
	# v5.3.0 Failed RIV reconciliation against current ledger + preflight.
	from erpnext_extensions.iran_accounting.historical_stock.riv_preflight import (
		classify_failed_riv_current_impact,
	)

	reconcile = classify_failed_riv_current_impact(doc)
	# Never mass-promote HISTORICAL_ONLY / SUPERSEDED to SAFE_TO_RETRY.
	if reconcile.get("riv_reconcile_status") in (
		"HISTORICAL_ONLY",
		"SUPERSEDED_BY_SUCCESSFUL_REPAIR",
	):
		eligible = False
		if riv_status == RIV_SAFE_TO_RETRY:
			riv_status = RIV_UNKNOWN
	elif reconcile.get("riv_reconcile_status") == "SAFE_TO_RETRY" and riv_status == RIV_UNKNOWN:
		riv_status = RIV_SAFE_TO_RETRY
		eligible = True
	elif str(reconcile.get("riv_reconcile_status") or "").startswith("BLOCKED"):
		eligible = False

	return {
		"topic": "FAILED_RIV",
		"riv_name": name,
		"item": item,
		"warehouse": warehouse,
		"voucher": voucher,
		"posting_date": str(posting_date) if posting_date else None,
		"error_head": (err_l or "")[:240],
		"sle_state": sle_state,
		"gl_class": gl_class,
		"zero_rate_dependency": zero_dep,
		"wrong_rate_dependency": wrong_dep,
		"patient_zero": patient_zero,
		"i1_root": i1_root,
		"rate_status": RIV_WAITING_RATE if (zero_dep or wrong_dep) else "HEALTHY",
		"sle_status": sle_state,
		"gl_status": gl_class,
		"replay_status": RIV_WAITING_REPLAY if sle_state not in (SLE_HEALTHY,) else "COMPLETE",
		"riv_status": riv_status,
		"status": riv_status,
		"eligible": eligible,
		"confidence": CONFIDENCE_EXACT if eligible else "LIKELY",
		"riv_reconcile_status": reconcile.get("riv_reconcile_status"),
		"riv_reconcile": reconcile,
	}


def retry_failed_riv(riv_name: str, *, dry_run=True) -> dict:
	preview = classify_failed_riv(frappe.get_doc("Repost Item Valuation", riv_name))
	from erpnext_extensions.iran_accounting.historical_stock.planner import attach_plan, READY_STATUSES

	planned = attach_plan(preview)
	if planned["planner_status"] not in READY_STATUSES and planned["planner_status"] != "READY":
		return {**planned, "dry_run": dry_run, "written": False, "blocked": True}
	if dry_run:
		return {**planned, "dry_run": True, "written": False}
	doc = frappe.get_doc("Repost Item Valuation", riv_name)
	if cint(doc.docstatus) != 1:
		frappe.throw(f"RIV {riv_name} is not submitted")
	if hasattr(doc, "restart_reposting"):
		doc.restart_reposting()
	else:
		doc.db_set("status", "Queued")
	frappe.db.commit()
	after = classify_failed_riv(frappe.get_doc("Repost Item Valuation", riv_name))
	return {**preview, "written": True, "status": "QUEUED", "after_status": after.get("riv_status"), "after_docstatus": after}


def _has_zero_rate_dependency(item, warehouse) -> bool:
	if not item:
		return False
	n = frappe.db.sql(
		"""
		SELECT COUNT(*) FROM `tabStock Entry Detail` sed
		JOIN `tabStock Entry` se ON se.name = sed.parent
		WHERE se.docstatus=1 AND sed.item_code=%s
		  AND (sed.s_warehouse=%s OR sed.t_warehouse=%s)
		  AND ABS(sed.qty) > 0.0001
		  AND ABS(IFNULL(sed.basic_rate,0)) < 0.0001
		  AND IFNULL(sed.allow_zero_valuation_rate,0)=0
		""",
		(item, warehouse, warehouse),
	)[0][0]
	return n > 0


def _has_wrong_rate_dependency(item, warehouse) -> bool:
	"""True when SLE txn rate is zero while SVD implies a nonzero rate (Wrong Rate EXACT)."""
	if not item or not warehouse:
		return False
	n = frappe.db.sql(
		"""
		SELECT COUNT(*) FROM `tabStock Ledger Entry`
		WHERE item_code=%s AND warehouse=%s AND is_cancelled=0
		  AND ABS(IFNULL(stock_value_difference,0)) > 0.5
		  AND ABS(IFNULL(actual_qty,0)) > 0.0001
		  AND (
		    (actual_qty < 0 AND ABS(IFNULL(outgoing_rate,0)) < 0.0001)
		    OR (actual_qty > 0 AND ABS(IFNULL(incoming_rate,0)) < 0.0001)
		  )
		LIMIT 1
		""",
		(item, warehouse),
	)[0][0]
	return n > 0


def _lookup_i1_root(item, warehouse, voucher=None, error_log=None):
	"""Manufacture voucher carrying the negative incoming rate.

	The guard message names the offending voucher directly, and that is the only reliable
	root: these reposts are ``based_on="Item and Warehouse"`` with no ``voucher_no``, and the
	negative incoming rate sits on the finished-good identity — not on the raw-material
	identity being reposted. Identity lookup is only a fallback.
	"""
	import re

	if error_log:
		match = re.search(r"voucher_no=(\S+)", error_log)
		if match:
			return match.group(1).strip()
	try:
		from erpnext_extensions.iran_accounting.historical_stock.i1_repair import i1_root_for_identity

		return i1_root_for_identity(item, warehouse) or voucher
	except Exception:
		return voucher


def _lookup_patient_zero(item, warehouse):
	try:
		from erpnext_extensions.iran_accounting.historical_stock.patient_zero import find_patient_zero

		pz = find_patient_zero(item, warehouse)
		if isinstance(pz, dict):
			return pz.get("voucher_no") or pz.get("voucher")
		return pz
	except Exception:
		return None


def _count(rows, key):
	out = defaultdict(int)
	for row in rows:
		out[str(row.get(key) or "")] += 1
	return dict(out)

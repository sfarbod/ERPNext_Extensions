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
	"""Scan Failed RIV with Stage-1 cheap screen + Stage-2 expensive survivors.

	Stage 1 (bulk metadata / current SLE):
	  SUPERSEDED_BY_SUCCESSFUL_REPAIR, HISTORICAL_ONLY, CURRENT_LEDGER_IMPACT
	  — no per-row RIV preflight / GL load / zero-rate dependency probes.

	Stage 2 (survivors only):
	  full ``classify_failed_riv`` for rows that may still be actionable
	  (CURRENT_LEDGER_IMPACT and MANUAL_REVIEW).
	"""
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
		       status, error_log, based_on, modified, company, creation
		FROM `tabRepost Item Valuation`
		WHERE {" AND ".join(conds)}
		ORDER BY modified DESC
		LIMIT {int(limit)}
		""",
		args,
		as_dict=True,
	)
	stage1 = stage1_screen_failed_rivs(docs)
	cache = stage1["cache"]
	rows = list(stage1["cheap_rows"])
	expensive_docs = stage1["expensive_docs"]
	for d in expensive_docs:
		rows.append(classify_failed_riv(d, _cache=cache))

	by_reconcile = _count(rows, "riv_reconcile_status")
	actionable = [
		r
		for r in rows
		if (r.get("riv_reconcile_status") or "")
		not in (
			"HISTORICAL_ONLY",
			"SUPERSEDED_BY_SUCCESSFUL_REPAIR",
		)
	]
	from erpnext_extensions.iran_accounting.historical_stock.planner import stamp_scan_result

	return stamp_scan_result(
		{
			"count": len(rows),
			"raw_count": len(rows),
			"actionable_count": len(actionable),
			"rows": rows,
			"by_status": _count(rows, "riv_status"),
			"by_reconcile": by_reconcile,
			"stage1": {
				"docs": len(docs),
				"cheap": stage1["cheap_count"],
				"historical_only": stage1["historical_only"],
				"superseded": stage1["superseded"],
				"current_impact_cheap": stage1["current_impact_cheap"],
				"expensive": len(expensive_docs),
			},
		}
	)


def stage1_screen_failed_rivs(docs: list) -> dict:
	"""Bulk Stage-1 screen: SUPERSEDED / HISTORICAL_ONLY without N+1 preflight.

	Survivors (unhealthy SLE / missing identity) go to Stage-2 full classify.
	"""
	from erpnext_extensions.iran_accounting.historical_stock import SLE_HEALTHY
	from erpnext_extensions.iran_accounting.historical_stock.sle_bin import classify_identity

	docs = list(docs or [])
	cache: dict = {}
	# Unique identities
	pairs = sorted(
		{
			(d.get("item_code") if isinstance(d, dict) else getattr(d, "item_code", None),
			 d.get("warehouse") if isinstance(d, dict) else getattr(d, "warehouse", None))
			for d in docs
			if (d.get("item_code") if isinstance(d, dict) else getattr(d, "item_code", None))
			and (d.get("warehouse") if isinstance(d, dict) else getattr(d, "warehouse", None))
		}
	)
	sle_by_pair: dict[tuple, str] = {}
	for item, wh in pairs:
		ck = ("sle", item, wh)
		if ck in cache:
			sle_by_pair[(item, wh)] = cache[ck]
			continue
		state = classify_identity(item, wh)
		cache[ck] = state
		sle_by_pair[(item, wh)] = state

	# Bulk: latest Completed RIV creation per identity (for SUPERSEDED).
	later_map: dict[tuple, str] = {}
	if pairs:
		# One query: all Completed RIVs for these identities, then compare in Python.
		# Chunk to keep IN lists bounded.
		chunk = 200
		for i in range(0, len(pairs), chunk):
			part = pairs[i : i + chunk]
			placeholders = ",".join(["(%s,%s)"] * len(part))
			flat = [x for p in part for x in p]
			rows = frappe.db.sql(
				f"""
				SELECT item_code, warehouse, name, creation
				FROM `tabRepost Item Valuation`
				WHERE status='Completed' AND docstatus=1
				  AND (item_code, warehouse) IN ({placeholders})
				ORDER BY creation DESC
				""",
				flat,
				as_dict=True,
			)
			for r in rows:
				key = (r.item_code, r.warehouse)
				# Keep the newest Completed only.
				if key not in later_map:
					later_map[key] = (r.name, r.creation)

	cheap_rows = []
	expensive_docs = []
	historical_only = superseded = current_impact_cheap = 0
	for d in docs:
		item = d.get("item_code") if isinstance(d, dict) else getattr(d, "item_code", None)
		wh = d.get("warehouse") if isinstance(d, dict) else getattr(d, "warehouse", None)
		name = d.get("name") if isinstance(d, dict) else getattr(d, "name", None)
		err = (d.get("error_log") if isinstance(d, dict) else getattr(d, "error_log", None)) or ""
		posting_date = d.get("posting_date") if isinstance(d, dict) else getattr(d, "posting_date", None)
		voucher = d.get("voucher_no") if isinstance(d, dict) else getattr(d, "voucher_no", None)
		creation = d.get("creation") if isinstance(d, dict) else getattr(d, "creation", None)

		if not item or not wh:
			expensive_docs.append(d)
			continue

		sle_state = sle_by_pair.get((item, wh), SLE_HEALTHY)
		cache[("sle", item, wh)] = sle_state
		later = later_map.get((item, wh))
		if later and creation and str(later[1]) > str(creation):
			cache[("later_ok", item, wh, name)] = ((later[0],),)
			reconcile_status = "SUPERSEDED_BY_SUCCESSFUL_REPAIR"
			later_completed = later[0]
			superseded += 1
		elif later and not creation:
			# No creation on Failed row — still treat Completed as superseding if present.
			cache[("later_ok", item, wh, name)] = ((later[0],),)
			reconcile_status = "SUPERSEDED_BY_SUCCESSFUL_REPAIR"
			later_completed = later[0]
			superseded += 1
		else:
			cache[("later_ok", item, wh, name)] = ()
			later_completed = None
			if sle_state == SLE_HEALTHY:
				reconcile_status = "HISTORICAL_ONLY"
				historical_only += 1
			else:
				# Unhealthy SLE — Stage-2 full classify (error-class + waiting deps).
				expensive_docs.append(d)
				current_impact_cheap += 1
				continue

		# Cheap row: historical / superseded — not actionable; skip GL/zero/wrong/preflight.
		row = {
			"topic": "FAILED_RIV",
			"riv_name": name,
			"item": item,
			"warehouse": wh,
			"voucher": voucher,
			"posting_date": str(posting_date) if posting_date else None,
			"error_head": (err or "")[:240],
			"sle_state": sle_state,
			"gl_class": G0_HEALTHY,
			"zero_rate_dependency": False,
			"wrong_rate_dependency": False,
			"patient_zero": None,
			"i1_root": None,
			"rate_status": "HEALTHY",
			"sle_status": sle_state,
			"gl_status": G0_HEALTHY,
			"replay_status": "COMPLETE",
			"riv_status": RIV_UNKNOWN,
			"status": RIV_UNKNOWN,
			"eligible": False,
			"confidence": "LIKELY",
			"riv_reconcile_status": reconcile_status,
			"riv_reconcile": {
				"riv": name,
				"item": item,
				"warehouse": wh,
				"riv_reconcile_status": reconcile_status,
				"sle_state": sle_state,
				"later_completed": later_completed,
				"preflight": None,
				"stage": "cheap",
			},
			"stage": "cheap",
			"actionable": False,
		}
		cheap_rows.append(row)

	return {
		"cache": cache,
		"cheap_rows": cheap_rows,
		"expensive_docs": expensive_docs,
		"cheap_count": len(cheap_rows),
		"historical_only": historical_only,
		"superseded": superseded,
		"current_impact_cheap": current_impact_cheap,
	}


def classify_failed_riv(doc, _cache=None) -> dict:
	"""Classify one Failed RIV with Phase 2 dependency statuses."""
	item = getattr(doc, "item_code", None) or (doc.get("item_code") if isinstance(doc, dict) else None)
	warehouse = getattr(doc, "warehouse", None) or (doc.get("warehouse") if isinstance(doc, dict) else None)
	err = getattr(doc, "error_log", None) or (doc.get("error_log") if isinstance(doc, dict) else None) or ""
	voucher = getattr(doc, "voucher_no", None) or (doc.get("voucher_no") if isinstance(doc, dict) else None)
	name = getattr(doc, "name", None) or (doc.get("name") if isinstance(doc, dict) else None)
	posting_date = getattr(doc, "posting_date", None) or (doc.get("posting_date") if isinstance(doc, dict) else None)

	ck = ("sle", item, warehouse) if item and warehouse else None
	if _cache is not None and ck and ck in _cache:
		sle_state = _cache[ck]
	else:
		# Backward-compatible cache key used by older callers: (item, warehouse)
		legacy_ck = (item, warehouse)
		if _cache is not None and legacy_ck in _cache:
			sle_state = _cache[legacy_ck]
		else:
			sle_state = classify_identity(item, warehouse) if item and warehouse else SLE_HEALTHY
			if _cache is not None:
				if ck:
					_cache[ck] = sle_state
				_cache[legacy_ck] = sle_state

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

	reconcile = classify_failed_riv_current_impact(
		doc, _cache=_cache, sle_state=sle_state, skip_preflight_when_healthy=True
	)
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

	actionable = reconcile.get("riv_reconcile_status") not in (
		"HISTORICAL_ONLY",
		"SUPERSEDED_BY_SUCCESSFUL_REPAIR",
	)

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
		"stage": reconcile.get("stage") or "expensive",
		"actionable": actionable,
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

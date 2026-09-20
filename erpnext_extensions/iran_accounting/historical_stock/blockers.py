# Copyright (c) 2026, ERPNext Extensions contributors
"""Historical Repair Blocker — USER_ACTION_REQUIRED vs TOOL_LIMIT (v5.3.0).

USER_ACTION_REQUIRED: genuine business/data blockers needing human judgment.
TOOL_LIMIT / CODE_REPAIR_REQUIRED: deterministic cases the tool cannot yet
handle — never classified as user blockers.
"""

from __future__ import annotations

from datetime import datetime

import frappe
from frappe.utils import now_datetime

# Workflow statuses
STATUS_OPEN = "OPEN"
STATUS_UNDER_REVIEW = "UNDER_REVIEW"
STATUS_USER_FIXED = "USER_FIXED"
STATUS_READY_TO_RECHECK = "READY_TO_RECHECK"
STATUS_RESOLVED = "RESOLVED"
STATUS_IGNORED_WITH_REASON = "IGNORED_WITH_REASON"

# Classification lane
LANE_USER_ACTION = "USER_ACTION_REQUIRED"
LANE_TOOL_LIMIT = "TOOL_LIMIT"

# Issue types (user blockers)
HISTORICAL_NEGATIVE_STOCK = "HISTORICAL_NEGATIVE_STOCK"
AMBIGUOUS_POSTING_ORDER = "AMBIGUOUS_POSTING_ORDER"
MISSING_SOURCE_TRANSACTION = "MISSING_SOURCE_TRANSACTION"
MISSING_VALUATION_SOURCE = "MISSING_VALUATION_SOURCE"
UNRESOLVED_BATCH_CHAIN = "UNRESOLVED_BATCH_CHAIN"
MANUFACTURE_DEPENDENCY = "MANUFACTURE_DEPENDENCY"
POISONED_UPSTREAM_VALUATION = "POISONED_UPSTREAM_VALUATION"
UNRECONSTRUCTABLE_RATE = "UNRECONSTRUCTABLE_RATE"
DOCUMENT_INCONSISTENCY = "DOCUMENT_INCONSISTENCY"
NEGATIVE_STOCK_REQUIRES_USER = "NEGATIVE_STOCK_REQUIRES_USER"
RIV_PREFLIGHT_BLOCKED = "RIV_PREFLIGHT_BLOCKED"
EXPECTED_GL_UNBALANCED = "EXPECTED_GL_UNBALANCED"
DEPENDENCY_CYCLE_UNRESOLVED = "DEPENDENCY_CYCLE_UNRESOLVED"
CONVERTED_DATA_AMBIGUITY = "CONVERTED_DATA_AMBIGUITY"
OTHER_MANUAL_REVIEW = "OTHER_MANUAL_REVIEW"

USER_ISSUE_TYPES = (
	HISTORICAL_NEGATIVE_STOCK,
	AMBIGUOUS_POSTING_ORDER,
	MISSING_SOURCE_TRANSACTION,
	MISSING_VALUATION_SOURCE,
	UNRESOLVED_BATCH_CHAIN,
	MANUFACTURE_DEPENDENCY,
	POISONED_UPSTREAM_VALUATION,
	UNRECONSTRUCTABLE_RATE,
	DOCUMENT_INCONSISTENCY,
	NEGATIVE_STOCK_REQUIRES_USER,
	RIV_PREFLIGHT_BLOCKED,
	EXPECTED_GL_UNBALANCED,
	DEPENDENCY_CYCLE_UNRESOLVED,
	CONVERTED_DATA_AMBIGUITY,
	OTHER_MANUAL_REVIEW,
)

SEVERITY_CRITICAL = "CRITICAL"
SEVERITY_HIGH = "HIGH"
SEVERITY_MEDIUM = "MEDIUM"
SEVERITY_LOW = "LOW"


def build_blocker_key(
	*,
	issue_type: str,
	item_code: str | None = None,
	warehouse: str | None = None,
	batch_no: str | None = None,
	voucher_no: str | None = None,
	lane: str = LANE_USER_ACTION,
) -> str:
	parts = [
		lane,
		issue_type or "OTHER",
		item_code or "",
		warehouse or "",
		batch_no or "",
		voucher_no or "",
	]
	return "|".join(parts)


def upsert_blocker(payload: dict, *, dry_run: bool = False) -> dict:
	"""Insert or update a blocker by blocker_key. Never auto-marks USER_FIXED."""
	key = payload.get("blocker_key") or build_blocker_key(
		issue_type=payload.get("issue_type") or OTHER_MANUAL_REVIEW,
		item_code=payload.get("item_code"),
		warehouse=payload.get("warehouse"),
		batch_no=payload.get("batch_no"),
		voucher_no=payload.get("voucher_no"),
		lane=payload.get("lane") or LANE_USER_ACTION,
	)
	payload = dict(payload)
	payload["blocker_key"] = key
	payload.setdefault("lane", LANE_USER_ACTION)
	payload.setdefault("status", STATUS_OPEN)
	payload.setdefault("last_scanned_at", now_datetime())
	payload.setdefault("can_recheck_after_user_action", 1 if payload["lane"] == LANE_USER_ACTION else 0)

	existing = frappe.db.get_value("Historical Repair Blocker", {"blocker_key": key}, "name")
	if dry_run:
		return {"dry_run": True, "blocker_key": key, "exists": bool(existing), "payload": payload}

	if existing:
		doc = frappe.get_doc("Historical Repair Blocker", existing)
		# Preserve workflow status unless still OPEN / READY_TO_RECHECK.
		preserve = doc.status in (
			STATUS_UNDER_REVIEW,
			STATUS_USER_FIXED,
			STATUS_RESOLVED,
			STATUS_IGNORED_WITH_REASON,
		)
		old_status = doc.status
		doc.update({k: v for k, v in payload.items() if k not in ("name", "creation", "owner") and v is not None})
		if preserve:
			doc.status = old_status
		doc.last_scanned_at = now_datetime()
		doc.save(ignore_permissions=True)
		return {"name": doc.name, "blocker_key": key, "updated": True, "status": doc.status}

	doc = frappe.get_doc({"doctype": "Historical Repair Blocker", **payload})
	doc.insert(ignore_permissions=True)
	return {"name": doc.name, "blocker_key": key, "created": True, "status": doc.status}


def scan_and_sync_blockers(company=None, *, include_tool_limits: bool = True, limit: int = 500) -> dict:
	"""Build root blockers from negative-stock + ambiguous PO + tool-limit samples."""
	from erpnext_extensions.iran_accounting.historical_stock.negative_stock_report import (
		HISTORICAL_NEGATIVE_REQUIRES_USER,
		build_negative_stock_root_report,
	)

	scanned_at = now_datetime()
	created = updated = 0
	rows_out = []

	neg = build_negative_stock_root_report(company=company, limit_chains=min(limit, 300))
	for chain in neg.get("chains") or []:
		if chain.get("classification") != HISTORICAL_NEGATIVE_REQUIRES_USER:
			continue
		explanation = (
			f"Item {chain.get('item_code')} ({chain.get('item_name') or ''}) / "
			f"Batch {chain.get('batch_no') or '(none)'} became negative in warehouse "
			f"{chain.get('warehouse')} at {chain.get('first_negative_datetime')} because "
			f"{chain.get('voucher_type')} {chain.get('voucher_no')} moved "
			f"{chain.get('transaction_qty')} while qty_before was {chain.get('qty_before')}. "
			f"Next inbound: {chain.get('next_inbound_voucher') or 'none'} "
			f"(gap {chain.get('time_gap_to_next_inbound_seconds')}s). "
			"The system cannot prove whether chronology or a missing receipt is the business truth. "
			"Review the first negative voucher and next inbound; correct operational data, then Recheck."
		)
		payload = {
			"lane": LANE_USER_ACTION,
			"status": STATUS_OPEN,
			"severity": SEVERITY_HIGH,
			"issue_type": NEGATIVE_STOCK_REQUIRES_USER,
			"root_cause": "Historical qty_after_transaction went negative with no deterministic chronology proof",
			"company": company,
			"item_code": chain.get("item_code"),
			"item_name": chain.get("item_name"),
			"batch_no": chain.get("batch_no"),
			"warehouse": chain.get("warehouse"),
			"voucher_type": chain.get("voucher_type"),
			"voucher_no": chain.get("voucher_no"),
			"posting_date": chain.get("first_negative_posting_date"),
			"posting_time": chain.get("first_negative_posting_time"),
			"current_qty": chain.get("qty_after"),
			"expected_qty": 0,
			"first_bad_sle": chain.get("first_sle"),
			"previous_sle": None,
			"next_inbound": chain.get("next_inbound_voucher"),
			"related_voucher": chain.get("previous_voucher"),
			"dependency_root": chain.get("voucher_no"),
			"affected_downstream_count": max(0, int(chain.get("negative_sle_count") or 1) - 1),
			"recommended_user_action": (
				"Confirm whether the next inbound should precede this consumption, "
				"or whether a missing receipt/transfer must be entered. Do not force timestamps blindly."
			),
			"why_automatic_repair_is_unsafe": (
				"No EXACT chronology proof; reordering or inventing stock would invent business truth."
			),
			"can_recheck_after_user_action": 1,
			"user_facing_explanation": explanation,
			"last_scanned_at": scanned_at,
			"payload_json": frappe.as_json(chain),
		}
		res = upsert_blocker(payload)
		created += int(bool(res.get("created")))
		updated += int(bool(res.get("updated")))
		rows_out.append({**payload, "name": res.get("name")})

	# Ambiguous posting-order roots (sample) — USER lane only when truly AMBIGUOUS.
	try:
		from erpnext_extensions.iran_accounting.stock_posting_order.scanner import run_full_history_scan

		from erpnext_extensions.iran_accounting.historical_stock.planner import (
			READY_STATUSES,
			attach_plan,
		)

		po = run_full_history_scan(company=company)
		amb = 0
		plan_cache = {}
		for raw in po.get("rows") or []:
			r = attach_plan(dict(raw), cache=plan_cache)
			opt = str(r.get("optimizer_status") or r.get("status") or "")
			conf = str(r.get("confidence") or r.get("repair_confidence") or "")
			raw_conf = str(r.get("raw_confidence") or raw.get("confidence") or "")
			if opt in ("NO_REPAIR_NEEDED", "ALREADY_ORDERED", "HEALTHY"):
				continue
			# Promoted / EXACT auto-repairable rows are not blockers.
			if conf == "EXACT" or (
				r.get("eligible")
				and str(r.get("planner_status") or "") in READY_STATUSES
				and int(r.get("sql_updates") or 0) > 0
			):
				continue
			if "EXACT" in conf or "EXACT" in opt:
				continue
			if "AMBIGUOUS" not in conf and "AMBIGUOUS" not in opt and "LIKELY" not in conf and "LIKELY" not in raw_conf:
				continue
			if "LIKELY" in conf or "LIKELY" in raw_conf:
				# Unpromoted LIKELY is TOOL_LIMIT until classifier upgrades — not a user blocker.
				if not include_tool_limits:
					continue
				lane = LANE_TOOL_LIMIT
				issue = AMBIGUOUS_POSTING_ORDER
				sev = SEVERITY_MEDIUM
				why = "Classifier confidence is LIKELY — needs deterministic upgrade in tool, not user guesswork."
			else:
				lane = LANE_USER_ACTION
				issue = AMBIGUOUS_POSTING_ORDER
				sev = SEVERITY_MEDIUM
				why = "Posting order is AMBIGUOUS; multiple chronologies are plausible."
			payload = {
				"lane": lane,
				"status": STATUS_OPEN,
				"severity": sev,
				"issue_type": issue,
				"root_cause": conf or opt,
				"company": company or r.get("company"),
				"item_code": r.get("item") or r.get("item_code"),
				"warehouse": r.get("warehouse"),
				"batch_no": r.get("batch") or r.get("batch_no"),
				"voucher_type": "Stock Entry",
				"voucher_no": (
					r.get("outbound_document")
					or r.get("voucher")
					or r.get("voucher_no")
				),
				"posting_date": r.get("posting_date"),
				"posting_time": r.get("posting_time"),
				"work_order": r.get("work_order"),
				"job_card": r.get("job_card"),
				"dependency_root": (
					r.get("outbound_document")
					or r.get("voucher")
					or r.get("voucher_no")
				),
				"affected_downstream_count": int(r.get("affected_count") or 0),
				"recommended_user_action": (
					"Review Work Order / Job Card / Batch flow and confirm intended posting order."
					if lane == LANE_USER_ACTION
					else "No user action — wait for tool classifier upgrade to EXACT/RECONSTRUCTABLE."
				),
				"why_automatic_repair_is_unsafe": why,
				"can_recheck_after_user_action": 1 if lane == LANE_USER_ACTION else 0,
				"user_facing_explanation": why,
				"last_scanned_at": scanned_at,
			}
			res = upsert_blocker(payload)
			created += int(bool(res.get("created")))
			updated += int(bool(res.get("updated")))
			rows_out.append({**payload, "name": res.get("name")})
			amb += 1
			if amb >= 80:
				break
	except Exception as exc:
		frappe.log_error(f"blocker PO scan: {exc}", "Historical Repair Blocker")

	# Posting Order REAL_STOCK_SHORTAGE → USER_ACTION_REQUIRED (true historical shortage).
	try:
		from erpnext_extensions.iran_accounting.stock_posting_order.scanner import run_full_history_scan
		from erpnext_extensions.iran_accounting.historical_stock.planner import attach_plan

		po = run_full_history_scan(company=company)
		shortage_n = 0
		for raw in po.get("rows") or []:
			opt = str(raw.get("optimizer_status") or raw.get("status") or "")
			if opt not in ("REAL_STOCK_SHORTAGE", "INSUFFICIENT_STOCK"):
				continue
			r = attach_plan(dict(raw))
			item = r.get("item") or r.get("item_code")
			wh = r.get("warehouse")
			payload = {
				"lane": LANE_USER_ACTION,
				"status": STATUS_OPEN,
				"severity": SEVERITY_HIGH,
				"issue_type": HISTORICAL_NEGATIVE_STOCK,
				"root_cause": "REAL_STOCK_SHORTAGE — ledger goes negative; chronology rewrite refused",
				"company": company or r.get("company"),
				"item_code": item,
				"warehouse": wh,
				"batch_no": r.get("batch") or r.get("batch_no"),
				"voucher_type": "Stock Entry",
				"voucher_no": r.get("outbound_document") or r.get("voucher") or r.get("voucher_no"),
				"posting_date": r.get("posting_date") or str(r.get("current_outbound_time") or "")[:10],
				"posting_time": r.get("posting_time"),
				"current_qty": r.get("min_qty_before") or r.get("qty_before"),
				"expected_qty": r.get("min_qty_after") or r.get("qty_after"),
				"dependency_root": r.get("outbound_document") or r.get("voucher"),
				"affected_downstream_count": int(r.get("affected_count") or 0),
				"recommended_user_action": (
					f"Investigate shortage for Item {item} in {wh}: first negative outbound "
					f"{r.get('outbound_document')}. Confirm missing inbound / wrong batch / "
					"wrong warehouse, or approve a business correction. Do not invent stock."
				),
				"why_automatic_repair_is_unsafe": (
					"Timestamp rewrite cannot create missing stock. This is a genuine historical "
					"shortage (USER_ACTION_REQUIRED), not a tool limitation."
				),
				"can_recheck_after_user_action": 1,
				"user_facing_explanation": (
					"Stock went negative historically. Review prior receipts/transfers and "
					"correct the business document if stock was mis-posted."
				),
				"last_scanned_at": scanned_at,
				"payload_json": frappe.as_json(
					{
						"optimizer_status": opt,
						"confidence": r.get("confidence"),
						"manual_reason": "MANUAL_NEGATIVE_STOCK_HISTORY",
						"inbound_document": r.get("inbound_document"),
						"min_qty_before": r.get("min_qty_before"),
						"min_qty_after": r.get("min_qty_after"),
					}
				),
			}
			res = upsert_blocker(payload)
			created += int(bool(res.get("created")))
			updated += int(bool(res.get("updated")))
			rows_out.append({**payload, "name": res.get("name")})
			shortage_n += 1
			if shortage_n >= 150:
				break
	except Exception as exc:
		frappe.log_error(f"blocker PO shortage scan: {exc}", "Historical Repair Blocker")

	# Material Receipt zero-rate without authoritative source → USER_ACTION_REQUIRED.
	try:
		from erpnext_extensions.iran_accounting.historical_stock.zero_rate import scan_zero_rate_rows
		from erpnext_extensions.iran_accounting.historical_stock.transaction_semantics import (
			material_receipt_user_message,
		)

		zr = scan_zero_rate_rows(company=company, limit=min(limit, 2000))
		mr_n = 0
		for r in zr.get("rows") or []:
			if (
				r.get("status") != "MATERIAL_RECEIPT_ZERO_RATE_USER_REVIEW"
				and r.get("zero_class") != "MATERIAL_RECEIPT_ZERO_RATE_USER_REVIEW"
				and r.get("kpi_bucket") != "MATERIAL_RECEIPT_ZERO_USER_REVIEW"
			):
				continue
			msg = material_receipt_user_message()
			payload = {
				"lane": LANE_USER_ACTION,
				"status": STATUS_OPEN,
				"severity": SEVERITY_MEDIUM,
				"issue_type": MISSING_VALUATION_SOURCE,
				"root_cause": "Material Receipt has no authoritative upstream valuation source",
				"company": company or r.get("company"),
				"item_code": r.get("item") or r.get("item_code"),
				"warehouse": r.get("warehouse") or r.get("t_warehouse"),
				"batch_no": r.get("batch") or r.get("batch_no"),
				"voucher_type": "Stock Entry",
				"voucher_no": r.get("voucher") or r.get("voucher_no"),
				"posting_date": r.get("posting_date"),
				"posting_time": r.get("posting_time"),
				"current_qty": r.get("qty"),
				"expected_qty": r.get("qty"),
				"dependency_root": r.get("voucher") or r.get("voucher_no"),
				"affected_downstream_count": 0,
				"recommended_user_action": msg,
				"why_automatic_repair_is_unsafe": (
					"Inventing a Material Receipt rate would invent business truth; "
					"no reconstructable upstream source exists."
				),
				"can_recheck_after_user_action": 1,
				"user_facing_explanation": msg,
				"last_scanned_at": scanned_at,
				"payload_json": frappe.as_json(
					{
						"purpose": r.get("purpose"),
						"kpi_bucket": r.get("kpi_bucket"),
						"scrap_warehouse_context": r.get("scrap_warehouse_context"),
					}
				),
			}
			res = upsert_blocker(payload)
			created += int(bool(res.get("created")))
			updated += int(bool(res.get("updated")))
			rows_out.append({**payload, "name": res.get("name")})
			mr_n += 1
			if mr_n >= 200:
				break
	except Exception as exc:
		frappe.log_error(f"blocker Material Receipt zero scan: {exc}", "Historical Repair Blocker")

	frappe.db.commit()
	user_n = sum(1 for r in rows_out if r.get("lane") == LANE_USER_ACTION)
	tool_n = sum(1 for r in rows_out if r.get("lane") == LANE_TOOL_LIMIT)
	return {
		"scanned_at": str(scanned_at),
		"created": created,
		"updated": updated,
		"count": len(rows_out),
		"user_action_required": user_n,
		"tool_limit": tool_n,
		"rows": rows_out[:200],
		"by_issue_type": _count(rows_out, "issue_type"),
		"by_lane": _count(rows_out, "lane"),
		"by_severity": _count(rows_out, "severity"),
	}


def list_blockers(
	company=None,
	lane=None,
	issue_type=None,
	item_code=None,
	warehouse=None,
	batch_no=None,
	status=None,
	severity=None,
	limit: int = 500,
) -> dict:
	conds = ["1=1"]
	args: list = []
	if company:
		conds.append("company=%s")
		args.append(company)
	if lane:
		conds.append("lane=%s")
		args.append(lane)
	if issue_type:
		conds.append("issue_type=%s")
		args.append(issue_type)
	if item_code:
		conds.append("item_code=%s")
		args.append(item_code)
	if warehouse:
		conds.append("warehouse=%s")
		args.append(warehouse)
	if batch_no:
		conds.append("batch_no=%s")
		args.append(batch_no)
	if status:
		conds.append("status=%s")
		args.append(status)
	if severity:
		conds.append("severity=%s")
		args.append(severity)
	rows = frappe.db.sql(
		f"""
		SELECT name, blocker_key, lane, status, severity, issue_type, root_cause,
		       company, item_code, item_name, batch_no, warehouse,
		       voucher_type, voucher_no, posting_date, posting_time,
		       work_order, job_card, related_voucher,
		       current_qty, expected_qty, current_rate, expected_rate,
		       current_stock_value, expected_stock_value,
		       first_bad_sle, previous_sle, next_sle, next_inbound,
		       dependency_root, affected_downstream_count,
		       recommended_user_action, why_automatic_repair_is_unsafe,
		       can_recheck_after_user_action, user_facing_explanation, last_scanned_at
		FROM `tabHistorical Repair Blocker`
		WHERE {" AND ".join(conds)}
		ORDER BY
		  FIELD(severity, 'CRITICAL','HIGH','MEDIUM','LOW'),
		  item_code, warehouse, IFNULL(batch_no,''), posting_date, posting_time
		LIMIT {int(limit)}
		""",
		args,
		as_dict=True,
	)
	return {
		"count": len(rows),
		"rows": rows,
		"by_issue_type": _count(rows, "issue_type"),
		"by_lane": _count(rows, "lane"),
		"by_status": _count(rows, "status"),
		"user_action_required": sum(1 for r in rows if r.get("lane") == LANE_USER_ACTION),
		"tool_limit": sum(1 for r in rows if r.get("lane") == LANE_TOOL_LIMIT),
	}


def recheck_blocker(name: str) -> dict:
	"""Rescan the blocked identity/root after user action. Never auto USER_FIXED."""
	doc = frappe.get_doc("Historical Repair Blocker", name)
	if doc.lane == LANE_TOOL_LIMIT:
		return {
			"name": name,
			"status": doc.status,
			"eligible_for_auto_repair": False,
			"message": "TOOL_LIMIT — improve v5.3.0 code; user recheck is not applicable",
		}

	# Re-run negative stock / identity health for this root.
	from erpnext_extensions.iran_accounting.historical_stock.sle_bin import classify_identity

	sle_state = None
	if doc.item_code and doc.warehouse:
		sle_state = classify_identity(doc.item_code, doc.warehouse)

	still_neg = False
	if doc.item_code and doc.warehouse:
		still_neg = bool(
			frappe.db.sql(
				"""
				SELECT 1 FROM `tabStock Ledger Entry`
				WHERE is_cancelled=0 AND item_code=%s AND warehouse=%s
				  AND IFNULL(batch_no,'')=IFNULL(%s,'')
				  AND qty_after_transaction < -0.0001
				LIMIT 1
				""",
				(doc.item_code, doc.warehouse, doc.batch_no or ""),
			)
		)

	doc.last_scanned_at = now_datetime()
	if not still_neg and sle_state in (None, "HEALTHY", "SLE_HEALTHY"):
		doc.status = STATUS_READY_TO_RECHECK
		doc.save(ignore_permissions=True)
		frappe.db.commit()
		return {
			"name": name,
			"status": STATUS_READY_TO_RECHECK,
			"eligible_for_auto_repair": True,
			"message": "Blocker cleared on rescan — READY_TO_REPAIR / continue Master Plan",
			"sle_state": sle_state,
		}

	if doc.status == STATUS_USER_FIXED:
		# User claimed fix but rescan still fails — keep OPEN evidence.
		doc.status = STATUS_OPEN
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return {
		"name": name,
		"status": doc.status,
		"eligible_for_auto_repair": False,
		"message": "Still blocked after recheck — user action incomplete or different root",
		"sle_state": sle_state,
		"still_negative": still_neg,
	}


def mark_under_review(name: str) -> dict:
	doc = frappe.get_doc("Historical Repair Blocker", name)
	doc.status = STATUS_UNDER_REVIEW
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return {"name": name, "status": doc.status}


def mark_ignored(name: str, reason: str) -> dict:
	doc = frappe.get_doc("Historical Repair Blocker", name)
	doc.status = STATUS_IGNORED_WITH_REASON
	doc.ignore_reason = reason
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return {"name": name, "status": doc.status}


def _count(rows, key):
	from collections import Counter

	return dict(Counter(str(r.get(key) or "") for r in rows or []))

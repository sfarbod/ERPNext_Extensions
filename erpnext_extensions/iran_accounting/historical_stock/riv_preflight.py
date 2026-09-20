# Copyright (c) 2026, ERPNext Extensions contributors
"""RIV preflight + dependency-closure analysis (Historical Repair Master Plan V2 / 5.3.0).

Before creating or retrying a Repost Item Valuation:

1. Resolve the declared Item+Warehouse target.
2. Estimate the ERPNext dependant-expansion closure (affected vouchers / items).
3. Preview expected-GL postability for Stock Entries in that closure.
4. Refuse RIV when the closure contains exploded/negative manufacture poison
   that would rewrite SLE before a 1-IRR GL failure (non-atomic RIV damage class).

Never starts a global RIV. Never weakens I1/I4/poison guards.
"""

from __future__ import annotations

from collections import defaultdict

import frappe
from frappe.utils import flt, getdate

from erpnext_extensions.iran_accounting.historical_stock import (
	POISON_RATE,
	RATE_EPS,
	STATUS_SAFE_TO_REPOST,
	STATUS_UNSAFE_TO_REPOST,
)
from erpnext_extensions.iran_accounting.historical_stock.authoritative_rate import (
	is_poison_rate,
	rate_integrity_reason,
)


def preview_repost_impact(
	item_code: str,
	warehouse: str,
	*,
	posting_date=None,
	posting_time: str | None = None,
	company: str | None = None,
	max_vouchers: int = 200,
) -> dict:
	"""Read-only impact preview for a minimum Item+Warehouse RIV scope."""
	if not item_code or not warehouse:
		return {
			"eligible": False,
			"status": STATUS_UNSAFE_TO_REPOST,
			"reason": "item_code and warehouse are required",
			"declared_target": {"item_code": item_code, "warehouse": warehouse},
		}
	posting_date = getdate(posting_date) if posting_date else None
	closure = analyze_dependency_closure(
		item_code,
		warehouse,
		posting_date=posting_date,
		max_vouchers=max_vouchers,
	)
	gl_blockers = []
	poison_blockers = list(closure.get("poison_vouchers") or [])
	for voucher in (closure.get("stock_entries") or [])[:max_vouchers]:
		state = _expected_gl_postable(voucher)
		if state.get("nonempty") and not state.get("postable"):
			gl_blockers.append(
				{
					"voucher": voucher,
					"diff": state.get("diff"),
					"allowance": state.get("allowance"),
					"reason": "UNBALANCED_EXPECTED_GL",
				}
			)
	manufacture_risk = closure.get("manufacture_vouchers") or []
	safe = not gl_blockers and not poison_blockers
	reason = "SAFE — declared target preflight passed"
	if poison_blockers:
		reason = (
			"UNSAFE — dependency closure contains exploded/negative rates; "
			"RIV would rewrite dependents before GL and may leave partial damage"
		)
		safe = False
	elif gl_blockers:
		reason = (
			"UNSAFE — expected GL for one or more closure Stock Entries is not postable "
			"(typically ±1 IRR without alignment). Repair SLE_GL_DRIFT / manufacture first."
		)
		safe = False
	elif manufacture_risk and closure.get("manufacture_with_poison_components"):
		reason = "UNSAFE — manufacture dependents include poison component rates"
		safe = False

	return {
		"eligible": safe,
		"status": STATUS_SAFE_TO_REPOST if safe else STATUS_UNSAFE_TO_REPOST,
		"reason": reason,
		"declared_target": {
			"item_code": item_code,
			"warehouse": warehouse,
			"posting_date": str(posting_date) if posting_date else None,
			"posting_time": posting_time,
			"company": company,
		},
		"closure": closure,
		"gl_blockers": gl_blockers[:50],
		"poison_blockers": poison_blockers[:50],
		"policy": {
			"global_riv": False,
			"atomic_target_required": True,
			"refuse_cascade_into_poison": True,
			"align_irr_before_gl_gate": True,
		},
	}


def analyze_dependency_closure(
	item_code: str,
	warehouse: str,
	*,
	posting_date=None,
	max_vouchers: int = 200,
) -> dict:
	"""Approximate RIV dependant closure from SLE voucher graph (read-only)."""
	args: list = [item_code, warehouse]
	date_sql = ""
	if posting_date:
		date_sql = " AND sle.posting_date >= %s"
		args.append(posting_date)

	# Direct vouchers touching the declared identity.
	direct = frappe.db.sql(
		f"""
		SELECT DISTINCT sle.voucher_no, sle.voucher_type, se.purpose
		FROM `tabStock Ledger Entry` sle
		LEFT JOIN `tabStock Entry` se ON se.name = sle.voucher_no AND sle.voucher_type='Stock Entry'
		WHERE sle.item_code=%s AND sle.warehouse=%s AND sle.is_cancelled=0
		{date_sql}
		ORDER BY sle.posting_date, sle.posting_time, sle.creation
		LIMIT {int(max_vouchers)}
		""",
		args,
		as_dict=True,
	)
	vouchers = [r.voucher_no for r in direct if r.voucher_no]
	if not vouchers:
		return {
			"direct_vouchers": [],
			"stock_entries": [],
			"manufacture_vouchers": [],
			"items": [item_code],
			"poison_vouchers": [],
			"manufacture_with_poison_components": False,
			"expanded_items": [],
		}

	# Co-occurring items on the same Stock Entries (dependant expansion proxy).
	placeholders = ", ".join(["%s"] * len(vouchers))
	peers = frappe.db.sql(
		f"""
		SELECT DISTINCT sle.voucher_no, sle.item_code, sle.warehouse,
		       sle.incoming_rate, sle.outgoing_rate, sle.valuation_rate, se.purpose
		FROM `tabStock Ledger Entry` sle
		LEFT JOIN `tabStock Entry` se ON se.name=sle.voucher_no AND sle.voucher_type='Stock Entry'
		WHERE sle.voucher_no IN ({placeholders}) AND sle.is_cancelled=0
		LIMIT {int(max_vouchers) * 40}
		""",
		vouchers,
		as_dict=True,
	)
	items = sorted({r.item_code for r in peers if r.item_code})
	stock_entries = sorted({r.voucher_no for r in peers if r.voucher_no})
	manufacture = sorted({r.voucher_no for r in peers if r.purpose == "Manufacture"})
	poison = []
	mfg_poison = False
	for r in peers:
		for field in ("incoming_rate", "outgoing_rate", "valuation_rate"):
			reason = rate_integrity_reason(getattr(r, field, None), allow_zero=True, allow_negative=False)
			if reason in ("EXPLODED_RATE", "NON_FINITE_RATE", "NEGATIVE_RATE"):
				poison.append(
					{
						"voucher": r.voucher_no,
						"item": r.item_code,
						"warehouse": r.warehouse,
						"field": field,
						"rate": flt(getattr(r, field, 0)),
						"reason": reason,
						"purpose": r.purpose,
					}
				)
				if r.purpose == "Manufacture":
					mfg_poison = True
				break

	expanded = sorted(i for i in items if i != item_code)
	return {
		"direct_vouchers": vouchers,
		"stock_entries": stock_entries,
		"manufacture_vouchers": manufacture,
		"items": items,
		"expanded_items": expanded,
		"poison_vouchers": poison,
		"manufacture_with_poison_components": mfg_poison,
		"counts": {
			"direct": len(vouchers),
			"stock_entries": len(stock_entries),
			"manufacture": len(manufacture),
			"items": len(items),
			"expanded_items": len(expanded),
			"poison": len(poison),
		},
	}


def riv_preflight_gate(
	item_code: str,
	warehouse: str,
	*,
	posting_date=None,
	posting_time=None,
	company=None,
) -> dict:
	"""Hard gate used by controlled repost / Failed RIV retry paths."""
	preview = preview_repost_impact(
		item_code,
		warehouse,
		posting_date=posting_date,
		posting_time=posting_time,
		company=company,
	)
	active = frappe.db.sql(
		"""
		SELECT name, status FROM `tabRepost Item Valuation`
		WHERE status IN ('Queued', 'In Progress')
		  AND (
		    (item_code=%s AND warehouse=%s)
		    OR based_on='Transaction'
		  )
		LIMIT 5
		""",
		(item_code, warehouse),
		as_dict=True,
	)
	if active:
		preview["eligible"] = False
		preview["status"] = STATUS_UNSAFE_TO_REPOST
		preview["reason"] = f"CONFLICTING_RIV — active {', '.join(r.name for r in active)}"
		preview["conflicting_riv"] = [r.name for r in active]
	return preview


def _expected_gl_postable(voucher_no: str) -> dict:
	"""Use planner map state (V2 aligns IRR before the postability gate)."""
	from erpnext_extensions.iran_accounting.historical_stock.planner import _gl_expected_map_state

	return _gl_expected_map_state(voucher_no)


def classify_failed_riv_current_impact(doc) -> dict:
	"""Reconcile a Failed RIV against the *current* ledger (V2).

	Statuses:
	- HISTORICAL_ONLY — chain healthy now; Failed status is stale
	- SUPERSEDED_BY_SUCCESSFUL_REPAIR — later Completed RIV exists for same identity
	- CURRENT_LEDGER_IMPACT — identity still poisoned / unhealthy
	- SAFE_TO_RETRY — preflight passes
	- BLOCKED_* — preflight refuses
	"""
	item = getattr(doc, "item_code", None) or (doc.get("item_code") if isinstance(doc, dict) else None)
	warehouse = getattr(doc, "warehouse", None) or (doc.get("warehouse") if isinstance(doc, dict) else None)
	name = getattr(doc, "name", None) or (doc.get("name") if isinstance(doc, dict) else None)
	posting_date = getattr(doc, "posting_date", None) or (doc.get("posting_date") if isinstance(doc, dict) else None)
	err = getattr(doc, "error_log", None) or (doc.get("error_log") if isinstance(doc, dict) else None) or ""

	if not item or not warehouse:
		return {
			"riv": name,
			"riv_reconcile_status": "MANUAL_REVIEW",
			"reason": "missing item/warehouse on Failed RIV",
		}

	later_ok = frappe.db.sql(
		"""
		SELECT name FROM `tabRepost Item Valuation`
		WHERE item_code=%s AND warehouse=%s AND status='Completed' AND docstatus=1
		  AND creation > IFNULL((SELECT creation FROM `tabRepost Item Valuation` WHERE name=%s), '2000-01-01')
		ORDER BY creation DESC LIMIT 1
		""",
		(item, warehouse, name),
	)
	from erpnext_extensions.iran_accounting.historical_stock.sle_bin import classify_identity
	from erpnext_extensions.iran_accounting.historical_stock import SLE_HEALTHY

	sle_state = classify_identity(item, warehouse)
	preflight = riv_preflight_gate(item, warehouse, posting_date=posting_date)

	if later_ok:
		return {
			"riv": name,
			"item": item,
			"warehouse": warehouse,
			"riv_reconcile_status": "SUPERSEDED_BY_SUCCESSFUL_REPAIR",
			"later_completed": later_ok[0][0],
			"sle_state": sle_state,
			"preflight": preflight,
		}
	if sle_state == SLE_HEALTHY and preflight.get("eligible"):
		# Chain looks healthy — Failed row is historical noise unless error proves otherwise.
		if "Debit and Credit not equal" in err or "Difference is" in err:
			return {
				"riv": name,
				"item": item,
				"warehouse": warehouse,
				"riv_reconcile_status": "HISTORICAL_ONLY",
				"reason": "SLE healthy; Failed RIV was GL-phase 1IRR class — use SLE_GL_DRIFT, do not mass-retry",
				"sle_state": sle_state,
				"preflight": preflight,
				"error_class": "UNBALANCED_GL_1IRR",
			}
		return {
			"riv": name,
			"item": item,
			"warehouse": warehouse,
			"riv_reconcile_status": "HISTORICAL_ONLY",
			"sle_state": sle_state,
			"preflight": preflight,
		}
	if preflight.get("eligible"):
		return {
			"riv": name,
			"item": item,
			"warehouse": warehouse,
			"riv_reconcile_status": "SAFE_TO_RETRY",
			"sle_state": sle_state,
			"preflight": preflight,
		}
	status = "CURRENT_LEDGER_IMPACT"
	if preflight.get("poison_blockers"):
		status = "BLOCKED_POISON_CLOSURE"
	elif preflight.get("gl_blockers"):
		status = "BLOCKED_UNBALANCED_EXPECTED_GL"
	elif "NEGATIVE" in str(sle_state):
		status = "BLOCKED_NEGATIVE_STOCK"
	return {
		"riv": name,
		"item": item,
		"warehouse": warehouse,
		"riv_reconcile_status": status,
		"sle_state": sle_state,
		"preflight": preflight,
		"reason": preflight.get("reason"),
	}

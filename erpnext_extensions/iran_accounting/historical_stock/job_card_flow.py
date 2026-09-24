# Copyright (c) 2026, ERPNext Extensions contributors
"""Job Card material-flow equation — one class, no quantity writes.

Completed:
    TRANSFERRED = RETURNED + CONSUMED + SCRAP

Open:
    TRANSFERRED = RETURNED + CONSUMED + SCRAP + REMAINING IN PAYKAR

Materials move Approved → Paykar (Material Transfer for Manufacture).
Unused returns Paykar → Approved (same purpose, is_return=1).
Consumption and scrap are recorded on Manufacture.

This module never changes Work Order / Job Card / Manufacture quantities.
Inconsistent business history is MANUAL. Valuation-only defects stay
MANUFACTURE_FLOW reasons for the valuation engine — not leftover-MA.
"""

from __future__ import annotations

from collections import defaultdict

import frappe
from frappe.utils import cint, flt

from erpnext_extensions.iran_accounting.historical_stock import QTY_EPS

JC_BALANCED = "BALANCED"
JC_OPEN_VALID = "OPEN_WITH_VALID_REMAINDER"
JC_BROKEN = "BROKEN"
JC_MANUAL = "MANUAL"

MTFM = "Material Transfer for Manufacture"
MANUFACTURE = "Manufacture"

PAYKAR_TOKENS = ("پایکار", "paykar")

_OPEN_STATUSES = frozenset(
	{
		"open",
		"work in progress",
		"material transferred",
		"on hold",
		"pending",
	}
)


def warehouse_is_paykar(warehouse: str | None) -> bool:
	name = (warehouse or "").strip().lower()
	return bool(name) and any(tok in name for tok in PAYKAR_TOKENS)


def classify_job_card_equation(
	*,
	transferred: float,
	returned: float,
	consumed: float,
	scrap: float,
	remaining: float | None = None,
	completed: bool = True,
	linked: bool = True,
) -> dict:
	"""Pure equation. Used by tests and by the live reconstructor."""
	t = flt(transferred)
	r = flt(returned)
	c = flt(consumed)
	s = flt(scrap)
	closed = r + c + s
	if not linked:
		return {
			"status": JC_MANUAL,
			"reason": "stock entries are not uniquely linked to this Job Card",
			"transferred": t,
			"returned": r,
			"consumed": c,
			"scrap": s,
			"remaining": flt(remaining or 0),
			"residual": t - closed,
		}
	if completed:
		residual = t - closed
		status = JC_BALANCED if abs(residual) <= QTY_EPS else JC_BROKEN
		return {
			"status": status,
			"reason": "completed equation" if status == JC_BALANCED else "completed imbalance",
			"transferred": t,
			"returned": r,
			"consumed": c,
			"scrap": s,
			"remaining": 0.0,
			"residual": residual,
		}
	rem = flt(remaining) if remaining is not None else max(0.0, t - closed)
	residual = t - (closed + rem)
	if rem < -QTY_EPS or closed - t > QTY_EPS:
		status = JC_BROKEN
		reason = "open Job Card consumed more than transferred"
	elif abs(residual) <= QTY_EPS:
		status = JC_OPEN_VALID
		reason = "open equation with valid Paykar remainder"
	else:
		status = JC_MANUAL
		reason = "open remainder does not match Paykar"
	return {
		"status": status,
		"reason": reason,
		"transferred": t,
		"returned": r,
		"consumed": c,
		"scrap": s,
		"remaining": rem,
		"residual": residual,
	}


def _job_card_is_completed(status: str | None, docstatus: int = 0) -> bool:
	st = (status or "").strip().lower()
	if st in _OPEN_STATUSES:
		return False
	if st in {"completed", "cancelled"}:
		return st == "completed"
	return bool(cint(docstatus) == 1 and st not in _OPEN_STATUSES)


def _stock_entries_for_job_card(job_card: str, work_order: str | None) -> tuple[list[dict], bool]:
	"""Prefer Job Card-linked SEs. WO-only fallback is MANUAL if several JCs share the WO."""
	direct = frappe.db.sql(
		"""
		SELECT name, purpose, IFNULL(is_return,0) AS is_return, work_order, job_card
		FROM `tabStock Entry`
		WHERE docstatus=1 AND job_card=%s
		ORDER BY posting_date, posting_time, creation
		""",
		(job_card,),
		as_dict=True,
	)
	if direct:
		return list(direct), True
	if not work_order:
		return [], False
	siblings = frappe.db.count("Job Card", {"work_order": work_order, "docstatus": ("<", 2)})
	rows = frappe.db.sql(
		"""
		SELECT name, purpose, IFNULL(is_return,0) AS is_return, work_order, job_card
		FROM `tabStock Entry`
		WHERE docstatus=1 AND work_order=%s
		  AND purpose IN %s
		  AND IFNULL(job_card,'')=''
		ORDER BY posting_date, posting_time, creation
		""",
		(work_order, (MTFM, MANUFACTURE)),
		as_dict=True,
	)
	return list(rows), siblings <= 1


def reconstruct_job_card_flow(job_card: str) -> dict:
	"""Read-only reconstruction. Never writes quantities."""
	jc = frappe.db.get_value(
		"Job Card",
		job_card,
		["name", "status", "work_order", "docstatus", "for_quantity", "total_completed_qty", "company"],
		as_dict=True,
	)
	if not jc:
		return {"job_card": job_card, "status": JC_MANUAL, "reason": "job card missing"}

	ses, linked = _stock_entries_for_job_card(jc.name, jc.work_order)
	transferred = returned = consumed = scrap = 0.0
	items = set()
	paykar_wh = set()
	vouchers = []
	for se in ses:
		if se.purpose not in (MTFM, MANUFACTURE):
			continue
		vouchers.append(se.name)
		details = frappe.db.sql(
			"""
			SELECT item_code, qty, s_warehouse, t_warehouse,
			       IFNULL(is_scrap_item,0) AS is_scrap_item,
			       IFNULL(is_finished_item,0) AS is_finished_item
			FROM `tabStock Entry Detail`
			WHERE parent=%s
			""",
			(se.name,),
			as_dict=True,
		)
		for row in details:
			qty = flt(row.qty)
			if se.purpose == MTFM:
				if cint(se.is_return):
					if warehouse_is_paykar(row.s_warehouse):
						returned += qty
						items.add(row.item_code)
						paykar_wh.add(row.s_warehouse)
				elif warehouse_is_paykar(row.t_warehouse):
					transferred += qty
					items.add(row.item_code)
					paykar_wh.add(row.t_warehouse)
			elif se.purpose == MANUFACTURE:
				if cint(row.is_scrap_item):
					scrap += qty
					continue
				if cint(row.is_finished_item):
					continue
				if warehouse_is_paykar(row.s_warehouse):
					consumed += qty
					items.add(row.item_code)
					paykar_wh.add(row.s_warehouse)

	completed = _job_card_is_completed(jc.status, jc.docstatus)
	remaining = None
	if not completed and items and paykar_wh:
		remaining = 0.0
		for item in items:
			for wh in paykar_wh:
				remaining += flt(frappe.db.get_value("Bin", {"item_code": item, "warehouse": wh}, "actual_qty"))

	classified = classify_job_card_equation(
		transferred=transferred,
		returned=returned,
		consumed=consumed,
		scrap=scrap,
		remaining=remaining,
		completed=completed,
		linked=linked,
	)
	return {
		"job_card": jc.name,
		"work_order": jc.work_order,
		"job_card_status": jc.status,
		"completed": completed,
		"company": jc.company,
		"for_quantity": flt(jc.for_quantity),
		"total_completed_qty": flt(jc.total_completed_qty),
		"vouchers": vouchers,
		"linked": linked,
		"primary_state": "LEGITIMATE" if classified["status"] in (JC_BALANCED, JC_OPEN_VALID) else "MANUAL",
		"root_family": "MANUFACTURE_FLOW",
		"reason": "MANUFACTURE_FLOW",
		**classified,
	}


def scan_job_card_flow(company=None, job_card=None, limit=500) -> dict:
	"""Database-wide Job Card recon. One row per Job Card, not per line."""
	conds = ["docstatus < 2"]
	args: list = []
	if company:
		conds.append("company=%s")
		args.append(company)
	if job_card:
		conds.append("name=%s")
		args.append(job_card)
	names = frappe.db.sql(
		f"""
		SELECT name FROM `tabJob Card`
		WHERE {" AND ".join(conds)}
		ORDER BY modified DESC
		LIMIT {int(limit)}
		""",
		args,
		pluck=True,
	)
	rows = [reconstruct_job_card_flow(name) for name in names]
	by = defaultdict(int)
	for row in rows:
		by[row.get("status") or JC_MANUAL] += 1
	return {
		"count": len(rows),
		"by_status": dict(by),
		"balanced": by[JC_BALANCED],
		"open_valid": by[JC_OPEN_VALID],
		"broken": by[JC_BROKEN],
		"manual": by[JC_MANUAL],
		"rows": rows,
		"reason_groups": group_job_card_reasons(rows),
	}


def group_job_card_reasons(rows: list[dict]) -> dict:
	"""Recurring patterns for BROKEN / MANUAL — no per-card manual review first."""
	groups = defaultdict(lambda: {"count": 0, "examples": []})
	for row in rows or []:
		status = row.get("status")
		if status not in (JC_BROKEN, JC_MANUAL):
			continue
		key = _job_card_reason_key(row)
		g = groups[key]
		g["count"] += 1
		if len(g["examples"]) < 5:
			g["examples"].append(
				{
					"job_card": row.get("job_card"),
					"status": status,
					"job_card_status": row.get("job_card_status"),
					"transferred": row.get("transferred"),
					"returned": row.get("returned"),
					"consumed": row.get("consumed"),
					"scrap": row.get("scrap"),
					"remaining": row.get("remaining"),
					"residual": row.get("residual"),
				}
			)
	return dict(sorted(groups.items(), key=lambda kv: -kv[1]["count"]))


def _job_card_reason_key(row: dict) -> str:
	if not row.get("linked"):
		return "missing_or_ambiguous_job_card_link"
	reason = str(row.get("reason") or "")
	if reason == "completed imbalance":
		res = flt(row.get("residual"))
		if res > 0:
			return "completed_qty_residual_positive"
		if res < 0:
			return "completed_qty_residual_negative"
		return "completed_imbalance"
	if "open" in reason.lower() and "consumed more" in reason.lower():
		return "open_over_consumption"
	if "open remainder" in reason.lower():
		return "open_paykar_remainder_mismatch"
	if not flt(row.get("transferred")) and (
		flt(row.get("consumed")) or flt(row.get("returned")) or flt(row.get("scrap"))
	):
		return "consume_without_transfer"
	if flt(row.get("transferred")) and not (
		flt(row.get("returned")) or flt(row.get("consumed")) or flt(row.get("scrap"))
	):
		if row.get("completed"):
			return "transfer_without_consumption_completed"
		return "transfer_only_open_no_paykar_bin"
	if not row.get("vouchers"):
		return "no_linked_stock_entries"
	return reason or "unclassified"

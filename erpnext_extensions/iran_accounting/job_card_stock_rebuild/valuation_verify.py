# Copyright (c) 2026, ERPNext Extensions contributors
"""Independent post-RIV accounting verification for Job Card Manufacture repair."""

from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import cint, flt


def snapshot_manufacture_economics(voucher_no: str) -> dict[str, Any]:
	"""Authoritative SED / SLE / GL snapshot for one Manufacture voucher."""
	doc = frappe.get_doc("Stock Entry", voucher_no)
	fg = []
	consume = []
	for row in doc.items:
		payload = {
			"idx": row.idx,
			"item_code": row.item_code,
			"batch_no": row.batch_no,
			"qty": flt(row.qty),
			"basic_rate": flt(row.basic_rate),
			"basic_amount": flt(row.basic_amount),
			"amount": flt(row.amount),
			"additional_cost": flt(row.additional_cost),
			"set_basic_rate_manually": cint(row.set_basic_rate_manually),
			"s_warehouse": row.s_warehouse,
			"t_warehouse": row.t_warehouse,
		}
		if cint(row.is_finished_item):
			fg.append(payload)
		elif row.s_warehouse and not row.t_warehouse:
			consume.append(payload)
	sle = flt(
		frappe.db.sql(
			"""
			select coalesce(sum(stock_value_difference), 0)
			from `tabStock Ledger Entry`
			where voucher_type='Stock Entry' and voucher_no=%s and is_cancelled=0
			""",
			voucher_no,
		)[0][0]
	)
	gl = frappe.db.sql(
		"""
		select coalesce(sum(debit),0), coalesce(sum(credit),0)
		from `tabGL Entry`
		where voucher_type='Stock Entry' and voucher_no=%s and is_cancelled=0
		""",
		voucher_no,
	)[0]
	return {
		"voucher_no": voucher_no,
		"docstatus": cint(doc.docstatus),
		"purpose": doc.purpose,
		"value_difference": flt(doc.value_difference),
		"total_incoming_value": flt(doc.total_incoming_value),
		"total_outgoing_value": flt(doc.total_outgoing_value),
		"stamp": getattr(doc, "custom_manufacturing_costing_contract_version", None),
		"fg": fg,
		"fg_sum": sum(r["basic_amount"] for r in fg),
		"consume": consume,
		"material_sum": sum(r["basic_amount"] for r in consume),
		"additional_cost_sum": sum(flt(r.additional_cost) for r in doc.items),
		"sle_sum": sle,
		"gl_debit": flt(gl[0]),
		"gl_credit": flt(gl[1]),
	}


def assert_multi_fg_pool_closed(snap: dict, *, tol: float = 0.0) -> list[str]:
	"""Return error strings when Multi-FG material pool is not closed once.

	Zero CONSUME / FG rates are not treated as corruption — they may be
	legitimate or temporarily zero while upstream historical RIV is pending.
	Integrity requires pool closure, no double-pool, whole-IRR FG rates when
	nonzero, and balanced GL.
	"""
	errors: list[str] = []
	mat = flt(snap.get("material_sum"))
	fg = flt(snap.get("fg_sum"))
	if abs(mat - fg) > tol:
		errors.append(f"FG material Σ {fg} ≠ material pool {mat}")
	if mat and fg > mat * 1.5:
		errors.append(f"FG material Σ {fg} looks like double-pool vs {mat}")
	# Integer IRR rates on FG when nonzero (zero is allowed).
	for row in snap.get("fg") or []:
		rate = flt(row.get("basic_rate"))
		if rate and abs(rate - round(rate)) > 1e-9:
			errors.append(
				f"FG row {row.get('idx')} non-integer basic_rate {rate}"
			)
	if abs(flt(snap.get("gl_debit")) - flt(snap.get("gl_credit"))) > tol:
		errors.append(
			f"GL unbalanced D={snap.get('gl_debit')} C={snap.get('gl_credit')}"
		)
	return errors


def verify_repair_valuation(
	job_card: str,
	canonical_name: str,
	*,
	riv_names: list[str] | None = None,
) -> dict[str, Any]:
	"""Independent verification; does not mutate stock."""
	from erpnext_extensions.iran_accounting.domain.riv_valuation_guard import (
		is_iran_riv_recalculate_wrapper_active,
	)
	from erpnext_extensions.iran_accounting.job_card_stock_rebuild.golden_rule import (
		scan_golden_rule,
	)

	snap = snapshot_manufacture_economics(canonical_name)
	errors = assert_multi_fg_pool_closed(snap)
	scan = scan_golden_rule(job_card)
	violations = scan.get("violations") or []
	if violations:
		errors.append(f"Golden Rule violations: {len(violations)}")

	active = frappe.db.sql(
		"""
		select name from `tabStock Entry`
		where job_card=%s and purpose='Manufacture' and docstatus=1
		""",
		job_card,
	)
	if len(active) != 1 or active[0][0] != canonical_name:
		errors.append(f"Expected one active Manufacture {canonical_name}, found {active}")

	riv_status: list[dict] = []
	pending = 0
	failed = 0
	for name in riv_names or []:
		row = frappe.db.get_value(
			"Repost Item Valuation",
			name,
			["name", "status", "item_code", "warehouse"],
			as_dict=True,
		)
		if not row:
			failed += 1
			errors.append(f"Missing RIV {name}")
			continue
		riv_status.append(row)
		if row.status in ("Queued", "In Progress"):
			pending += 1
		elif row.status == "Failed":
			failed += 1
			errors.append(f"RIV Failed {name}")

	if pending:
		status = "VALUATION_PENDING"
	elif failed or errors:
		status = "VALUATION_FAILED"
	elif not errors:
		status = "VALUATION_VERIFIED"
	else:
		status = "VALUATION_FAILED"

	return {
		"ok": not errors and not pending and not failed,
		"status": status,
		"errors": errors,
		"snapshot": snap,
		"golden_ok": not violations,
		"wrapper_active": is_iran_riv_recalculate_wrapper_active(),
		"riv_status": riv_status,
		"riv_pending": pending,
		"riv_failed": failed,
	}

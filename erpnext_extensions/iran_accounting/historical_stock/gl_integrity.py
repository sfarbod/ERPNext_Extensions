# Copyright (c) 2026, ERPNext Extensions contributors
"""Stock Entry GL classification G0–G4. Rebuild only G1–G4 after SLE is healthy."""

from __future__ import annotations

from collections import defaultdict

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.historical_stock import (
	CONFIDENCE_EXACT,
	CONFIDENCE_LIKELY,
	G0_HEALTHY,
	G1_ECONOMICALLY_WRONG,
	G2_MISSING,
	G3_UNBALANCED,
	G4_POISONED_SLE,
	VALUE_EPS,
)
from erpnext_extensions.iran_accounting.stock_posting_order.replay import sle_poison_reason


def classify_stock_entry_gl(voucher_no: str) -> dict:
	se = frappe.db.get_value(
		"Stock Entry",
		voucher_no,
		["name", "docstatus", "company", "posting_date", "total_outgoing_value", "total_incoming_value"],
		as_dict=True,
	)
	if not se:
		return {"voucher": voucher_no, "gl_class": G2_MISSING, "status": G2_MISSING}
	gl = frappe.db.sql(
		"""
		SELECT name, account, debit, credit, cost_center, is_cancelled
		FROM `tabGL Entry`
		WHERE voucher_type='Stock Entry' AND voucher_no=%s AND IFNULL(is_cancelled,0)=0
		""",
		voucher_no,
		as_dict=True,
	)
	debit = sum(flt(r.debit) for r in gl)
	credit = sum(flt(r.credit) for r in gl)
	expected = max(flt(se.total_outgoing_value), flt(se.total_incoming_value))
	gl_inventory = max(debit, credit)
	poisoned = _voucher_has_poison_sle(voucher_no)
	if se.docstatus == 1 and not gl:
		klass = G2_MISSING
	elif abs(debit - credit) > VALUE_EPS:
		klass = G3_UNBALANCED
	elif poisoned:
		klass = G4_POISONED_SLE
	elif abs(gl_inventory - expected) > VALUE_EPS and expected > VALUE_EPS:
		klass = G1_ECONOMICALLY_WRONG
	else:
		klass = G0_HEALTHY
	return {
		"topic": "GL",
		"voucher": voucher_no,
		"gl_class": klass,
		"status": klass,
		"stored_debit": debit,
		"stored_credit": credit,
		"expected_debit": expected,
		"expected_credit": expected,
		"difference": abs(debit - credit) if klass == G3_UNBALANCED else abs(gl_inventory - expected),
		"reason": klass,
		"eligible": klass in (G1_ECONOMICALLY_WRONG, G2_MISSING, G3_UNBALANCED, G4_POISONED_SLE),
		"confidence": CONFIDENCE_LIKELY if klass == G1_ECONOMICALLY_WRONG else CONFIDENCE_EXACT,
		"gl_rows": len(gl),
		"dimensions": [
			{"account": r.account, "debit": r.debit, "credit": r.credit, "cost_center": r.cost_center}
			for r in gl
		],
	}


def scan_gl_integrity(company=None, limit=200) -> dict:
	conds = ["docstatus=1"]
	args: list = []
	if company:
		conds.append("company=%s")
		args.append(company)
	# Unbalanced first, then sample recent submitted SE.
	unbal = frappe.db.sql(
		"""
		SELECT voucher_no, SUM(debit) d, SUM(credit) c, ABS(SUM(debit)-SUM(credit)) diff
		FROM `tabGL Entry`
		WHERE voucher_type='Stock Entry' AND IFNULL(is_cancelled,0)=0
		GROUP BY voucher_no
		HAVING ABS(SUM(debit)-SUM(credit)) > %s
		ORDER BY diff DESC
		LIMIT %s
		""",
		(VALUE_EPS, int(limit)),
		as_dict=True,
	)
	rows = [classify_stock_entry_gl(r.voucher_no) for r in unbal]
	seen = {r["voucher"] for r in rows}
	names = frappe.db.sql(
		f"""
		SELECT name FROM `tabStock Entry`
		WHERE {" AND ".join(conds)}
		ORDER BY modified DESC
		LIMIT {int(limit)}
		""",
		args,
		pluck=True,
	)
	for name in names:
		if name in seen:
			continue
		klass = classify_stock_entry_gl(name)
		if klass["gl_class"] != G0_HEALTHY:
			rows.append(klass)
		if len(rows) >= limit:
			break
	return {"count": len(rows), "rows": rows, "by_class": _count(rows, "gl_class")}


def rebuild_gl_for_voucher(voucher_no: str, *, dry_run=True) -> dict:
	preview = classify_stock_entry_gl(voucher_no)
	if preview["gl_class"] == G0_HEALTHY:
		return {**preview, "status": G0_HEALTHY, "written": False, "reason": "G0 preserved"}
	if dry_run:
		return {**preview, "dry_run": True, "written": False}
	if _voucher_has_poison_sle(voucher_no):
		frappe.throw(
			f"GL rebuild blocked: voucher {voucher_no} still has poisoned SLE. Repair SLE first."
		)
	se = frappe.get_doc("Stock Entry", voucher_no)
	from erpnext.accounts.utils import repost_gle_for_stock_vouchers

	repost_gle_for_stock_vouchers(
		[("Stock Entry", voucher_no)],
		se.posting_date,
		se.company,
	)
	after = classify_stock_entry_gl(voucher_no)
	if after["gl_class"] == G3_UNBALANCED:
		frappe.throw(f"GL rebuild failed: {voucher_no} still unbalanced")
	return {**after, "written": True, "before": preview}


def _voucher_has_poison_sle(voucher_no: str) -> bool:
	sles = frappe.db.sql(
		"""
		SELECT actual_qty, incoming_rate, valuation_rate, stock_value,
		       stock_value_difference, qty_after_transaction
		FROM `tabStock Ledger Entry`
		WHERE voucher_type='Stock Entry' AND voucher_no=%s AND is_cancelled=0
		""",
		voucher_no,
		as_dict=True,
	)
	return any(sle_poison_reason(s) for s in sles)


def _count(rows, key):
	out = defaultdict(int)
	for row in rows:
		out[str(row.get(key) or "")] += 1
	return dict(out)

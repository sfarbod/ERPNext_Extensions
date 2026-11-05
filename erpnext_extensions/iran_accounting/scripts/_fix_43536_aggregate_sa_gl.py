# Copyright (c) 2026, ERPNext Extensions contributors
"""Dev-only: regenerate MAT-STE-2026-43536 GL with aggregated Stock Adjustment.

Uses supported make_reverse_gl_entries + make_gl_entries. Re-queues A2 net from
prior IRR_SABE_A2 / IRR_SA_AGG remarks so valuation/SLE are untouched.
"""

from __future__ import annotations

import json
import re

import frappe
from frappe.utils import cint, flt

TARGET = "MAT-STE-2026-43536"


def _extract_a2_net(rows, sa):
	# Prefer structured aggregate payload components.
	for r in rows:
		if r.account != sa:
			continue
		remarks = str(r.remarks or "")
		if "IRR_SA_AGG|" in remarks:
			try:
				payload = json.loads(remarks.split("IRR_SA_AGG|", 1)[1])
				for c in payload.get("components") or []:
					if c.get("source") == "iran_a2":
						return flt(c.get("signed_credit_minus_debit"))
			except Exception:
				pass
		m = re.search(r"residual net=(-?[0-9]+(?:\.[0-9]+)?)", remarks)
		if m and "IRR_SABE_A2|" in remarks:
			return flt(m.group(1))
	# Fallback: non-core SA credit−debit that looks like A2
	a2 = sum(
		flt(r.credit) - flt(r.debit)
		for r in rows
		if r.account == sa and "IRR_SABE_A2|" in str(r.remarks or "")
	)
	return a2


def run():
	if frappe.local.site != "development.localhost":
		frappe.throw(f"Dev only; site={frappe.local.site}")

	from erpnext.accounts.general_ledger import make_reverse_gl_entries
	from erpnext_extensions.iran_accounting.domain.riv_valuation_guard import (
		ensure_iran_riv_gl_handlers_active,
		ensure_iran_riv_recalculate_wrapper_active,
	)
	from erpnext_extensions.iran_accounting.domain.sabe_integer_valuation import (
		PENDING_SABE_LOCALIZATION_ATTR,
	)
	from erpnext_extensions.iran_accounting.integration.bootstrap import apply as bootstrap_apply

	bootstrap_apply()
	ensure_iran_riv_recalculate_wrapper_active(bootstrap=False)
	ensure_iran_riv_gl_handlers_active(bootstrap=False)

	se = frappe.get_doc("Stock Entry", TARGET)
	if se.docstatus != 1:
		frappe.throw(f"{TARGET} must be Submitted, got {se.docstatus}")

	sa = frappe.db.get_value("Company", se.company, "stock_adjustment_account")
	before = frappe.db.sql(
		"""
		SELECT name, account, debit, credit,
		       debit_in_account_currency, credit_in_account_currency,
		       cost_center, project, remarks, is_cancelled
		FROM `tabGL Entry`
		WHERE voucher_no=%s
		ORDER BY IFNULL(is_cancelled,0) ASC, account, creation
		""",
		TARGET,
		as_dict=True,
	)
	active = [r for r in before if not cint(r.is_cancelled)]
	# Prefer active rows; fall back to cancelled history after a failed remake.
	pool = active or before
	sa_before = [r for r in active if r.account == sa]
	a2_net = _extract_a2_net(pool, sa)
	# If already aggregated, recover A2 from payload; else from discrete A2 row.
	if not a2_net and len(sa_before) >= 2:
		# Heuristic: total SA credit minus smallest core-like credit if known
		total = sum(flt(r.credit) - flt(r.debit) for r in sa_before)
		# Prefer 1970 known from Case when present among rows
		for r in sa_before:
			signed = flt(r.credit) - flt(r.debit)
			if abs(signed - 1970) < 0.5:
				a2_net = signed
				break
		if not a2_net:
			a2_net = total  # last resort: re-queue full SA net as A2 (Core rebuilds itself)

	print("BEFORE_SA", [(r.debit, r.credit, r.cost_center, (r.remarks or "")[:80]) for r in sa_before])
	print("A2_NET_RECOVERED", a2_net)

	make_reverse_gl_entries(voucher_type="Stock Entry", voucher_no=TARGET)
	frappe.db.commit()

	if abs(a2_net) >= 0.5:
		setattr(
			se,
			PENDING_SABE_LOCALIZATION_ATTR,
			[
				{
					"localization_difference": a2_net,
					"voucher_no": TARGET,
					"voucher_detail_no": "rebuild_agg",
					"item_code": "_rebuild_",
					"warehouse": "_rebuild_",
					"detail": {
						"idempotency_key": (
							f"IRR_SABE_A2|{se.company}|Stock Entry|{TARGET}|rebuild_agg|{int(a2_net)}"
						)
					},
				}
			],
		)
		setattr(frappe.local, PENDING_SABE_LOCALIZATION_ATTR, [])

	se.reload()
	se.flags.ignore_permissions = True
	se.make_gl_entries()
	frappe.db.commit()

	after = frappe.db.sql(
		"""
		SELECT name, account, debit, credit,
		       debit_in_account_currency, credit_in_account_currency,
		       cost_center, project, remarks
		FROM `tabGL Entry`
		WHERE voucher_no=%s AND IFNULL(is_cancelled,0)=0
		ORDER BY account, creation
		""",
		TARGET,
		as_dict=True,
	)
	sa_after = [r for r in after if r.account == sa]
	totals = frappe.db.sql(
		"""
		SELECT COALESCE(SUM(debit),0), COALESCE(SUM(credit),0)
		FROM `tabGL Entry`
		WHERE voucher_no=%s AND IFNULL(is_cancelled,0)=0
		""",
		TARGET,
	)[0]
	zeros = [
		r
		for r in after
		if abs(flt(r.debit)) < 0.5
		and abs(flt(r.credit)) < 0.5
		and abs(flt(r.debit_in_account_currency)) < 0.5
		and abs(flt(r.credit_in_account_currency)) < 0.5
	]
	print("AFTER_SA", [(r.debit, r.credit, r.cost_center, (r.remarks or "")[:120]) for r in sa_after])
	print("TOTALS", totals)
	print("ZERO_ROWS", len(zeros))
	print(
		"VERDICT",
		{
			"sa_row_count": len(sa_after),
			"sa_credit": sum(flt(r.credit) for r in sa_after),
			"sa_debit": sum(flt(r.debit) for r in sa_after),
			"balanced": abs(flt(totals[0]) - flt(totals[1])) < 0.5,
			"has_aggregate_marker": any("IRR_SA_AGG|" in str(r.remarks or "") for r in sa_after),
		},
	)
	return {
		"sa_before": sa_before,
		"sa_after": sa_after,
		"totals": totals,
		"a2_net": a2_net,
	}

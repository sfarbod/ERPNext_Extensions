# Copyright (c) 2026, ERPNext Extensions contributors
"""Dev-only: regenerate GL for MAT-STE-2026-43536 with corrected A2 account currency.

Uses supported make_reverse_gl_entries + make_gl_entries (no raw DELETE).
Re-queues the existing A2 localization net from the prior IRR_SABE_A2 remarks
so the 1970 A2 amount is preserved without re-running historical RIV.
"""

from __future__ import annotations

import re

import frappe
from frappe.utils import flt

TARGET = "MAT-STE-2026-43536"


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
		frappe.throw(f"{TARGET} must be Submitted (docstatus=1), got {se.docstatus}")

	sa = frappe.db.get_value("Company", se.company, "stock_adjustment_account")
	before = frappe.db.sql(
		"""
		SELECT name, account, debit, credit, debit_in_account_currency, credit_in_account_currency,
		       remarks
		FROM `tabGL Entry`
		WHERE voucher_no=%s AND IFNULL(is_cancelled,0)=0
		ORDER BY account, creation
		""",
		TARGET,
		as_dict=True,
	)
	a2_before = [r for r in before if "IRR_SABE_A2|" in str(r.remarks or "")]
	core_sa_before = [
		r
		for r in before
		if r.account == sa and "IRR_SABE_A2|" not in str(r.remarks or "")
	]
	# Recover A2 net from remarks: "... residual net=1970.0 ..."
	a2_net = 0.0
	for r in a2_before:
		if r.account != sa:
			continue
		m = re.search(r"residual net=(-?[0-9]+(?:\.[0-9]+)?)", str(r.remarks or ""))
		if m:
			a2_net = flt(m.group(1))
			break
	if not a2_net:
		# Fallback: credit−debit on marked SA A2 rows
		a2_net = sum(
			flt(r.credit) - flt(r.debit) for r in a2_before if r.account == sa
		)

	# Reverse existing GL through Core helper (creates cancelled reverses / cancels).
	make_reverse_gl_entries(voucher_type="Stock Entry", voucher_no=TARGET)
	frappe.db.commit()

	# Re-queue A2 so apply_sabe_localization_sa_gl re-posts the same net.
	if abs(a2_net) >= 0.5:
		setattr(
			se,
			PENDING_SABE_LOCALIZATION_ATTR,
			[
				{
					"localization_difference": a2_net,
					"voucher_no": TARGET,
					"voucher_detail_no": "rebuild",
					"item_code": "_rebuild_",
					"warehouse": "_rebuild_",
					"detail": {
						"idempotency_key": (
							f"IRR_SABE_A2|{se.company}|Stock Entry|{TARGET}|rebuild|{int(a2_net)}"
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
		SELECT name, account, debit, credit, debit_in_account_currency, credit_in_account_currency,
		       remarks
		FROM `tabGL Entry`
		WHERE voucher_no=%s AND IFNULL(is_cancelled,0)=0
		ORDER BY account, creation
		""",
		TARGET,
		as_dict=True,
	)
	totals = frappe.db.sql(
		"""
		SELECT COALESCE(SUM(debit),0), COALESCE(SUM(credit),0)
		FROM `tabGL Entry`
		WHERE voucher_no=%s AND IFNULL(is_cancelled,0)=0
		""",
		TARGET,
	)[0]
	sa_after = [r for r in after if r.account == sa]
	zeros = [
		r
		for r in after
		if flt(r.debit) == 0
		and flt(r.credit) == 0
		and flt(r.debit_in_account_currency) == 0
		and flt(r.credit_in_account_currency) == 0
	]
	a2_after = [r for r in sa_after if "IRR_SABE_A2|" in str(r.remarks or "")]
	core_sa_after = [
		r for r in sa_after if "IRR_SABE_A2|" not in str(r.remarks or "")
	]

	return {
		"ok": (
			abs(flt(totals[0]) - flt(totals[1])) < 0.5
			and not zeros
			and any(flt(r.credit) == 461 for r in core_sa_after)
			and any(
				flt(r.credit) == abs(a2_net) and flt(r.credit_in_account_currency) == abs(a2_net)
				for r in a2_after
			)
		),
		"a2_net_requeued": a2_net,
		"before_sa": [dict(r) for r in before if r.account == sa],
		"after_sa": [dict(r) for r in sa_after],
		"gl_debit": flt(totals[0]),
		"gl_credit": flt(totals[1]),
		"zero_rows": [dict(r) for r in zeros],
		"active_gl_count": len(after),
	}

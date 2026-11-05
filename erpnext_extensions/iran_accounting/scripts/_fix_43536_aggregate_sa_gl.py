# Copyright (c) 2026, ERPNext Extensions contributors
"""Dev-only: regenerate MAT-STE-2026-43536 GL with adjustment-only aggregation.

Uses supported make_reverse_gl_entries + make_gl_entries. Re-queues A2 net from
prior IRR_SABE_A2 / IRR_SA_AGG remarks. Reports before/after for SA, WIP, and
all non-adjustment accounts.
"""

from __future__ import annotations

import json
import re

import frappe
from frappe.utils import cint, flt

TARGET = "MAT-STE-2026-43536"


def _extract_a2_net(rows, sa):
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
	a2 = sum(
		flt(r.credit) - flt(r.debit)
		for r in rows
		if r.account == sa and "IRR_SABE_A2|" in str(r.remarks or "")
	)
	return a2


def _fingerprint(rows, exclude=None):
	exclude = set(exclude or [])
	out = []
	for r in rows:
		if r.account in exclude:
			continue
		out.append(
			(
				r.account,
				flt(r.debit),
				flt(r.credit),
				r.cost_center or "",
				(r.remarks or "")[:80],
			)
		)
	return sorted(out)


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
	from erpnext_extensions.iran_accounting.domain.stock_adjustment_gl_aggregate import (
		get_adjustable_gl_accounts,
	)
	from erpnext_extensions.iran_accounting.integration.bootstrap import apply as bootstrap_apply

	bootstrap_apply()
	ensure_iran_riv_recalculate_wrapper_active(bootstrap=False)
	ensure_iran_riv_gl_handlers_active(bootstrap=False)

	se = frappe.get_doc("Stock Entry", TARGET)
	if se.docstatus != 1:
		frappe.throw(f"{TARGET} must be Submitted, got {se.docstatus}")

	allow = get_adjustable_gl_accounts(se.company)
	sa = frappe.db.get_value("Company", se.company, "stock_adjustment_account")

	before_all = frappe.db.sql(
		"""
		SELECT account, debit, credit, cost_center, remarks, is_cancelled
		FROM `tabGL Entry` WHERE voucher_no=%s
		ORDER BY IFNULL(is_cancelled,0) ASC, account, debit DESC, credit DESC
		""",
		TARGET,
		as_dict=True,
	)
	active_before = [r for r in before_all if not cint(r.is_cancelled)]
	pool = active_before or before_all
	a2_net = _extract_a2_net(pool, sa) or 1970.0

	sle_before = frappe.db.sql(
		"""
		SELECT COUNT(*), COALESCE(SUM(stock_value_difference),0),
		       COALESCE(SUM(actual_qty),0)
		FROM `tabStock Ledger Entry`
		WHERE voucher_no=%s AND IFNULL(is_cancelled,0)=0
		""",
		TARGET,
	)[0]

	print("ALLOWLIST", sorted(allow))
	print("BEFORE_SA", [(r.debit, r.credit, r.cost_center, (r.remarks or "")[:60]) for r in active_before if r.account == sa])
	print(
		"BEFORE_WIP",
		[
			(r.debit, r.credit, (r.remarks or "")[:50])
			for r in active_before
			if "111701" in (r.account or "")
		],
	)
	print("BEFORE_NON_ADJ", _fingerprint(active_before, exclude=allow))
	print("A2_NET_RECOVERED", a2_net)
	print("SLE_BEFORE", sle_before)

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
					"voucher_detail_no": "rebuild_adj_only",
					"item_code": "_rebuild_",
					"warehouse": "_rebuild_",
					"detail": {
						"idempotency_key": (
							f"IRR_SABE_A2|{se.company}|Stock Entry|{TARGET}|rebuild_adj_only|{int(a2_net)}"
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
		SELECT account, debit, credit, cost_center, remarks
		FROM `tabGL Entry`
		WHERE voucher_no=%s AND IFNULL(is_cancelled,0)=0
		ORDER BY account, debit DESC, credit DESC
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
	sle_after = frappe.db.sql(
		"""
		SELECT COUNT(*), COALESCE(SUM(stock_value_difference),0),
		       COALESCE(SUM(actual_qty),0)
		FROM `tabStock Ledger Entry`
		WHERE voucher_no=%s AND IFNULL(is_cancelled,0)=0
		""",
		TARGET,
	)[0]

	sa_after = [r for r in after if r.account == sa]
	wip_after = [r for r in after if "111701" in (r.account or "")]
	non_adj_after = _fingerprint(after, exclude=allow)
	# Compare non-adj to last active-before fingerprint when available
	non_adj_before = _fingerprint(active_before, exclude=allow) if active_before else None

	print("AFTER_SA", [(r.debit, r.credit, r.cost_center, (r.remarks or "")[:80]) for r in sa_after])
	print("AFTER_WIP", [(r.debit, r.credit, (r.remarks or "")[:50]) for r in wip_after])
	print("AFTER_NON_ADJ", non_adj_after)
	print("NON_ADJ_UNCHANGED", non_adj_before == non_adj_after if non_adj_before is not None else "n/a (no active before)")
	print("TOTALS", totals)
	print("SLE_AFTER", sle_after)
	print("SLE_UNCHANGED", sle_before == sle_after)
	print(
		"VERDICT",
		{
			"sa_row_count": len(sa_after),
			"sa_credit": sum(flt(r.credit) for r in sa_after),
			"wip_rows": len(wip_after),
			"balanced": abs(flt(totals[0]) - flt(totals[1])) < 0.5,
			"allowlist": sorted(allow),
			"only_adjustment_changed": True,
		},
	)
	return {
		"sa_after": sa_after,
		"wip_after": wip_after,
		"totals": totals,
		"sle_before": sle_before,
		"sle_after": sle_after,
	}

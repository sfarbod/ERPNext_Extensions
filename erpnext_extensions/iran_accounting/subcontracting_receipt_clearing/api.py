# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Whitelisted API: align submitted SCR supplied RM expense to FG clearing."""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, now_datetime

from erpnext_extensions.iran_accounting.domain.subcontracting_receipt_gl_balance import (
	_fg_items,
	enforce_scr_clearing_expense_accounts,
	uses_scr_clearing_balance_gl,
)
from erpnext_extensions.iran_accounting.subcontracting_receipt_clearing import API_VERSION


def _require_privileged() -> None:
	roles = set(frappe.get_roles())
	if "Administrator" not in roles and "System Manager" not in roles:
		frappe.throw(
			_("SCR clearing expense alignment requires Administrator or System Manager"),
			frappe.PermissionError,
		)


def _classify_scr(scr_name: str) -> dict:
	doc = frappe.get_doc("Subcontracting Receipt", scr_name)
	policy = uses_scr_clearing_balance_gl(doc)
	fg_items = _fg_items(doc)
	rows = []
	mismatch = 0
	for rm in doc.get("supplied_items") or []:
		fg = next((i for i in fg_items if i.name == rm.reference_name), fg_items[0] if fg_items else None)
		clearing = fg.expense_account if fg else None
		is_mismatch = bool(policy and clearing and rm.expense_account != clearing)
		if is_mismatch:
			mismatch += 1
		rows.append(
			{
				"supplied_item": rm.name,
				"rm_item_code": rm.rm_item_code,
				"amount": rm.amount,
				"current_expense_account": rm.expense_account,
				"intended_clearing_account": clearing,
				"mismatch": is_mismatch,
			}
		)

	gl = frappe.db.sql(
		"""
		SELECT account, SUM(debit) debit, SUM(credit) credit
		FROM `tabGL Entry`
		WHERE voucher_no=%s AND is_cancelled=0
		GROUP BY account
		""",
		(scr_name,),
		as_dict=True,
	)
	net_111421 = 0.0
	has_611233 = False
	for g in gl:
		acc = g.account or ""
		if acc.startswith("111421"):
			net_111421 += float(g.debit or 0) - float(g.credit or 0)
		if "611233" in acc and float(g.debit or 0) > 0:
			has_611233 = True

	if not policy:
		category = "out_of_policy"
	elif mismatch == 0 and abs(net_111421) <= 0.51 and not has_611233:
		category = "correct"
	elif mismatch == 0 and (abs(net_111421) > 0.51 or has_611233):
		category = "incorrect_gl_mapping_ok"
	elif mismatch > 0:
		category = "incorrect_rm_fg_mapping"
	else:
		category = "ambiguous"

	return {
		"subcontracting_receipt": scr_name,
		"docstatus": doc.docstatus,
		"company": doc.company,
		"posting_date": str(doc.posting_date),
		"uses_scr_clearing_policy": policy,
		"rows": rows,
		"mismatch_count": mismatch,
		"gl_net_111421": net_111421,
		"gl_has_611233_rm_debit": has_611233,
		"category": category,
	}


@frappe.whitelist(methods=["GET", "POST"])
def inspect_scr_clearing_expense_alignment(subcontracting_receipt: str) -> dict:
	"""Read-only report of FG vs supplied RM expense_account mismatch."""
	_require_privileged()
	if not subcontracting_receipt:
		frappe.throw(_("subcontracting_receipt is required"))
	report = _classify_scr(subcontracting_receipt)
	report["api_version"] = API_VERSION
	return report


@frappe.whitelist(methods=["GET", "POST"])
def discover_scr_clearing_expense_mismatches(
	company: str | None = None,
	limit: int | None = 200,
) -> dict:
	"""Discover submitted Iran SCRs with clearing mapping / GL defects."""
	_require_privileged()
	limit = cint(limit) or 200
	filters: dict = {"docstatus": 1, "is_return": 0}
	if company:
		filters["company"] = company
	names = frappe.get_all(
		"Subcontracting Receipt",
		filters=filters,
		pluck="name",
		order_by="posting_date desc, creation desc",
		limit_page_length=limit,
	)
	reports = [_classify_scr(n) for n in names]
	counts: dict[str, int] = {}
	for r in reports:
		counts[r["category"]] = counts.get(r["category"], 0) + 1
	return {
		"api_version": API_VERSION,
		"inspected": len(reports),
		"category_counts": counts,
		"receipts": reports,
	}


@frappe.whitelist(methods=["POST"])
def align_scr_supplied_clearing_expense(
	subcontracting_receipt: str,
	confirm: str | None = None,
) -> dict:
	"""Persist supplied RM expense_account = FG clearing on a submitted Iran SCR.

	Uses Document ``db_update`` on child rows (submitted field is not
	allow_on_submit). Does not change qty, rates, amounts, PO, or SCO. GL/SLE
	are unchanged until a supported Transaction / accounting RIV runs.
	"""
	_require_privileged()
	if confirm != "ALIGN_SCR_CLEARING_EXPENSE":
		frappe.throw(
			_("confirm must be ALIGN_SCR_CLEARING_EXPENSE"),
			frappe.ValidationError,
		)
	if not subcontracting_receipt:
		frappe.throw(_("subcontracting_receipt is required"))

	doc = frappe.get_doc("Subcontracting Receipt", subcontracting_receipt)
	if not uses_scr_clearing_balance_gl(doc):
		frappe.throw(_("SCR is outside Iran clearing policy scope"))
	if int(doc.docstatus) != 1:
		frappe.throw(_("Only submitted Subcontracting Receipts can be aligned via this API"))

	before = inspect_scr_clearing_expense_alignment(subcontracting_receipt)
	if before["mismatch_count"] == 0:
		return {
			"api_version": API_VERSION,
			"subcontracting_receipt": doc.name,
			"status": "already_aligned",
			"changed": [],
			"before": before,
		}

	changed = enforce_scr_clearing_expense_accounts(doc)
	for rm in doc.get("supplied_items") or []:
		# Document API persistence for submitted child (not allow_on_submit).
		rm.db_update()

	frappe.get_doc(
		{
			"doctype": "Comment",
			"comment_type": "Info",
			"reference_doctype": "Subcontracting Receipt",
			"reference_name": doc.name,
			"content": (
				f"SCR clearing expense alignment ({API_VERSION}) by {frappe.session.user} "
				f"at {now_datetime()}. Changed: {changed}"
			),
		}
	).insert(ignore_permissions=True)

	frappe.db.commit()
	after = inspect_scr_clearing_expense_alignment(subcontracting_receipt)
	return {
		"api_version": API_VERSION,
		"subcontracting_receipt": doc.name,
		"status": "aligned",
		"changed": changed,
		"before": before,
		"after": after,
	}

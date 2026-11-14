# Copyright (c) 2026, ERPNext Extensions contributors
"""Stock Entry hooks for MCA — guard submitted additional_costs edits."""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt

from erpnext_extensions.manufacturing_cost_adjustment.constants import CF_MCA, CF_MCA_COST_ROW


def on_update_after_submit(doc, method=None):
	"""Reject unauthorized edits to additional_costs on submitted Stock Entry.

	Allowed:
	- MCA engine (`doc.flags.mca_engine_update`)
	- RIV / stock repost paths (`frappe.flags.through_repost_item_valuation`)

	Blocked:
	- Removing original (untagged) rows
	- Removing MCA-tagged rows outside the engine
	- Changing expense_account / amount / base_amount on existing rows
	"""
	if doc.doctype != "Stock Entry" or doc.docstatus != 1:
		return
	if getattr(doc.flags, "mca_engine_update", False):
		return
	if getattr(frappe.flags, "through_repost_item_valuation", False):
		return
	if getattr(frappe.flags, "in_mca_pipeline", False):
		return

	db_rows = frappe.get_all(
		"Landed Cost Taxes and Charges",
		filters={"parent": doc.name, "parenttype": "Stock Entry"},
		fields=[
			"name",
			CF_MCA,
			CF_MCA_COST_ROW,
			"expense_account",
			"amount",
			"base_amount",
		],
	)
	db_by_name = {r.name: r for r in db_rows}
	form_by_name = {r.name: r for r in (doc.get("additional_costs") or []) if r.name}

	for name, row in db_by_name.items():
		if name not in form_by_name:
			if row.get(CF_MCA):
				frappe.throw(
					_(
						"Cannot remove MCA Additional Cost row {0} from submitted Stock Entry. "
						"Cancel the Stock Entry to void linked Manufacturing Cost Adjustments."
					).format(name)
				)
			frappe.throw(
				_("Cannot remove original Additional Cost row {0} from submitted Stock Entry.").format(
					name
				)
			)
		form_row = form_by_name[name]
		if (form_row.expense_account or "") != (row.expense_account or ""):
			frappe.throw(
				_("Cannot change Expense Account on submitted Additional Cost row {0}.").format(name)
			)
		if abs(flt(form_row.amount) - flt(row.amount)) > 1e-9 or abs(
			flt(form_row.base_amount) - flt(row.base_amount)
		) > 1e-9:
			frappe.throw(
				_("Cannot change Amount on submitted Additional Cost row {0}.").format(name)
			)

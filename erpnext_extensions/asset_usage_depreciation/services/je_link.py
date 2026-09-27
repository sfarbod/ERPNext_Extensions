# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

"""Repair Depreciation Entry → ADS row linking (date + whole-IRR amount)."""

from __future__ import annotations

import frappe
from frappe.utils import flt, getdate

from erpnext.assets.doctype.asset_depreciation_schedule.asset_depreciation_schedule import (
	get_depr_schedule,
)

from erpnext_extensions.asset_usage_depreciation.services.accounting_amounts import to_depr_amount


def ensure_depreciation_schedule_je_link(doc, method=None):
	"""Journal Entry on_submit: ensure ADS row is linked for Depreciation Entry."""
	if getattr(doc, "voucher_type", None) != "Depreciation Entry":
		return
	if frappe.flags.get("usage_replan_in_progress"):
		return
	if frappe.flags.get("asset_depr_reset_in_progress"):
		return

	for je_row in doc.get("accounts") or []:
		if not (
			je_row.reference_type == "Asset"
			and je_row.reference_name
			and flt(je_row.debit)
			and frappe.get_cached_value("Account", je_row.account, "root_type") == "Expense"
		):
			continue

		if not frappe.db.get_value("Asset", je_row.reference_name, "calculate_depreciation"):
			continue

		depr_schedule = get_depr_schedule(je_row.reference_name, "Active", doc.finance_book)
		posting = getdate(doc.posting_date)
		je_amt = to_depr_amount(je_row.debit)

		for schedule_row in depr_schedule or []:
			if schedule_row.journal_entry:
				if schedule_row.journal_entry == doc.name:
					return
				continue
			if getdate(schedule_row.schedule_date) != posting:
				continue
			# Prefer whole-IRR equality; fall back to date-only when amounts already match via normalize
			sched_amt = to_depr_amount(schedule_row.depreciation_amount)
			if sched_amt != je_amt:
				continue
			frappe.db.set_value("Depreciation Schedule", schedule_row.name, "journal_entry", doc.name)
			return

		# Date-unique fallback: if exactly one unlinked row shares posting date, link it.
		candidates = [
			r
			for r in (depr_schedule or [])
			if not r.journal_entry and getdate(r.schedule_date) == posting
		]
		if len(candidates) == 1:
			frappe.db.set_value("Depreciation Schedule", candidates[0].name, "journal_entry", doc.name)
			return

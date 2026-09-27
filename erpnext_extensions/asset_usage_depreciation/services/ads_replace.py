# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

from __future__ import annotations

from typing import Any

import frappe
from frappe import _
from frappe.utils import flt, get_link_to_form

from erpnext_extensions.asset_usage_depreciation.services.accounting_amounts import to_depr_amount


def replace_asset_depr_schedule(
	old_ads,
	new_rows: list[dict[str, Any]],
	notes: str,
):
	"""Cancel Active ADS without cancelling posted JEs; submit replacement with new rows.

	Amounts on unposted rows must already be whole IRR (caller runs ads_amount_normalize).
	``before_submit`` re-normalizes full unposted schedules as a safety net.
	"""
	new_ads = frappe.copy_doc(old_ads)
	new_ads.depreciation_schedule = []
	new_ads.amended_from = None
	new_ads.status = "Draft"

	for row in new_rows:
		amount = (
			flt(row["depreciation_amount"])
			if row.get("journal_entry")
			else to_depr_amount(row["depreciation_amount"])
		)
		accum = row.get("accumulated_depreciation_amount")
		if accum is None:
			accum = 0
		elif not row.get("journal_entry"):
			accum = to_depr_amount(accum)

		new_ads.append(
			"depreciation_schedule",
			{
				"schedule_date": row["schedule_date"],
				"depreciation_amount": amount,
				"accumulated_depreciation_amount": accum,
				"journal_entry": row.get("journal_entry"),
				"shift": row.get("shift"),
			},
		)

	new_ads.notes = notes

	old_ads.flags.should_not_cancel_depreciation_entries = True
	old_ads.cancel()

	# When schedule still has posted rows, skip full-schedule normalize in hooks
	# (tail already normalized by caller). Full unposted schedules are normalized on submit.
	if any(r.get("journal_entry") for r in new_rows):
		new_ads.flags.skip_iran_ads_normalize = True

	new_ads.submit()
	return new_ads


def build_replan_notes(asset_name: str, trigger_doc=None, suspended_message: str | None = None) -> str:
	parts = [
		_("This schedule was replanned for Asset {0} by Asset Usage Depreciation.").format(
			get_link_to_form("Asset", asset_name)
		)
	]
	if trigger_doc:
		parts.append(
			_("Trigger: {0} {1}.").format(
				trigger_doc.doctype,
				get_link_to_form(trigger_doc.doctype, trigger_doc.name),
			)
		)
	if suspended_message:
		parts.append(str(suspended_message))
	return " ".join(parts)


def recompute_accumulated(rows: list[dict[str, Any]], opening_accumulated: float, precision: int | None = None) -> None:
	accum = to_depr_amount(opening_accumulated)
	for row in rows:
		if row.get("journal_entry") and row.get("accumulated_depreciation_amount") is not None:
			accum = to_depr_amount(row["accumulated_depreciation_amount"])
			continue
		accum = to_depr_amount(accum + to_depr_amount(row["depreciation_amount"]))
		row["accumulated_depreciation_amount"] = accum

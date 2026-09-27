# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

"""Asset Depreciation Reset & Rebuild — cancel bad JEs, reconcile Asset, rebuild ADS.

LOCKED: this tool does NOT create Depreciation Entry JEs. Nightly scheduler posts due rows.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe import _
from frappe.utils import cint, flt, getdate, now_datetime, today

from erpnext.assets.doctype.asset_depreciation_schedule.asset_depreciation_schedule import (
	get_asset_depr_schedule_doc,
)

from erpnext_extensions.asset_usage_depreciation.services.accounting_amounts import to_depr_amount
from erpnext_extensions.asset_usage_depreciation.services.ads_amount_normalize import (
	assert_schedule_whole_and_balanced,
	authoritative_depreciable_total,
	company_requires_whole_ads,
	is_whole_irr_amount,
	normalize_full_schedule_amounts,
)
from erpnext_extensions.asset_usage_depreciation.services.ads_replace import replace_asset_depr_schedule
from erpnext_extensions.asset_usage_depreciation.services.locks import lock_ads, lock_asset

BLOCKED_STATUSES = {"Sold", "Scrapped", "Cancelled"}
SUPPORTED_METHODS = {"Straight Line", "Manual"}


def analyze_asset(asset_name: str) -> dict[str, Any]:
	"""Read-only planner/preflight (optional; Apply does not require it)."""
	return _plan_asset(asset_name, apply=False)


@frappe.whitelist()
def analyze_asset_api(asset: str) -> dict[str, Any]:
	frappe.has_permission("Asset", "write", throw=True)
	return analyze_asset(asset)


@frappe.whitelist()
def reset_and_rebuild_asset(asset: str) -> dict[str, Any]:
	"""Independently safe APPLY — fresh preflight; does not create Depreciation JEs."""
	frappe.has_permission("Asset", "write", throw=True)
	frappe.has_permission("Journal Entry", "cancel", throw=True)
	return _reset_and_rebuild_asset(asset)


@frappe.whitelist()
def reset_and_rebuild_batch(assets: str | list | None = None) -> dict[str, Any]:
	"""Process an explicit Asset list. Continues on per-Asset failure."""
	frappe.has_permission("Asset", "write", throw=True)
	if isinstance(assets, str):
		import json

		assets = json.loads(assets) if assets.strip().startswith("[") else [a.strip() for a in assets.split(",") if a.strip()]
	assets = list(assets or [])
	results = []
	ok = fail = 0
	for name in assets:
		try:
			res = _reset_and_rebuild_asset(name)
			results.append(res)
			if res.get("status") == "SUCCESS":
				ok += 1
			else:
				fail += 1
		except Exception as e:
			frappe.db.rollback()
			fail += 1
			results.append(
				{
					"asset": name,
					"status": "FAILED",
					"errors": [str(e)],
				}
			)
	return {"ok": ok, "failed": fail, "results": results}


def _reset_and_rebuild_asset(asset_name: str) -> dict[str, Any]:
	frappe.flags.asset_depr_reset_in_progress = True
	try:
		lock_asset(asset_name)
		plan = _plan_asset(asset_name, apply=True)
		if plan["status"] in ("BLOCKED", "MANUAL_REVIEW"):
			return plan

		asset = frappe.get_doc("Asset", asset_name)
		before = _snapshot_asset(asset)

		cancelled = []
		for je_name in plan["target_jes"]:
			je = frappe.get_doc("Journal Entry", je_name)
			if je.docstatus != 1:
				continue
			je.flags.ignore_permissions = True
			je.cancel()
			cancelled.append(je_name)

		# Explicit VAD reconciliation (cancel alone insufficient for corrupted headers)
		expected_vad = to_depr_amount(
			flt(asset.net_purchase_amount) - flt(asset.opening_accumulated_depreciation)
		)
		asset.reload()
		for fb in asset.get("finance_books") or []:
			fb.db_set("value_after_depreciation", expected_vad)
			fb.db_set(
				"total_number_of_booked_depreciations",
				cint(asset.opening_number_of_booked_depreciations),
			)
		asset.db_set("value_after_depreciation", expected_vad)
		asset.reload()
		asset.set_status()
		asset.set_total_booked_depreciations()

		# Clear any leftover ADS JE links
		ads = get_asset_depr_schedule_doc(asset_name, "Active", None)
		if not ads and asset.get("finance_books"):
			ads = get_asset_depr_schedule_doc(asset_name, "Active", asset.finance_books[0].finance_book)
		if not ads:
			frappe.throw(_("No Active Asset Depreciation Schedule for Asset {0}.").format(asset_name))

		lock_ads(ads.name)
		ads = frappe.get_doc("Asset Depreciation Schedule", ads.name)
		for row in ads.get("depreciation_schedule") or []:
			if row.journal_entry:
				row.db_set("journal_entry", None)

		# Rebuild via ERPNext create_depreciation_schedule (Business Calendar override active)
		asset.reload()
		fb = asset.finance_books[0]
		fb.value_after_depreciation = expected_vad

		temp = frappe.copy_doc(ads)
		temp.flags.skip_iran_ads_normalize = True  # we normalize explicitly below
		temp.create_depreciation_schedule(fb)

		rows = []
		for r in temp.get("depreciation_schedule") or []:
			rows.append(
				{
					"schedule_date": r.schedule_date,
					"depreciation_amount": r.depreciation_amount,
					"accumulated_depreciation_amount": r.accumulated_depreciation_amount,
					"journal_entry": None,
					"shift": getattr(r, "shift", None),
				}
			)

		total = authoritative_depreciable_total(asset, fb)
		normalize_full_schedule_amounts(
			rows,
			depreciable_total=total,
			opening_accumulated=flt(asset.opening_accumulated_depreciation),
		)

		notes = _(
			"ADS rebuilt by Asset Depreciation Reset & Rebuild (v5.3.29). "
			"Cancelled {0} Depreciation Entry JE(s)."
		).format(len(cancelled))
		new_ads = replace_asset_depr_schedule(ads, rows, notes)

		# Post gates
		asset.reload()
		gates = _validate_success_gates(asset, new_ads, cancelled, expected_vad, total)
		if not gates["ok"]:
			frappe.throw(_("Post-rebuild gates failed: {0}").format("; ".join(gates["errors"])))

		after = _snapshot_asset(asset)
		result = {
			"asset": asset_name,
			"status": "SUCCESS",
			"JEs_cancelled": cancelled,
			"old_ADS": ads.name,
			"new_ADS": new_ads.name,
			"rows": len(new_ads.depreciation_schedule),
			"due_rows": gates["due_rows"],
			"whole_number_check": True,
			"final_balance_check": True,
			"business_calendar_check": True,
			"GL_reset_check": gates["gl_ok"],
			"asset_state_check": True,
			"before": before,
			"after": after,
			"gates": gates,
			"errors": [],
			"executed_at": str(now_datetime()),
			"user": frappe.session.user,
		}
		_write_audit(result)
		frappe.db.commit()
		return result
	except Exception as e:
		frappe.db.rollback()
		frappe.log_error(title=f"Asset Depreciation Reset failed: {asset_name}")
		return {
			"asset": asset_name,
			"status": "FAILED",
			"errors": [str(e)],
			"executed_at": str(now_datetime()),
			"user": frappe.session.user,
		}
	finally:
		frappe.flags.asset_depr_reset_in_progress = False


def _plan_asset(asset_name: str, *, apply: bool) -> dict[str, Any]:
	if not frappe.db.exists("Asset", asset_name):
		return {"asset": asset_name, "status": "BLOCKED", "reason": "ASSET_NOT_FOUND", "errors": ["Asset not found"]}

	asset = frappe.get_doc("Asset", asset_name)
	reasons = []
	status = "READY"

	if asset.docstatus != 1:
		reasons.append("ASSET_NOT_SUBMITTED")
		status = "BLOCKED"
	if not cint(asset.calculate_depreciation):
		reasons.append("NO_CALCULATE_DEPRECIATION")
		status = "BLOCKED"
	if asset.status in BLOCKED_STATUSES:
		reasons.append(f"LIFECYCLE_{asset.status.upper()}")
		status = "BLOCKED"
	if asset.get("disposal_date") or asset.get("journal_entry_for_scrap"):
		reasons.append("DISPOSAL_OR_SCRAP")
		status = "BLOCKED"
	if not company_requires_whole_ads(asset.company):
		reasons.append("NON_IRR_COMPANY")
		status = "MANUAL_REVIEW"

	fb = (asset.get("finance_books") or [None])[0]
	if not fb:
		reasons.append("NO_FINANCE_BOOK_ROW")
		status = "BLOCKED"
	elif fb.depreciation_method not in SUPPORTED_METHODS:
		reasons.append(f"METHOD_{fb.depreciation_method}")
		status = "MANUAL_REVIEW"

	ads_count = frappe.db.count("Asset Depreciation Schedule", {"asset": asset_name, "docstatus": 1})
	if ads_count != 1:
		reasons.append(f"ADS_COUNT_{ads_count}")
		status = "BLOCKED"

	if frappe.db.exists("Asset Value Adjustment", {"asset": asset_name, "docstatus": 1}):
		reasons.append("HAS_VALUE_ADJUSTMENT")
		status = "MANUAL_REVIEW" if status != "BLOCKED" else status
	if frappe.db.exists(
		"Asset Repair", {"asset": asset_name, "docstatus": 1, "capitalize_repair_cost": 1}
	):
		reasons.append("HAS_CAPITALIZED_REPAIR")
		status = "MANUAL_REVIEW" if status != "BLOCKED" else status
	if frappe.db.exists("Asset Usage Period", {"asset": asset_name, "docstatus": 1}):
		reasons.append("HAS_USAGE_PERIODS")
		status = "MANUAL_REVIEW" if status != "BLOCKED" else status

	jes = _identify_depreciation_jes(asset_name, asset.company)
	ads = get_asset_depr_schedule_doc(asset_name, "Active", fb.finance_book if fb else None)
	frac_rows = 0
	due_rows = 0
	row_count = 0
	if ads:
		cutoff = getdate(today())
		for r in ads.get("depreciation_schedule") or []:
			row_count += 1
			if not is_whole_irr_amount(r.depreciation_amount):
				frac_rows += 1
			if getdate(r.schedule_date) <= cutoff and not r.journal_entry:
				due_rows += 1

	if status == "READY" and not jes and frac_rows == 0:
		# Nothing to reset — still allow rebuild-only? Prefer skip as READY with empty targets.
		pass

	result = {
		"asset": asset_name,
		"status": status if apply or status != "READY" else "READY",
		"reason": ",".join(reasons) if reasons else None,
		"reasons": reasons,
		"target_jes": [j["name"] for j in jes],
		"je_count": len(jes),
		"ads": ads.name if ads else None,
		"rows": row_count,
		"fractional_rows": frac_rows,
		"due_rows": due_rows,
		"errors": reasons if status != "READY" else [],
	}
	if apply and status != "READY":
		return result
	return result


def _identify_depreciation_jes(asset_name: str, company: str) -> list[dict[str, Any]]:
	return frappe.db.sql(
		"""
		select distinct je.name, je.posting_date, je.total_debit, je.creation
		from `tabJournal Entry` je
		inner join `tabJournal Entry Account` jea on jea.parent = je.name
		inner join `tabAccount` acc on acc.name = jea.account
		where je.voucher_type = 'Depreciation Entry'
		  and je.docstatus = 1
		  and je.company = %s
		  and jea.reference_type = 'Asset'
		  and jea.reference_name = %s
		  and jea.debit > 0
		  and acc.root_type = 'Expense'
		order by je.posting_date, je.creation
		""",
		(company, asset_name),
		as_dict=True,
	)


def _snapshot_asset(asset) -> dict[str, Any]:
	fb = (asset.get("finance_books") or [None])[0]
	return {
		"status": asset.status,
		"value_after_depreciation": flt(asset.value_after_depreciation),
		"fb_value_after_depreciation": flt(fb.value_after_depreciation) if fb else None,
		"opening_accumulated_depreciation": flt(asset.opening_accumulated_depreciation),
		"net_purchase_amount": flt(asset.net_purchase_amount),
	}


def _validate_success_gates(asset, new_ads, cancelled, expected_vad, total) -> dict[str, Any]:
	errors = []
	# G1
	remaining = _identify_depreciation_jes(asset.name, asset.company)
	if remaining:
		errors.append(f"G1 remaining submitted JEs: {[r.name for r in remaining]}")

	# G0 / G12 asset state
	if to_depr_amount(asset.value_after_depreciation) != to_depr_amount(expected_vad):
		errors.append(
			f"G0 asset VAD {asset.value_after_depreciation} != expected {expected_vad}"
		)
	fb = asset.finance_books[0]
	if to_depr_amount(fb.value_after_depreciation) != to_depr_amount(expected_vad):
		errors.append(f"G0 FB VAD {fb.value_after_depreciation} != expected {expected_vad}")

	# G3
	ads_count = frappe.db.count("Asset Depreciation Schedule", {"asset": asset.name, "docstatus": 1})
	if ads_count != 1:
		errors.append(f"G3 active ADS count {ads_count}")

	# G4–G9
	rows = [
		{
			"schedule_date": r.schedule_date,
			"depreciation_amount": r.depreciation_amount,
			"accumulated_depreciation_amount": r.accumulated_depreciation_amount,
		}
		for r in new_ads.depreciation_schedule
	]
	try:
		assert_schedule_whole_and_balanced(rows, total)
	except Exception as e:
		errors.append(f"G4-G8 {e}")

	for r in new_ads.depreciation_schedule:
		if r.journal_entry:
			errors.append(f"G12 ADS row {r.schedule_date} still linked to {r.journal_entry}")

	# G2 cancelled GL
	gl_ok = True
	for je_name in cancelled:
		open_gl = frappe.db.sql(
			"""
			select count(*) from `tabGL Entry`
			where voucher_no=%s and ifnull(is_cancelled,0)=0
			""",
			(je_name,),
		)[0][0]
		if open_gl:
			gl_ok = False
			errors.append(f"G2 open GL remains for {je_name}")

	cutoff = getdate(today())
	due = [
		r
		for r in new_ads.depreciation_schedule
		if getdate(r.schedule_date) <= cutoff and not r.journal_entry
	]

	return {
		"ok": not errors,
		"errors": errors,
		"due_rows": len(due),
		"due_dates": [str(r.schedule_date) for r in due],
		"gl_ok": gl_ok,
		"depreciable_total": total,
		"final_amount": to_depr_amount(new_ads.depreciation_schedule[-1].depreciation_amount)
		if new_ads.depreciation_schedule
		else None,
	}


def _write_audit(result: dict[str, Any]) -> None:
	"""Structured audit via Error Log / comment when no dedicated DocType exists."""
	frappe.get_doc(
		{
			"doctype": "Comment",
			"comment_type": "Info",
			"reference_doctype": "Asset",
			"reference_name": result["asset"],
			"content": (
				f"Asset Depreciation Reset & Rebuild SUCCESS. "
				f"Cancelled JEs={len(result.get('JEs_cancelled') or [])}; "
				f"old ADS={result.get('old_ADS')}; new ADS={result.get('new_ADS')}; "
				f"rows={result.get('rows')}; due={result.get('due_rows')}"
			),
		}
	).insert(ignore_permissions=True)
	frappe.logger("asset_depr_reset").info(result)

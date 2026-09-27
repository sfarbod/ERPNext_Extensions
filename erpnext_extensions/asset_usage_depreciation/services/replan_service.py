# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

from __future__ import annotations

from typing import Any

import frappe
from frappe import _
from frappe.utils import add_days, cint, flt, getdate

from erpnext.assets.doctype.asset_activity.asset_activity import add_asset_activity
from erpnext.assets.doctype.asset_depreciation_schedule.asset_depreciation_schedule import (
	get_asset_depr_schedule_doc,
)

from erpnext_extensions.asset_usage_depreciation.constants import (
	COMPANY_FIELD_REDUCED_HANDLING,
	HANDLING_ADJUST_FINAL,
	HANDLING_EXTEND,
	HANDLING_REDISTRIBUTE_LEGACY,
)
from erpnext_extensions.asset_usage_depreciation.services.ads_replace import (
	build_replan_notes,
	recompute_accumulated,
	replace_asset_depr_schedule,
)
from erpnext_extensions.asset_usage_depreciation.services.accounting_amounts import (
	sum_unposted_amounts,
	to_depr_amount,
)
from erpnext_extensions.asset_usage_depreciation.services.ads_amount_normalize import (
	normalize_unposted_tail,
)
from erpnext_extensions.asset_usage_depreciation.services.locks import lock_ads, lock_asset
from erpnext_extensions.asset_usage_depreciation.services.mode_a import apply_mode_a_extension
from erpnext_extensions.asset_usage_depreciation.services.mode_b import apply_fixed_end_usage_adjustment
from erpnext_extensions.asset_usage_depreciation.services.usage_timeline import (
	day_weighted_factor,
	factor_on_date,
	load_submitted_usage_periods,
	validate_timeline_consistency,
)

# Recursion / re-entrancy guard (request-local via frappe.flags)
_FLAG_USAGE_REPLAN_IN_PROGRESS = "usage_replan_in_progress"


def asset_has_submitted_usage_periods(asset_name: str) -> bool:
	return bool(
		frappe.db.exists(
			"Asset Usage Period",
			{"asset": asset_name, "docstatus": 1},
		)
	)


def get_reduced_depreciation_handling(company: str) -> str:
	"""Return the Company reduced-depreciation policy.

	Maps legacy ``Redistribute Within Remaining Schedule`` to
	``Adjust Final Depreciation Installment``.
	"""
	value = frappe.db.get_value("Company", company, COMPANY_FIELD_REDUCED_HANDLING)
	if value in (HANDLING_ADJUST_FINAL, HANDLING_REDISTRIBUTE_LEGACY):
		return HANDLING_ADJUST_FINAL
	if value == HANDLING_EXTEND:
		return HANDLING_EXTEND
	return HANDLING_EXTEND


def replan_asset_usage_depreciation(asset_name: str, trigger_doc=None, context: dict | None = None):
	"""Single orchestration entry point for usage-driven ADS replanning."""
	from erpnext_extensions.asset_usage_depreciation.services.transition_service import (
		is_usage_transition_in_progress,
	)

	if is_usage_transition_in_progress():
		return

	if frappe.flags.get(_FLAG_USAGE_REPLAN_IN_PROGRESS):
		return

	context = context or {}
	if context.get("skip_if_no_usage") and not asset_has_submitted_usage_periods(asset_name):
		return

	# Always no-op when no submitted usage periods (except when called from Usage Period itself
	# which may be mid-cancel — still load timeline after state change).
	frappe.flags[_FLAG_USAGE_REPLAN_IN_PROGRESS] = True
	try:
		_replan_asset_usage_depreciation(asset_name, trigger_doc=trigger_doc, context=context)
	finally:
		frappe.flags[_FLAG_USAGE_REPLAN_IN_PROGRESS] = False


def _replan_asset_usage_depreciation(asset_name: str, trigger_doc=None, context: dict | None = None):
	lock_asset(asset_name)
	asset = frappe.get_doc("Asset", asset_name)

	if not asset.calculate_depreciation:
		return

	timeline = load_submitted_usage_periods(asset_name)
	if not timeline and not (trigger_doc and trigger_doc.doctype == "Asset Usage Period"):
		# LOCKED: no Usage Periods ⇒ implicit Normal; leave standard ERPNext ADS untouched
		# (do not replace ADS merely to reaffirm default factor 1.0).
		return

	if timeline:
		validate_timeline_consistency(timeline)

	policy = get_reduced_depreciation_handling(asset.company)
	# Whole-number accounting amounts (Iran Accounting ROUND_HALF_UP precision 0)
	precision = 0

	# Re-validate timeline under Asset lock
	timeline = load_submitted_usage_periods(asset_name)
	if timeline:
		validate_timeline_consistency(timeline)

	# Discover Active ADS for each finance book row
	ads_jobs: list[tuple[Any, Any]] = []
	for fb in asset.get("finance_books") or []:
		ads = get_asset_depr_schedule_doc(asset.name, "Active", fb.finance_book)
		if not ads:
			continue
		ads_jobs.append((fb, ads))

	if not ads_jobs:
		frappe.throw(
			_("No Active Asset Depreciation Schedule found for Asset {0}.").format(asset_name)
		)

	# Preflight all ADS before mutating any
	for fb, ads in ads_jobs:
		_validate_supported_method(fb, ads)
		_validate_posted_prefix(ads)
		_validate_no_draft_depreciation_je(asset, ads)

	prepared: list[dict[str, Any]] = []

	for fb, ads in ads_jobs:
		lock_ads(ads.name)
		# Reload after lock
		ads = frappe.get_doc("Asset Depreciation Schedule", ads.name)
		_validate_posted_prefix(ads)
		_validate_no_draft_depreciation_je(asset, ads)

		posted_snapshot = _capture_posted_snapshot(ads)

		# Standard ERPNext rebuild of unposted tail
		temp = frappe.copy_doc(ads)
		temp.create_depreciation_schedule(fb)

		rows = _schedule_to_row_dicts(temp)
		# Capture ERPNext standard baseline BEFORE any usage factoring so Mode B
		# (and Mode A base estimates) never start from a prior balancing row.
		for row in rows:
			row["_standard_amount"] = flt(row["depreciation_amount"])

		remaining = to_depr_amount(
			flt(fb.value_after_depreciation) - flt(fb.expected_value_after_useful_life)
		)
		suspended_message = None

		if not timeline:
			# Usage cancelled: ERPNext standard amounts → Iran whole-number + life-final balance
			normalize_unposted_tail(
				rows,
				remaining_depreciable=remaining,
				opening_accumulated=flt(asset.opening_accumulated_depreciation),
				allow_incomplete=False,
			)
		elif policy == HANDLING_ADJUST_FINAL:
			# Fixed-end: factor every unposted row except the final balancing row.
			# Do NOT pre-multiply via _apply_usage_factors (avoids double-counting).
			def _resolve(idx, row, _rows=rows, _timeline=timeline, _fb=fb, _asset=asset, _temp=temp):
				standard = flt(row.get("_standard_amount", row["depreciation_amount"]))
				factor = _usage_factor_for_row(_rows, idx, _timeline, _fb, _asset, _temp)
				return standard, factor

			apply_fixed_end_usage_adjustment(
				rows,
				remaining,
				resolve_amount_and_factor=_resolve,
			)
			# Mode B already balances final; re-assert whole IRR via shared gate
			normalize_unposted_tail(
				rows,
				remaining_depreciable=remaining,
				opening_accumulated=flt(asset.opening_accumulated_depreciation),
				allow_incomplete=False,
			)
		else:
			_apply_usage_factors(rows, timeline, fb, asset, temp)
			base_installment = _estimate_base_installment(rows)
			meta = apply_mode_a_extension(
				rows,
				remaining,
				timeline,
				frequency_of_depreciation=cint(fb.frequency_of_depreciation),
				base_installment=base_installment,
				resolve_factor_for_date=lambda d: factor_on_date(timeline, d),
				daily_prorata_based=cint(fb.daily_prorata_based),
			)
			suspended_message = meta.get("message")
			normalize_unposted_tail(
				rows,
				remaining_depreciable=remaining,
				opening_accumulated=flt(asset.opening_accumulated_depreciation),
				allow_incomplete=bool(suspended_message),
			)

		# Accum already set inside normalize_unposted_tail; keep explicit recompute for posted prefix
		recompute_accumulated(rows, flt(asset.opening_accumulated_depreciation))
		_assert_whole_unposted_amounts(rows)
		_assert_posted_unchanged(posted_snapshot, rows)

		notes = build_replan_notes(asset.name, trigger_doc, suspended_message)
		row_diffs = _build_row_diffs(ads, rows)

		prepared.append(
			{
				"ads": ads,
				"fb": fb,
				"rows": rows,
				"notes": notes,
				"row_diffs": row_diffs,
				"suspended_message": suspended_message,
			}
		)

	# Replace all ADS atomically (same DB transaction; throw rolls back)
	for item in prepared:
		new_ads = replace_asset_depr_schedule(item["ads"], item["rows"], item["notes"])
		_write_audit(asset, trigger_doc, item["ads"].name, new_ads.name, item["fb"], item["row_diffs"])
		if item.get("suspended_message"):
			add_asset_activity(asset.name, item["suspended_message"])


def _validate_supported_method(fb, ads) -> None:
	method = fb.depreciation_method or ads.depreciation_method
	shift_based = cint(fb.shift_based or ads.shift_based)
	if method != "Straight Line" or shift_based:
		frappe.throw(
			_(
				"Asset Usage Depreciation supports only Straight Line schedules without shift-based "
				"depreciation. Finance Book '{0}' uses method '{1}' (shift_based={2}). "
				"The entire usage replan was aborted."
			).format(fb.finance_book or _("(default)"), method, shift_based)
		)


def _validate_posted_prefix(ads) -> None:
	seen_unposted = False
	for row in ads.get("depreciation_schedule") or []:
		if row.journal_entry:
			if seen_unposted:
				frappe.throw(
					_(
						"Asset Depreciation Schedule {0} has a non-contiguous posted state "
						"(a submitted Journal Entry appears after an unposted row). "
						"Usage replan aborted. Do not auto-repair; resolve the schedule manually."
					).format(ads.name)
				)
			# Must be submitted JE
			je_status = frappe.db.get_value("Journal Entry", row.journal_entry, "docstatus")
			if je_status != 1:
				frappe.throw(
					_(
						"Depreciation Schedule row on {0} links Journal Entry {1} which is not submitted "
						"(docstatus={2}). Submit or cancel it before replanning."
					).format(row.schedule_date, row.journal_entry, je_status)
				)
		else:
			seen_unposted = True


def _validate_no_draft_depreciation_je(asset, ads) -> None:
	# Linked draft on schedule rows
	for row in ads.get("depreciation_schedule") or []:
		if not row.journal_entry:
			continue
		je_status = frappe.db.get_value("Journal Entry", row.journal_entry, "docstatus")
		if je_status == 0:
			frappe.throw(
				_(
					"Draft Depreciation Entry {0} is linked to the schedule. "
					"Submit or cancel it before usage-based replanning."
				).format(row.journal_entry)
			)

	# Unlinked draft Depreciation Entry referencing the asset
	drafts = frappe.db.sql(
		"""
		SELECT DISTINCT je.name
		FROM `tabJournal Entry` je
		INNER JOIN `tabJournal Entry Account` jea ON jea.parent = je.name
		WHERE je.docstatus = 0
			AND je.voucher_type = 'Depreciation Entry'
			AND jea.reference_type = 'Asset'
			AND jea.reference_name = %s
		LIMIT 1
		""",
		asset.name,
	)
	if drafts:
		frappe.throw(
			_(
				"Draft Depreciation Entry {0} references Asset {1}. "
				"Submit or cancel it before usage-based replanning."
			).format(drafts[0][0], asset.name)
		)


def _capture_posted_snapshot(ads) -> list[dict[str, Any]]:
	snapshot = []
	for row in ads.get("depreciation_schedule") or []:
		if not row.journal_entry:
			break
		snapshot.append(
			{
				"schedule_date": getdate(row.schedule_date),
				"depreciation_amount": flt(row.depreciation_amount),
				"accumulated_depreciation_amount": flt(row.accumulated_depreciation_amount),
				"journal_entry": row.journal_entry,
				"shift": row.shift,
			}
		)
	return snapshot


def _schedule_to_row_dicts(ads) -> list[dict[str, Any]]:
	rows = []
	for row in ads.get("depreciation_schedule") or []:
		rows.append(
			{
				"schedule_date": getdate(row.schedule_date),
				"depreciation_amount": flt(row.depreciation_amount),
				"accumulated_depreciation_amount": flt(row.accumulated_depreciation_amount),
				"journal_entry": row.journal_entry,
				"shift": row.shift,
				"usage_factor": 1.0,
			}
		)
	return rows


def _usage_factor_for_row(rows, idx, timeline, fb, asset, temp) -> float:
	if cint(fb.daily_prorata_based):
		start, end = _coverage_window_from_rows(rows, idx, fb, asset, temp)
		return day_weighted_factor(timeline, start, end)
	return factor_on_date(timeline, rows[idx]["schedule_date"])


def _apply_usage_factors(rows, timeline, fb, asset, temp) -> None:
	if not timeline:
		return

	for idx, row in enumerate(rows):
		if row.get("journal_entry"):
			continue
		standard = flt(row.get("_standard_amount", row["depreciation_amount"]))
		factor = _usage_factor_for_row(rows, idx, timeline, fb, asset, temp)
		row["usage_factor"] = factor
		# Keep full float until Mode A / salvage; normalize there
		row["depreciation_amount"] = standard * factor


def _coverage_window_from_rows(rows, idx, fb, asset, temp) -> tuple:
	end = getdate(rows[idx]["schedule_date"])
	if idx > 0:
		start = add_days(getdate(rows[idx - 1]["schedule_date"]), 1)
		return start, end
	if hasattr(temp, "_get_total_days"):
		try:
			start, _days = temp._get_total_days(fb.depreciation_start_date, idx)
			return getdate(start), end
		except Exception:
			pass
	return getdate(asset.available_for_use_date), end


def _estimate_base_installment(rows) -> float:
	"""Prefer a Normal (factor~1) unposted standard-equivalent installment."""
	candidates = []
	for row in rows:
		if row.get("journal_entry"):
			continue
		factor = flt(row.get("usage_factor") or 1)
		amt = flt(row.get("depreciation_amount"))
		if factor > 0 and amt > 0:
			candidates.append(amt / factor)
	if candidates:
		return candidates[0]
	return 0.0


def _enforce_salvage(rows, remaining, allow_incomplete: bool = False) -> None:
	target = to_depr_amount(remaining)
	# Normalize unposted first
	for row in rows:
		if row.get("journal_entry"):
			continue
		row["depreciation_amount"] = to_depr_amount(row["depreciation_amount"])

	total = sum_unposted_amounts(rows)

	if allow_incomplete:
		if total > target:
			frappe.throw(
				_("Usage replan would depreciate below salvage value (unposted {0} > remaining {1}).").format(
					total, target
				)
			)
		return

	diff = target - total
	unposted = [r for r in rows if not r.get("journal_entry")]
	if not unposted:
		if abs(diff) > 0:
			frappe.throw(
				_("No unposted rows available to allocate remaining depreciable value {0}.").format(diff)
			)
		return

	unposted[-1]["depreciation_amount"] = to_depr_amount(unposted[-1]["depreciation_amount"] + diff)
	if unposted[-1]["depreciation_amount"] < 0:
		frappe.throw(_("Salvage enforcement produced a negative depreciation amount."))


def _assert_whole_unposted_amounts(rows) -> None:
	for row in rows:
		if row.get("journal_entry"):
			continue
		amount = flt(row["depreciation_amount"])
		if abs(amount - int(amount)) > 1e-9:
			frappe.throw(
				_("Unposted depreciation amount {0} on {1} is not a whole number.").format(
					amount, row.get("schedule_date")
				)
			)
		row["depreciation_amount"] = int(amount)


def _assert_posted_unchanged(snapshot, rows) -> None:
	posted_rows = [r for r in rows if r.get("journal_entry")]
	if len(posted_rows) < len(snapshot):
		frappe.throw(_("Usage replan dropped posted depreciation rows. Aborted."))
	for i, expected in enumerate(snapshot):
		actual = posted_rows[i]
		if getdate(actual["schedule_date"]) != expected["schedule_date"]:
			frappe.throw(_("Posted schedule_date changed during usage replan. Aborted."))
		# Exact stored amount — do not re-round posted accounting history
		if flt(actual["depreciation_amount"]) != flt(expected["depreciation_amount"]):
			frappe.throw(_("Posted depreciation_amount changed during usage replan. Aborted."))
		if actual.get("journal_entry") != expected["journal_entry"]:
			frappe.throw(_("Posted journal_entry link changed during usage replan. Aborted."))


def _build_row_diffs(old_ads, new_rows) -> list[dict[str, Any]]:
	old_map = {}
	for row in old_ads.get("depreciation_schedule") or []:
		if row.journal_entry:
			continue
		old_map[getdate(row.schedule_date)] = to_depr_amount(row.depreciation_amount)

	diffs = []
	for row in new_rows:
		if row.get("journal_entry"):
			continue
		d = getdate(row["schedule_date"])
		old_amt = old_map.get(d)
		new_amt = to_depr_amount(row["depreciation_amount"])
		if old_amt is None or old_amt != new_amt:
			diffs.append({"schedule_date": d, "old_amount": old_amt, "new_amount": new_amt})
	return diffs


def _write_audit(asset, trigger_doc, old_ads_name, new_ads_name, fb, row_diffs) -> None:
	add_asset_activity(
		asset.name,
		_("Depreciation schedule replanned ({0} → {1}) due to Asset Usage Depreciation.").format(
			old_ads_name, new_ads_name
		),
	)

	if not trigger_doc or trigger_doc.doctype != "Asset Usage Period":
		return

	if trigger_doc.docstatus != 1:
		return

	# Insert child rows directly to avoid TimestampMismatch on the parent
	# document mid on_submit / before cancel.
	parent = trigger_doc.name
	idx = cint(frappe.db.sql(
		"SELECT COALESCE(MAX(idx), 0) FROM `tabAsset Usage Replan Log` WHERE parent=%s",
		parent,
	)[0][0])

	for diff in row_diffs:
		idx += 1
		frappe.get_doc(
			{
				"doctype": "Asset Usage Replan Log",
				"parent": parent,
				"parenttype": "Asset Usage Period",
				"parentfield": "replan_log",
				"idx": idx,
				"old_ads": old_ads_name,
				"new_ads": new_ads_name,
				"schedule_date": diff["schedule_date"],
				"old_amount": diff["old_amount"],
				"new_amount": diff["new_amount"],
				"finance_book": fb.finance_book,
			}
		).db_insert()


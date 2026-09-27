# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

"""Authoritative Iran whole-number ADS amount normalization & life-final balancing.

LOCKED (v5.3.29):
- Every persisted ADS ``depreciation_amount`` for IRR companies is whole IRR.
- Cumulative rounding residual is absorbed ONLY by the life-final schedule row.
- The final row itself must remain whole IRR.
- Do not use Python ``round()`` — use ``to_depr_amount`` / ``round_currency(..., 0)``.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe import _
from frappe.utils import flt

from erpnext_extensions.asset_usage_depreciation.services.accounting_amounts import to_depr_amount
from erpnext_extensions.iran_accounting.domain.currency import get_company_currency, is_irr_currency


def company_requires_whole_ads(company: str | None) -> bool:
	if not company:
		return False
	return is_irr_currency(get_company_currency(company))


def authoritative_depreciable_total(asset_doc, fb_row) -> int:
	"""Whole-IRR depreciable total from authoritative Asset economics."""
	raw = (
		flt(asset_doc.net_purchase_amount)
		- flt(asset_doc.opening_accumulated_depreciation)
		- flt(fb_row.expected_value_after_useful_life)
	)
	return to_depr_amount(raw)


def is_whole_irr_amount(value: Any) -> bool:
	amount = flt(value)
	return abs(amount - to_depr_amount(amount)) < 1e-9


def normalize_full_schedule_amounts(
	rows: list[dict[str, Any]],
	*,
	depreciable_total: int,
	opening_accumulated: float | int = 0,
) -> list[dict[str, Any]]:
	"""Normalize an entire (unposted) schedule: round 1..N-1, balance on life-final row N."""
	if not rows:
		return rows

	total = to_depr_amount(depreciable_total)
	if total < 0:
		frappe.throw(_("Depreciable total cannot be negative ({0}).").format(total))

	n = len(rows)
	if n == 1:
		rows[0]["depreciation_amount"] = total
	else:
		running = 0
		for i in range(n - 1):
			amt = to_depr_amount(rows[i].get("depreciation_amount"))
			if amt < 0:
				frappe.throw(
					_("Rounded depreciation amount for {0} is negative.").format(
						rows[i].get("schedule_date")
					)
				)
			rows[i]["depreciation_amount"] = amt
			running += amt
		final_amt = total - running
		if final_amt < 0:
			frappe.throw(
				_(
					"Life-final balancing installment would be negative ({0}). "
					"Check schedule economics / salvage."
				).format(final_amt)
			)
		# final_amt is integer by construction (int - int)
		rows[n - 1]["depreciation_amount"] = int(final_amt)

	_recompute_accumulated(rows, opening_accumulated)
	assert_schedule_whole_and_balanced(rows, total)
	return rows


def normalize_unposted_tail(
	rows: list[dict[str, Any]],
	*,
	remaining_depreciable: int,
	opening_accumulated: float | int = 0,
	allow_incomplete: bool = False,
) -> list[dict[str, Any]]:
	"""Normalize unposted rows only; life-final unposted row absorbs residual.

	Posted rows keep exact stored amounts. Remaining budget is
	``remaining_depreciable`` (typically FB value_after_depreciation - salvage),
	already whole via ``to_depr_amount``.
	"""
	target = to_depr_amount(remaining_depreciable)
	unposted = [r for r in rows if not r.get("journal_entry")]
	if not unposted:
		if abs(target) > 0 and not allow_incomplete:
			frappe.throw(
				_("No unposted rows available to allocate remaining depreciable value {0}.").format(
					target
				)
			)
		_recompute_accumulated(rows, opening_accumulated)
		return rows

	if allow_incomplete:
		for row in unposted:
			row["depreciation_amount"] = to_depr_amount(row.get("depreciation_amount"))
		total = sum(to_depr_amount(r["depreciation_amount"]) for r in unposted)
		if total > target:
			frappe.throw(
				_("Usage replan would depreciate below salvage value (unposted {0} > remaining {1}).").format(
					total, target
				)
			)
		_recompute_accumulated(rows, opening_accumulated)
		return rows

	if len(unposted) == 1:
		unposted[0]["depreciation_amount"] = target
	else:
		running = 0
		for row in unposted[:-1]:
			amt = to_depr_amount(row.get("depreciation_amount"))
			if amt < 0:
				frappe.throw(
					_("Rounded depreciation amount for {0} is negative.").format(row.get("schedule_date"))
				)
			row["depreciation_amount"] = amt
			running += amt
		final_amt = target - running
		if final_amt < 0:
			frappe.throw(_("Salvage / residual balancing produced a negative depreciation amount."))
		unposted[-1]["depreciation_amount"] = int(final_amt)

	_recompute_accumulated(rows, opening_accumulated)
	for row in unposted:
		if not is_whole_irr_amount(row["depreciation_amount"]):
			frappe.throw(
				_("Unposted depreciation amount {0} on {1} is not whole IRR.").format(
					row["depreciation_amount"], row.get("schedule_date")
				)
			)
	return rows


def _recompute_accumulated(rows: list[dict[str, Any]], opening_accumulated: float | int) -> None:
	accum = to_depr_amount(opening_accumulated)
	for row in rows:
		if row.get("journal_entry") and row.get("accumulated_depreciation_amount") is not None:
			# Preserve posted accounting history when an explicit accum is provided
			accum = to_depr_amount(row["accumulated_depreciation_amount"])
			row["accumulated_depreciation_amount"] = accum
			continue
		accum = to_depr_amount(accum + to_depr_amount(row.get("depreciation_amount")))
		row["accumulated_depreciation_amount"] = accum


def assert_schedule_whole_and_balanced(rows: list[dict[str, Any]], depreciable_total: int) -> None:
	total = to_depr_amount(depreciable_total)
	running = 0
	for row in rows:
		amt = row.get("depreciation_amount")
		if not is_whole_irr_amount(amt):
			frappe.throw(
				_("ADS depreciation_amount {0} on {1} is not whole IRR.").format(
					amt, row.get("schedule_date")
				)
			)
		running += to_depr_amount(amt)
		accum = row.get("accumulated_depreciation_amount")
		if accum is not None and not is_whole_irr_amount(accum):
			frappe.throw(
				_("ADS accumulated_depreciation_amount {0} on {1} is not whole IRR.").format(
					accum, row.get("schedule_date")
				)
			)
	if running != total:
		frappe.throw(
			_("ADS schedule sum {0} does not equal depreciable total {1}.").format(running, total)
		)


def normalize_ads_document(doc) -> None:
	"""Mutate an Asset Depreciation Schedule document in-place before submit/save."""
	company = None
	asset_name = getattr(doc, "asset", None)
	if asset_name:
		company = frappe.db.get_value("Asset", asset_name, "company")
	if not company_requires_whole_ads(company):
		return

	schedule = list(doc.get("depreciation_schedule") or [])
	if not schedule:
		return

	# Skip if any row already posted — usage/replace paths handle tails explicitly.
	if any(getattr(r, "journal_entry", None) for r in schedule):
		return

	asset = frappe.get_doc("Asset", asset_name)
	fb = None
	for row in asset.get("finance_books") or []:
		if cint_eq(row.idx, getattr(doc, "finance_book_id", None)) or (
			(row.finance_book or None) == (getattr(doc, "finance_book", None) or None)
		):
			fb = row
			break
	if fb is None and asset.get("finance_books"):
		fb = asset.finance_books[0]
	if fb is None:
		return

	rows = [
		{
			"schedule_date": r.schedule_date,
			"depreciation_amount": r.depreciation_amount,
			"accumulated_depreciation_amount": r.accumulated_depreciation_amount,
			"journal_entry": r.journal_entry,
			"_row": r,
		}
		for r in schedule
	]
	total = authoritative_depreciable_total(asset, fb)
	normalize_full_schedule_amounts(
		rows,
		depreciable_total=total,
		opening_accumulated=flt(asset.opening_accumulated_depreciation),
	)
	for item in rows:
		row = item["_row"]
		row.depreciation_amount = item["depreciation_amount"]
		row.accumulated_depreciation_amount = item["accumulated_depreciation_amount"]


def cint_eq(a, b) -> bool:
	try:
		return int(a or 0) == int(b or 0)
	except (TypeError, ValueError):
		return False

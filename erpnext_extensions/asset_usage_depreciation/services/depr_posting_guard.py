# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

"""Idempotent depreciation JE creation guard (Asset + schedule_date identity)."""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import getdate

from erpnext.assets.doctype.asset.depreciation import (
	_make_journal_entry_for_depreciation as _core_make_journal_entry_for_depreciation,
)

from erpnext_extensions.asset_usage_depreciation.services.accounting_amounts import to_depr_amount

_PATCHED = False


def find_submitted_depreciation_jes(
	asset_name: str, schedule_date, company: str | None = None
) -> list[str]:
	"""Return submitted Depreciation Entry names for Asset + schedule_date."""
	schedule_date = getdate(schedule_date)
	sql = """
		select distinct je.name
		from `tabJournal Entry` je
		inner join `tabJournal Entry Account` jea on jea.parent = je.name
		where je.voucher_type = 'Depreciation Entry'
		  and je.docstatus = 1
		  and je.posting_date = %s
		  and jea.reference_type = 'Asset'
		  and jea.reference_name = %s
		  and jea.debit > 0
	"""
	params: list = [schedule_date, asset_name]
	if company:
		sql += " and je.company = %s"
		params.append(company)
	sql += " order by je.creation asc"
	return [r[0] for r in frappe.db.sql(sql, params)]


def link_schedule_row_to_je(schedule_row_name: str, je_name: str) -> None:
	frappe.db.set_value("Depreciation Schedule", schedule_row_name, "journal_entry", je_name)


def resolve_existing_je_for_row(asset_name: str, schedule_row, company: str | None = None) -> str | None:
	"""Return JE to use for this ADS row, or None if a new JE may be created.

	Raises if multiple submitted JEs already exist for the logical period.
	"""
	if schedule_row.journal_entry:
		ds = frappe.db.get_value("Journal Entry", schedule_row.journal_entry, "docstatus")
		if _cint_safe(ds) == 1:
			return schedule_row.journal_entry
		frappe.db.set_value("Depreciation Schedule", schedule_row.name, "journal_entry", None)

	existing = find_submitted_depreciation_jes(asset_name, schedule_row.schedule_date, company)
	if not existing:
		return None
	if len(existing) > 1:
		frappe.throw(
			_(
				"Asset {0} has {1} submitted Depreciation Entries for schedule date {2}: {3}. "
				"Resolve duplicates before posting."
			).format(asset_name, len(existing), schedule_row.schedule_date, ", ".join(existing))
		)
	je_name = existing[0]
	link_schedule_row_to_je(schedule_row.name, je_name)
	return je_name


def guarded_make_journal_entry_for_depreciation(
	depr_schedule_doc,
	asset,
	date,
	depr_schedule,
	sch_start_idx,
	sch_end_idx,
	depr_cost_center,
	depr_series,
	credit_account,
	debit_account,
	accounting_dimensions,
):
	"""Wrapper: skip/link existing JE for Asset+schedule_date; else call core."""
	if not (sch_start_idx and sch_end_idx) and not (
		not depr_schedule.journal_entry and getdate(depr_schedule.schedule_date) <= getdate(date)
	):
		return

	# Campaign race: do not post while Asset is claimed by an active repair campaign
	try:
		from erpnext_extensions.asset_usage_depreciation.services.depr_reset_rebuild_campaign import (
			is_asset_claimed_by_active_campaign,
		)

		if is_asset_claimed_by_active_campaign(asset.name):
			frappe.logger("asset_depr_idempotency").info(
				"Skip depreciation posting for Asset %s — claimed by active repair campaign",
				asset.name,
			)
			return
	except Exception:
		# DocTypes may not be migrated yet; never block posting on import/schema errors
		pass

	existing = resolve_existing_je_for_row(asset.name, depr_schedule, asset.company)
	if existing:
		je_debit = frappe.db.sql(
			"""
			select jea.debit
			from `tabJournal Entry Account` jea
			inner join `tabAccount` acc on acc.name = jea.account
			where jea.parent = %s and jea.reference_type = 'Asset'
			  and jea.reference_name = %s and jea.debit > 0 and acc.root_type = 'Expense'
			limit 1
			""",
			(existing, asset.name),
		)
		if je_debit:
			expected = to_depr_amount(depr_schedule.depreciation_amount)
			actual = to_depr_amount(je_debit[0][0])
			if expected and actual and expected != actual:
				frappe.logger("asset_depr_idempotency").warning(
					"Depreciation JE %s amount %s differs from ADS %s expected %s for Asset %s",
					existing,
					actual,
					depr_schedule.schedule_date,
					expected,
					asset.name,
				)
		return

	return _core_make_journal_entry_for_depreciation(
		depr_schedule_doc,
		asset,
		date,
		depr_schedule,
		sch_start_idx,
		sch_end_idx,
		depr_cost_center,
		depr_series,
		credit_account,
		debit_account,
		accounting_dimensions,
	)


def install_depreciation_idempotency_patch() -> None:
	"""Monkey-patch core `_make_journal_entry_for_depreciation` (idempotent)."""
	global _PATCHED
	import erpnext.assets.doctype.asset.depreciation as depr_mod

	if getattr(depr_mod, "_make_journal_entry_for_depreciation", None) is guarded_make_journal_entry_for_depreciation:
		_PATCHED = True
		return
	depr_mod._make_journal_entry_for_depreciation = guarded_make_journal_entry_for_depreciation
	_PATCHED = True


def _cint_safe(value) -> int:
	try:
		return int(value or 0)
	except (TypeError, ValueError):
		return 0

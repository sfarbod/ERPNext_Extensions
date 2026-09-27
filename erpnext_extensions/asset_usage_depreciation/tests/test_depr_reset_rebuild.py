# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

"""Integration tests: Reset & Rebuild + scheduler idempotency (v5.3.29)."""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt, getdate, today

from erpnext.assets.doctype.asset.depreciation import make_depreciation_entry, post_depreciation_entries
from erpnext.assets.doctype.asset_depreciation_schedule.asset_depreciation_schedule import (
	get_asset_depr_schedule_doc,
)

from erpnext_extensions.asset_usage_depreciation.services.accounting_amounts import to_depr_amount
from erpnext_extensions.asset_usage_depreciation.services.ads_amount_normalize import is_whole_irr_amount
from erpnext_extensions.asset_usage_depreciation.services.depr_posting_guard import (
	install_depreciation_idempotency_patch,
)
from erpnext_extensions.asset_usage_depreciation.services.depr_reset_rebuild import (
	analyze_asset,
	reset_and_rebuild_asset,
)


def _submitted_depr_jes(asset: str) -> list[str]:
	return [
		r[0]
		for r in frappe.db.sql(
			"""
			select distinct je.name
			from `tabJournal Entry` je
			inner join `tabJournal Entry Account` jea on jea.parent=je.name
			where je.voucher_type='Depreciation Entry' and je.docstatus=1
			  and jea.reference_type='Asset' and jea.reference_name=%s and jea.debit>0
			""",
			(asset,),
		)
	]


def _ads_fractional_count(ads_name: str) -> int:
	n = 0
	for amt in frappe.db.sql(
		"select depreciation_amount from `tabDepreciation Schedule` where parent=%s",
		(ads_name,),
	):
		if not is_whole_irr_amount(amt[0]):
			n += 1
	return n


@frappe.whitelist()
def run_v5329_canary(asset: str = "3760", posting_runs: int = 40) -> dict:
	"""Controlled development canary — Reset/Rebuild then exercise posting path."""
	install_depreciation_idempotency_patch()
	frappe.set_user("Administrator")

	before_jes = _submitted_depr_jes(asset)
	plan = analyze_asset(asset)
	result = reset_and_rebuild_asset(asset)
	if result.get("status") != "SUCCESS":
		return {"phase": "reset", "result": result, "plan": plan}

	ads = get_asset_depr_schedule_doc(asset, "Active", None)
	frac = _ads_fractional_count(ads.name)
	due = [
		r
		for r in ads.depreciation_schedule
		if getdate(r.schedule_date) <= getdate(today()) and not r.journal_entry
	]
	after_reset_jes = _submitted_depr_jes(asset)

	# First posting
	make_depreciation_entry(ads.name, date=today())
	ads.reload()
	first_jes = _submitted_depr_jes(asset)
	linked = sum(1 for r in ads.depreciation_schedule if r.journal_entry)

	# Repeated posting
	for _ in range(max(int(posting_runs) - 1, 1)):
		make_depreciation_entry(ads.name, date=today())
	final_jes = _submitted_depr_jes(asset)

	return {
		"asset": asset,
		"plan_status": plan.get("status"),
		"reset_status": result.get("status"),
		"before_je_count": len(before_jes),
		"after_reset_je_count": len(after_reset_jes),
		"ads": ads.name,
		"rows": len(ads.depreciation_schedule),
		"fractional_after": frac,
		"due_rows": len(due),
		"first_posting_je_count": len(first_jes),
		"linked_rows": linked,
		"final_je_count": len(final_jes),
		"duplicates_after_repeats": len(final_jes) - len(first_jes),
		"first_due_amounts": [to_depr_amount(r.depreciation_amount) for r in due[:5]],
		"final_amount": to_depr_amount(ads.depreciation_schedule[-1].depreciation_amount),
		"asset_vad": flt(frappe.db.get_value("Asset", asset, "value_after_depreciation")),
		"result": result,
	}


class TestDeprResetRebuildSite(FrappeTestCase):
	"""Runs only when Asset 3760 exists on the site (development canary)."""

	def setUp(self):
		install_depreciation_idempotency_patch()

	def test_analyze_blocked_scrapped_if_any(self):
		scrapped = frappe.db.get_value("Asset", {"status": "Scrapped", "docstatus": 1}, "name")
		if not scrapped:
			self.skipTest("No scrapped asset")
		plan = analyze_asset(scrapped)
		self.assertEqual(plan["status"], "BLOCKED")

	def test_normalize_unit_available(self):
		from erpnext_extensions.asset_usage_depreciation.tests.test_ads_amount_normalize import (
			TestAdsAmountNormalize,
		)

		TestAdsAmountNormalize().test_full_schedule_negative_residual_final_whole()

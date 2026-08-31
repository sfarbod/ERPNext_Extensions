# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

"""Integration tests for Asset Usage Depreciation replan.

Uses ``unittest.TestCase`` (not FrappeTestCase) to avoid ERPNext BootStrapTestData
side-effects during discovery. Run::

    bench --site development.localhost run-tests \\
        --module erpnext_extensions.asset_usage_depreciation.tests.test_asset_usage_replan \\
        --skip-before-tests
"""

from __future__ import annotations

import unittest

import frappe
from frappe.utils import flt, getdate, random_string

from erpnext.assets.doctype.asset.depreciation import make_depreciation_entry
from erpnext.assets.doctype.asset_depreciation_schedule.asset_depreciation_schedule import (
	get_asset_depr_schedule_doc,
)

from erpnext_extensions.asset_usage_depreciation.constants import (
	COMPANY_FIELD_REDUCED_HANDLING,
	HANDLING_ADJUST_FINAL,
	HANDLING_EXTEND,
)
from erpnext_extensions.asset_usage_depreciation.custom_fields import ensure_custom_fields
from erpnext_extensions.asset_usage_depreciation.services.accounting_amounts import to_depr_amount
from erpnext_extensions.asset_usage_depreciation.services.replan_service import (
	replan_asset_usage_depreciation,
)
from erpnext_extensions.asset_usage_depreciation.tests.replan_fixtures import ensure_replan_fixtures


def _fx() -> dict:
	return ensure_replan_fixtures()


def _ensure_company_handling(handling: str):
	ensure_custom_fields()
	frappe.db.set_value("Company", _fx()["company"], COMPANY_FIELD_REDUCED_HANDLING, handling)


def _unique_asset_name(prefix: str) -> str:
	return f"{prefix}-{random_string(6)}"


def _make_sl_asset(**kwargs):
	fx = _fx()
	company = kwargs.get("company") or fx["company"]
	asset_name = kwargs.get("asset_name") or _unique_asset_name("AUD-Asset")

	finance_books = kwargs.get("finance_books")
	if finance_books is None:
		finance_books = [
			{
				"finance_book": kwargs.get("finance_book"),
				"depreciation_method": kwargs.get("depreciation_method") or "Straight Line",
				"frequency_of_depreciation": kwargs.get("frequency_of_depreciation") or 1,
				"total_number_of_depreciations": kwargs.get("total_number_of_depreciations") or 12,
				"expected_value_after_useful_life": kwargs.get("expected_value_after_useful_life") or 0,
				"depreciation_start_date": kwargs.get("depreciation_start_date") or "2026-01-31",
				"daily_prorata_based": kwargs.get("daily_prorata_based") or 0,
				"shift_based": kwargs.get("shift_based") or 0,
				"rate_of_depreciation": kwargs.get("rate_of_depreciation") or 0,
			}
		]

	payload = {
		"doctype": "Asset",
		"asset_name": asset_name,
		"asset_category": kwargs.get("asset_category") or fx["asset_category"],
		"item_code": kwargs.get("item_code") or fx["item_code"],
		"company": company,
		"purchase_date": kwargs.get("purchase_date") or "2026-01-01",
		"available_for_use_date": kwargs.get("available_for_use_date") or "2026-01-01",
		"calculate_depreciation": 1,
		"net_purchase_amount": kwargs.get("net_purchase_amount") or 120000,
		"purchase_amount": kwargs.get("purchase_amount") or 120000,
		"location": kwargs.get("location") or fx["location"],
		"asset_owner": "Company",
		"asset_type": "Existing Asset",
		"asset_quantity": 1,
		"finance_books": finance_books,
	}
	meta = frappe.get_meta("Asset")
	if meta.has_field("cost_center"):
		payload["cost_center"] = kwargs.get("cost_center") or fx["cost_center"]
	if meta.has_field("warehouse"):
		payload["warehouse"] = kwargs.get("warehouse") or fx.get("warehouse")
	asset = frappe.get_doc(payload)
	if (meta.autoname or "") == "prompt":
		asset.name = asset_name
		asset.flags.name_set = True
	asset.flags.ignore_permissions = True
	asset.insert(ignore_permissions=True)
	asset.submit()
	return asset


def _submit_usage(asset, from_date, mode, percentage=None, to_date=None, reason="test"):
	doc = frappe.get_doc(
		{
			"doctype": "Asset Usage Period",
			"asset": asset.name if hasattr(asset, "name") else asset,
			"from_date": from_date,
			"to_date": to_date,
			"depreciation_mode": mode,
			"depreciation_percentage": percentage,
			"reason": reason,
		}
	)
	doc.insert()
	doc.submit()
	return doc


def _active_schedule(asset_name, finance_book=None):
	asset = frappe.get_doc("Asset", asset_name)
	fb = finance_book
	if fb is None:
		fb = asset.finance_books[0].finance_book
	return get_asset_depr_schedule_doc(asset_name, "Active", fb)


def _unposted_amounts(ads):
	return [flt(r.depreciation_amount) for r in ads.depreciation_schedule if not r.journal_entry]


def _assert_whole_amounts(test_case, ads):
	for row in ads.depreciation_schedule:
		amount = flt(row.depreciation_amount)
		test_case.assertEqual(
			amount,
			float(int(amount)),
			msg=f"Non-whole depreciation amount {amount} on {row.schedule_date}",
		)


def _post_first_n_simulated(ads_name, n):
	"""Legacy simulated JE link helper (kept for non-JE-path coverage)."""
	from erpnext.assets.doctype.asset.depreciation import get_depreciation_accounts

	ads = frappe.get_doc("Asset Depreciation Schedule", ads_name)
	asset = frappe.get_doc("Asset", ads.asset)
	_fa, accum, expense = get_depreciation_accounts(asset.asset_category, asset.company)

	posted = 0
	for row in ads.depreciation_schedule:
		if posted >= n:
			break
		if row.journal_entry:
			posted += 1
			continue

		je = frappe.get_doc(
			{
				"doctype": "Journal Entry",
				"voucher_type": "Depreciation Entry",
				"company": asset.company,
				"posting_date": row.schedule_date,
				"finance_book": ads.finance_book,
				"accounts": [
					{
						"account": expense,
						"debit_in_account_currency": row.depreciation_amount,
						"reference_type": "Asset",
						"reference_name": asset.name,
					},
					{
						"account": accum,
						"credit_in_account_currency": row.depreciation_amount,
						"reference_type": "Asset",
						"reference_name": asset.name,
					},
				],
			}
		)
		je.flags.ignore_permissions = True
		je.flags.ignore_links = True
		je.insert()
		frappe.db.set_value("Journal Entry", je.name, "docstatus", 1)
		frappe.db.set_value("Depreciation Schedule", row.name, "journal_entry", je.name)
		fb = asset.finance_books[0]
		fb.value_after_depreciation = flt(fb.value_after_depreciation) - flt(row.depreciation_amount)
		fb.db_update()
		posted += 1

	ads.reload()


class TestAssetUsageReplanIntegration(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		ensure_custom_fields()
		ensure_replan_fixtures()
		frappe.db.commit()

	def tearDown(self):
		frappe.db.rollback()

	def test_monthly_percentage_extend_does_not_inflate_next(self):
		_ensure_company_handling(HANDLING_EXTEND)
		asset = _make_sl_asset()
		ads = _active_schedule(asset.name)
		standard = to_depr_amount(ads.depreciation_schedule[0].depreciation_amount)

		_submit_usage(asset, "2026-02-01", "Percentage", 30, to_date="2026-02-28")

		ads = _active_schedule(asset.name)
		_assert_whole_amounts(self, ads)
		self.assertEqual(to_depr_amount(ads.depreciation_schedule[0].depreciation_amount), standard)
		self.assertEqual(
			to_depr_amount(ads.depreciation_schedule[1].depreciation_amount),
			to_depr_amount(standard * 0.3),
		)
		self.assertEqual(to_depr_amount(ads.depreciation_schedule[2].depreciation_amount), standard)
		self.assertGreaterEqual(len(ads.depreciation_schedule), 12)
		# Completed schedule reaches salvage exactly
		asset.reload()
		remaining = to_depr_amount(
			flt(asset.finance_books[0].value_after_depreciation)
			- flt(asset.finance_books[0].expected_value_after_useful_life)
		)
		self.assertEqual(sum(to_depr_amount(a) for a in _unposted_amounts(ads)), remaining)

	def test_mid_month_non_daily_uses_schedule_date(self):
		_ensure_company_handling(HANDLING_EXTEND)
		asset = _make_sl_asset()
		ads = _active_schedule(asset.name)
		apr_before = next(
			r for r in ads.depreciation_schedule if getdate(r.schedule_date) == getdate("2026-04-30")
		)
		standard = to_depr_amount(apr_before.depreciation_amount)

		_submit_usage(asset, "2026-04-15", "Percentage", 30, to_date="2026-04-30")

		ads = _active_schedule(asset.name)
		apr = next(r for r in ads.depreciation_schedule if getdate(r.schedule_date) == getdate("2026-04-30"))
		self.assertEqual(to_depr_amount(apr.depreciation_amount), to_depr_amount(standard * 0.3))

	def test_mode_b_factors_rows_and_balances_final(self):
		"""Fixed-end: reduced month stays reduced; later months stay standard; final balances."""
		_ensure_company_handling(HANDLING_ADJUST_FINAL)
		asset = _make_sl_asset()
		ads_before = _active_schedule(asset.name)
		end_before = getdate(ads_before.depreciation_schedule[-1].schedule_date)
		count_before = len(ads_before.depreciation_schedule)
		standards = [to_depr_amount(r.depreciation_amount) for r in ads_before.depreciation_schedule]

		_submit_usage(asset, "2026-01-01", "Percentage", 30, to_date="2026-01-31")

		ads = _active_schedule(asset.name)
		_assert_whole_amounts(self, ads)
		self.assertEqual(len(ads.depreciation_schedule), count_before)
		self.assertEqual(getdate(ads.depreciation_schedule[-1].schedule_date), end_before)

		jan = to_depr_amount(ads.depreciation_schedule[0].depreciation_amount)
		feb = to_depr_amount(ads.depreciation_schedule[1].depreciation_amount)
		final = to_depr_amount(ads.depreciation_schedule[-1].depreciation_amount)
		self.assertEqual(jan, to_depr_amount(standards[0] * 0.3))
		self.assertEqual(feb, standards[1])
		# Final absorbs January shortfall (and any Iran rounding residue)
		self.assertGreaterEqual(final, standards[-1] + (standards[0] - jan))
		asset.reload()
		remaining = to_depr_amount(
			flt(asset.finance_books[0].value_after_depreciation)
			- flt(asset.finance_books[0].expected_value_after_useful_life)
		)
		self.assertEqual(sum(to_depr_amount(a) for a in _unposted_amounts(ads)), remaining)

	def test_mode_b_no_depreciation_absorbed_by_final(self):
		"""No Depreciation zeros applicable rows; final balancing row absorbs remaining value."""
		_ensure_company_handling(HANDLING_ADJUST_FINAL)
		asset = _make_sl_asset()
		count_before = len(_active_schedule(asset.name).depreciation_schedule)
		_submit_usage(asset, "2026-01-01", "No Depreciation", to_date=None)
		ads = _active_schedule(asset.name)
		self.assertEqual(len(ads.depreciation_schedule), count_before)
		_assert_whole_amounts(self, ads)
		# All non-final rows zero; final = full remaining
		for row in ads.depreciation_schedule[:-1]:
			self.assertEqual(to_depr_amount(row.depreciation_amount), 0)
		asset.reload()
		remaining = to_depr_amount(
			flt(asset.finance_books[0].value_after_depreciation)
			- flt(asset.finance_books[0].expected_value_after_useful_life)
		)
		self.assertEqual(to_depr_amount(ads.depreciation_schedule[-1].depreciation_amount), remaining)

	def test_posted_rows_immutable_simulated(self):
		_ensure_company_handling(HANDLING_EXTEND)
		asset = _make_sl_asset()
		ads = _active_schedule(asset.name)
		_post_first_n_simulated(ads.name, 2)
		ads = _active_schedule(asset.name)
		posted = [
			(getdate(r.schedule_date), flt(r.depreciation_amount), r.journal_entry)
			for r in ads.depreciation_schedule
			if r.journal_entry
		]
		self.assertEqual(len(posted), 2)

		_submit_usage(asset, "2026-01-01", "Percentage", 30, to_date="2026-06-30")

		ads = _active_schedule(asset.name)
		posted_after = [
			(getdate(r.schedule_date), flt(r.depreciation_amount), r.journal_entry)
			for r in ads.depreciation_schedule
			if r.journal_entry
		]
		self.assertEqual(posted, posted_after)

	def test_real_je_ads_link_and_posted_immutability(self):
		"""Real make_depreciation_entry → JE link (via getdate repair) → replan."""
		_ensure_company_handling(HANDLING_EXTEND)
		asset = _make_sl_asset()
		ads = _active_schedule(asset.name)
		first = ads.depreciation_schedule[0]
		first_date = getdate(first.schedule_date)
		first_amount = flt(first.depreciation_amount)

		# Production posting path — must not manually assign journal_entry
		make_depreciation_entry(ads.name, date=str(first_date))

		ads = _active_schedule(asset.name)
		linked = ads.depreciation_schedule[0]
		self.assertTrue(
			linked.journal_entry,
			"Depreciation JE must be linked on ADS row after make_depreciation_entry "
			"(je_link.ensure_depreciation_schedule_je_link repairs core date/str mismatch)",
		)
		je_name = linked.journal_entry
		je = frappe.get_doc("Journal Entry", je_name)
		self.assertEqual(je.docstatus, 1)
		self.assertEqual(je.voucher_type, "Depreciation Entry")
		self.assertEqual(flt(linked.depreciation_amount), first_amount)
		self.assertEqual(getdate(linked.schedule_date), first_date)

		_submit_usage(asset, "2026-01-01", "Percentage", 30, to_date="2026-06-30")

		ads = _active_schedule(asset.name)
		posted = [r for r in ads.depreciation_schedule if r.journal_entry]
		self.assertEqual(len(posted), 1)
		self.assertEqual(posted[0].journal_entry, je_name)
		self.assertEqual(flt(posted[0].depreciation_amount), first_amount)
		self.assertEqual(getdate(posted[0].schedule_date), first_date)

		je.reload()
		self.assertEqual(je.docstatus, 1)
		self.assertEqual(je.name, je_name)

		# Future unposted amounts must reflect reduced usage (regenerated base × 30%)
		feb = next(r for r in ads.depreciation_schedule if getdate(r.schedule_date) == getdate("2026-02-28"))
		jul = next(r for r in ads.depreciation_schedule if getdate(r.schedule_date) == getdate("2026-07-31"))
		self.assertFalse(feb.journal_entry)
		self.assertEqual(to_depr_amount(feb.depreciation_amount), flt(feb.depreciation_amount))
		self.assertLess(to_depr_amount(feb.depreciation_amount), to_depr_amount(jul.depreciation_amount))
		# ~30% of a Normal installment (allow Mode A / salvage residue on later rows only)
		self.assertAlmostEqual(
			to_depr_amount(feb.depreciation_amount) / to_depr_amount(jul.depreciation_amount),
			0.3,
			places=1,
		)
		_assert_whole_amounts(self, ads)

		# Automatic posting must not re-post the frozen row
		make_depreciation_entry(ads.name, date=str(first_date))
		ads = _active_schedule(asset.name)
		self.assertEqual(ads.depreciation_schedule[0].journal_entry, je_name)
		dupes = frappe.db.sql(
			"""
			SELECT COUNT(DISTINCT je.name)
			FROM `tabJournal Entry` je
			INNER JOIN `tabJournal Entry Account` jea ON jea.parent = je.name
			WHERE je.voucher_type='Depreciation Entry' AND je.docstatus=1
				AND jea.reference_name=%s AND je.posting_date=%s
			""",
			(asset.name, first_date),
		)[0][0]
		self.assertEqual(dupes, 1)

	def test_manual_blocked(self):
		_ensure_company_handling(HANDLING_EXTEND)
		asset = _make_sl_asset(depreciation_method="Manual")
		with self.assertRaises(frappe.ValidationError):
			_submit_usage(asset, "2026-02-01", "Percentage", 30, to_date="2026-02-28")

	def test_open_ended_zero_no_infinite_rows(self):
		_ensure_company_handling(HANDLING_EXTEND)
		asset = _make_sl_asset()
		_submit_usage(asset, "2026-01-01", "No Depreciation", to_date=None)
		ads = _active_schedule(asset.name)
		self.assertLess(len(ads.depreciation_schedule), 50)
		self.assertEqual(sum(1 for a in _unposted_amounts(ads) if a > 0), 0)

	def test_cancel_usage_replans(self):
		_ensure_company_handling(HANDLING_EXTEND)
		asset = _make_sl_asset()
		ads0 = _active_schedule(asset.name)
		standard = to_depr_amount(ads0.depreciation_schedule[1].depreciation_amount)
		usage = _submit_usage(asset, "2026-02-01", "Percentage", 30, to_date="2026-02-28")
		usage.cancel()
		ads = _active_schedule(asset.name)
		self.assertEqual(to_depr_amount(ads.depreciation_schedule[1].depreciation_amount), standard)

	def test_daily_prorata_mid_period_whole_amount(self):
		_ensure_company_handling(HANDLING_EXTEND)
		asset = _make_sl_asset(daily_prorata_based=1)
		ads = _active_schedule(asset.name)
		apr = next(r for r in ads.depreciation_schedule if getdate(r.schedule_date) == getdate("2026-04-30"))
		standard = flt(apr.depreciation_amount)

		_submit_usage(asset, "2026-04-11", "Percentage", 30, to_date="2026-04-30")

		ads = _active_schedule(asset.name)
		apr = next(r for r in ads.depreciation_schedule if getdate(r.schedule_date) == getdate("2026-04-30"))
		expected_factor = (10 * 1.0 + 20 * 0.3) / 30.0
		self.assertEqual(to_depr_amount(apr.depreciation_amount), to_depr_amount(standard * expected_factor))
		_assert_whole_amounts(self, ads)

	def test_regression_no_usage_period_replan_noop(self):
		"""Req 11: zero Usage Periods → standard ERPNext ADS, no unnecessary replace."""
		_ensure_company_handling(HANDLING_EXTEND)
		asset = _make_sl_asset()
		ads_before = _active_schedule(asset.name)
		before_name = ads_before.name
		before_amounts = [(getdate(r.schedule_date), flt(r.depreciation_amount)) for r in ads_before.depreciation_schedule]

		replan_asset_usage_depreciation(asset.name)

		ads_after = _active_schedule(asset.name)
		self.assertEqual(ads_after.name, before_name)
		after_amounts = [(getdate(r.schedule_date), flt(r.depreciation_amount)) for r in ads_after.depreciation_schedule]
		self.assertEqual(before_amounts, after_amounts)
		self.assertFalse(
			frappe.db.exists("Asset Usage Replan Log", {"new_ads": before_name})
			or frappe.db.exists("Asset Usage Replan Log", {"old_ads": before_name})
		)

	def test_non_daily_before_first_usage_period_unchanged(self):
		"""Req 10: schedule_date before first Usage Period keeps standard ERPNext amount."""
		_ensure_company_handling(HANDLING_EXTEND)
		asset = _make_sl_asset()
		ads = _active_schedule(asset.name)
		jan = next(r for r in ads.depreciation_schedule if getdate(r.schedule_date) == getdate("2026-01-31"))
		feb = next(r for r in ads.depreciation_schedule if getdate(r.schedule_date) == getdate("2026-02-28"))
		mar = next(r for r in ads.depreciation_schedule if getdate(r.schedule_date) == getdate("2026-03-31"))
		jan_std = to_depr_amount(jan.depreciation_amount)
		feb_std = to_depr_amount(feb.depreciation_amount)
		mar_std = to_depr_amount(mar.depreciation_amount)

		# First explicit period starts in April (open 30%) — Jan–Mar stay implicit Normal
		_submit_usage(asset, "2026-04-01", "Percentage", 30, to_date=None)

		ads = _active_schedule(asset.name)
		jan = next(r for r in ads.depreciation_schedule if getdate(r.schedule_date) == getdate("2026-01-31"))
		feb = next(r for r in ads.depreciation_schedule if getdate(r.schedule_date) == getdate("2026-02-28"))
		mar = next(r for r in ads.depreciation_schedule if getdate(r.schedule_date) == getdate("2026-03-31"))
		apr = next(r for r in ads.depreciation_schedule if getdate(r.schedule_date) == getdate("2026-04-30"))
		self.assertEqual(to_depr_amount(jan.depreciation_amount), jan_std)
		self.assertEqual(to_depr_amount(feb.depreciation_amount), feb_std)
		self.assertEqual(to_depr_amount(mar.depreciation_amount), mar_std)
		self.assertEqual(to_depr_amount(apr.depreciation_amount), to_depr_amount(jan_std * 0.3))

	def test_multi_ads_unsupported_atomic_abort(self):
		"""SL + WDV: fail before any ADS is replaced."""
		_ensure_company_handling(HANDLING_EXTEND)
		if not frappe.db.exists("Finance Book", "AUD-WDV-FB"):
			frappe.get_doc({"doctype": "Finance Book", "finance_book_name": "AUD-WDV-FB"}).insert()

		asset = _make_sl_asset(
			finance_books=[
				{
					"finance_book": "default",
					"depreciation_method": "Straight Line",
					"frequency_of_depreciation": 1,
					"total_number_of_depreciations": 12,
					"expected_value_after_useful_life": 0,
					"depreciation_start_date": "2026-01-31",
					"daily_prorata_based": 0,
					"shift_based": 0,
					"rate_of_depreciation": 0,
				},
				{
					"finance_book": "AUD-WDV-FB",
					"depreciation_method": "Written Down Value",
					"frequency_of_depreciation": 1,
					"total_number_of_depreciations": 12,
					"expected_value_after_useful_life": 0,
					"depreciation_start_date": "2026-01-31",
					"daily_prorata_based": 0,
					"shift_based": 0,
					"rate_of_depreciation": 10,
				},
			]
		)
		sl_ads = _active_schedule(asset.name, finance_book="default")
		wdv_ads = _active_schedule(asset.name, finance_book="AUD-WDV-FB")
		self.assertTrue(sl_ads)
		self.assertTrue(wdv_ads)
		sl_name = sl_ads.name
		wdv_name = wdv_ads.name
		sl_amounts = [flt(r.depreciation_amount) for r in sl_ads.depreciation_schedule]

		with self.assertRaises(frappe.ValidationError):
			_submit_usage(asset, "2026-02-01", "Percentage", 30, to_date="2026-02-28")

		# Transaction rolled back in tearDown; within the failed submit the DB may
		# already be rolled back by frappe.throw. Re-check Active schedules still exist.
		self.assertTrue(frappe.db.exists("Asset Depreciation Schedule", {"name": sl_name, "docstatus": 1}))
		self.assertTrue(frappe.db.exists("Asset Depreciation Schedule", {"name": wdv_name, "docstatus": 1}))
		sl_ads = frappe.get_doc("Asset Depreciation Schedule", sl_name)
		self.assertEqual(sl_ads.status, "Active")
		self.assertEqual([flt(r.depreciation_amount) for r in sl_ads.depreciation_schedule], sl_amounts)

	def test_replan_log_whole_amounts(self):
		_ensure_company_handling(HANDLING_EXTEND)
		asset = _make_sl_asset()
		usage = _submit_usage(asset, "2026-02-01", "Percentage", 30, to_date="2026-02-28")
		logs = frappe.get_all(
			"Asset Usage Replan Log",
			filters={"parent": usage.name},
			fields=["old_amount", "new_amount", "old_ads", "new_ads", "schedule_date"],
		)
		self.assertTrue(logs)
		for row in logs:
			if row.old_amount is not None:
				self.assertEqual(flt(row.old_amount), float(to_depr_amount(row.old_amount)))
			self.assertEqual(flt(row.new_amount), float(to_depr_amount(row.new_amount)))
			self.assertTrue(row.old_ads)
			self.assertTrue(frappe.db.exists("Asset Depreciation Schedule", row.new_ads))


class TestRepeatedUsageTransitions(unittest.TestCase):
	"""Auto-close open periods via amend for repeated status changes."""

	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		ensure_custom_fields()
		ensure_replan_fixtures()
		frappe.db.commit()

	def tearDown(self):
		frappe.db.rollback()

	def _submitted_timeline(self, asset_name):
		from erpnext_extensions.asset_usage_depreciation.services.usage_timeline import (
			factor_on_date,
			load_submitted_usage_periods,
			validate_timeline_consistency,
		)

		rows = load_submitted_usage_periods(asset_name)
		validate_timeline_consistency(rows)
		return rows, factor_on_date

	def test_five_open_transitions_auto_close_chain(self):
		"""30 → Normal → 30 → Normal → 30 with auto-closed history."""
		_ensure_company_handling(HANDLING_EXTEND)
		asset = _make_sl_asset(total_number_of_depreciations=24)

		p1 = _submit_usage(asset, "2026-01-01", "Percentage", 30, to_date=None)
		p2 = _submit_usage(asset, "2026-04-01", "Normal", to_date=None)
		p3 = _submit_usage(asset, "2026-09-01", "Percentage", 30, to_date=None)
		p4 = _submit_usage(asset, "2027-01-01", "Normal", to_date=None)
		p5 = _submit_usage(asset, "2027-06-01", "Percentage", 30, to_date=None)

		rows, factor_on_date = self._submitted_timeline(asset.name)
		self.assertEqual(len(rows), 5)
		opens = [r for r in rows if not r["to_date"]]
		self.assertEqual(len(opens), 1)
		self.assertEqual(opens[0]["from_date"], getdate("2027-06-01"))
		self.assertEqual(opens[0]["factor"], 0.3)

		expected = [
			(getdate("2026-01-01"), getdate("2026-03-31"), 0.3),
			(getdate("2026-04-01"), getdate("2026-08-31"), 1.0),
			(getdate("2026-09-01"), getdate("2026-12-31"), 0.3),
			(getdate("2027-01-01"), getdate("2027-05-31"), 1.0),
			(getdate("2027-06-01"), None, 0.3),
		]
		for row, (fr, to, fac) in zip(rows, expected, strict=True):
			self.assertEqual(row["from_date"], fr)
			self.assertEqual(row["to_date"], to)
			self.assertEqual(row["factor"], fac)

		self.assertEqual(factor_on_date(rows, "2025-12-01"), 1.0)  # before first → Normal
		self.assertEqual(factor_on_date(rows, "2026-02-15"), 0.3)
		self.assertEqual(factor_on_date(rows, "2026-06-15"), 1.0)
		self.assertEqual(factor_on_date(rows, "2026-10-15"), 0.3)
		self.assertEqual(factor_on_date(rows, "2027-03-15"), 1.0)
		self.assertEqual(factor_on_date(rows, "2027-07-15"), 0.3)

		# Amendment chain: original open cancelled; closed successor has amended_from
		self.assertEqual(frappe.db.get_value("Asset Usage Period", p1.name, "docstatus"), 2)
		closed1 = frappe.db.get_value(
			"Asset Usage Period", {"amended_from": p1.name, "docstatus": 1}, "name"
		)
		self.assertTrue(closed1)
		self.assertEqual(
			getdate(frappe.db.get_value("Asset Usage Period", closed1, "to_date")),
			getdate("2026-03-31"),
		)
		self.assertEqual(frappe.db.get_value("Asset Usage Period", p2.name, "docstatus"), 2)
		self.assertEqual(frappe.db.get_value("Asset Usage Period", p5.name, "docstatus"), 1)
		self.assertFalse(frappe.db.get_value("Asset Usage Period", p5.name, "to_date"))

	def test_one_replan_per_transition(self):
		_ensure_company_handling(HANDLING_EXTEND)
		asset = _make_sl_asset()
		_submit_usage(asset, "2026-01-01", "Percentage", 30, to_date=None)

		import erpnext_extensions.asset_usage_depreciation.doctype.asset_usage_period.asset_usage_period as aup_mod
		import erpnext_extensions.asset_usage_depreciation.services.replan_service as replan_mod

		calls = {"n": 0}
		orig = replan_mod.replan_asset_usage_depreciation

		def counting(*args, **kwargs):
			calls["n"] += 1
			return orig(*args, **kwargs)

		replan_mod.replan_asset_usage_depreciation = counting
		aup_mod.replan_asset_usage_depreciation = counting
		try:
			_submit_usage(asset, "2026-04-01", "Normal", to_date=None)
			self.assertEqual(calls["n"], 1, "auto-close must not replan; only final submit replans once")
		finally:
			replan_mod.replan_asset_usage_depreciation = orig
			aup_mod.replan_asset_usage_depreciation = orig

	def test_new_from_date_on_or_before_open_fails(self):
		_ensure_company_handling(HANDLING_EXTEND)
		asset = _make_sl_asset()
		_submit_usage(asset, "2026-04-01", "Percentage", 30, to_date=None)
		with self.assertRaises(frappe.ValidationError):
			_submit_usage(asset, "2026-04-01", "Normal", to_date=None)
		with self.assertRaises(frappe.ValidationError):
			_submit_usage(asset, "2026-03-01", "Normal", to_date=None)
		# Open period unchanged
		rows, _ = self._submitted_timeline(asset.name)
		self.assertEqual(len(rows), 1)
		self.assertIsNone(rows[0]["to_date"])

	def test_no_depreciation_normal_cycle(self):
		_ensure_company_handling(HANDLING_EXTEND)
		asset = _make_sl_asset()
		_submit_usage(asset, "2026-01-01", "No Depreciation", to_date=None)
		_submit_usage(asset, "2026-05-01", "Normal", to_date=None)
		_submit_usage(asset, "2026-09-01", "No Depreciation", to_date=None)
		rows, factor_on_date = self._submitted_timeline(asset.name)
		self.assertEqual(len(rows), 3)
		self.assertEqual(factor_on_date(rows, "2026-02-01"), 0.0)
		self.assertEqual(factor_on_date(rows, "2026-06-01"), 1.0)
		self.assertEqual(factor_on_date(rows, "2026-10-01"), 0.0)

	def test_arbitrary_percentage_transition(self):
		_ensure_company_handling(HANDLING_EXTEND)
		asset = _make_sl_asset()
		_submit_usage(asset, "2026-01-01", "Percentage", 45, to_date=None)
		_submit_usage(asset, "2026-06-01", "Percentage", 10, to_date=None)
		rows, factor_on_date = self._submitted_timeline(asset.name)
		self.assertEqual(factor_on_date(rows, "2026-03-01"), 0.45)
		self.assertEqual(factor_on_date(rows, "2026-07-01"), 0.1)
		self.assertEqual(rows[0]["to_date"], getdate("2026-05-31"))

	def test_rollback_when_final_replan_fails(self):
		_ensure_company_handling(HANDLING_EXTEND)
		asset = _make_sl_asset()
		first = _submit_usage(asset, "2026-01-01", "Percentage", 30, to_date=None)
		frappe.db.savepoint("after_first_usage")

		import erpnext_extensions.asset_usage_depreciation.doctype.asset_usage_period.asset_usage_period as aup_mod
		import erpnext_extensions.asset_usage_depreciation.services.replan_service as replan_mod

		orig = replan_mod.replan_asset_usage_depreciation

		def boom(*args, **kwargs):
			frappe.throw("forced replan failure")

		replan_mod.replan_asset_usage_depreciation = boom
		aup_mod.replan_asset_usage_depreciation = boom
		try:
			with self.assertRaises(frappe.ValidationError):
				_submit_usage(asset, "2026-04-01", "Normal", to_date=None)
		finally:
			replan_mod.replan_asset_usage_depreciation = orig
			aup_mod.replan_asset_usage_depreciation = orig

		frappe.db.rollback(save_point="after_first_usage")
		self.assertEqual(frappe.db.get_value("Asset Usage Period", first.name, "docstatus"), 1)
		self.assertFalse(frappe.db.get_value("Asset Usage Period", first.name, "to_date"))
		rows, _ = self._submitted_timeline(asset.name)
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["name"], first.name)

	def test_posted_jes_survive_multiple_transitions(self):
		_ensure_company_handling(HANDLING_EXTEND)
		asset = _make_sl_asset()
		_submit_usage(asset, "2026-01-01", "Percentage", 30, to_date="2026-06-30")
		ads = _active_schedule(asset.name)
		# Post first two months via production path
		d0 = getdate(ads.depreciation_schedule[0].schedule_date)
		make_depreciation_entry(ads.name, date=str(d0))
		ads = _active_schedule(asset.name)
		d1 = getdate(ads.depreciation_schedule[1].schedule_date)
		make_depreciation_entry(ads.name, date=str(d1))
		ads = _active_schedule(asset.name)
		posted_before = [
			(getdate(r.schedule_date), flt(r.depreciation_amount), r.journal_entry)
			for r in ads.depreciation_schedule
			if r.journal_entry
		]
		self.assertEqual(len(posted_before), 2)
		for _d, amt, je in posted_before:
			self.assertEqual(amt, float(int(amt)))
			self.assertEqual(frappe.db.get_value("Journal Entry", je, "docstatus"), 1)

		_submit_usage(asset, "2026-07-01", "Normal", to_date=None)
		_submit_usage(asset, "2026-10-01", "Percentage", 30, to_date=None)

		ads = _active_schedule(asset.name)
		posted_after = [
			(getdate(r.schedule_date), flt(r.depreciation_amount), r.journal_entry)
			for r in ads.depreciation_schedule
			if r.journal_entry
		]
		self.assertEqual(posted_before, posted_after)
		_assert_whole_amounts(self, ads)

	def test_manual_closed_periods_still_work_with_later_open(self):
		"""Existing manually closed periods + later open transitions."""
		_ensure_company_handling(HANDLING_EXTEND)
		asset = _make_sl_asset()
		_submit_usage(asset, "2026-01-01", "Percentage", 30, to_date="2026-03-31")
		_submit_usage(asset, "2026-04-01", "Normal", to_date="2026-06-30")
		_submit_usage(asset, "2026-07-01", "Percentage", 30, to_date=None)
		_submit_usage(asset, "2026-10-01", "Normal", to_date=None)
		rows, factor_on_date = self._submitted_timeline(asset.name)
		self.assertEqual(len(rows), 4)
		self.assertEqual(factor_on_date(rows, "2026-08-01"), 0.3)
		self.assertEqual(factor_on_date(rows, "2026-11-01"), 1.0)
		# Gap none between first two; after closed Apr–Jun before Jul open was continuous
		self.assertEqual(rows[-1]["to_date"], None)


class TestModeBFixedEndIntegration(unittest.TestCase):
	"""Adjust Final Depreciation Installment — multi-transition + 120-row scenarios."""

	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		ensure_custom_fields()
		ensure_replan_fixtures()
		frappe.db.commit()

	def tearDown(self):
		frappe.db.rollback()

	def _make_long_asset(self, periods=120, amount=1_200_000_000, daily=0):
		return _make_sl_asset(
			total_number_of_depreciations=periods,
			net_purchase_amount=amount,
			purchase_amount=amount,
			daily_prorata_based=daily,
			expected_value_after_useful_life=0,
		)

	def _remaining(self, asset):
		asset.reload()
		return to_depr_amount(
			flt(asset.finance_books[0].value_after_depreciation)
			- flt(asset.finance_books[0].expected_value_after_useful_life)
		)

	def test_120_rows_30pct_from_installment_3_then_normal_then_30_again(self):
		_ensure_company_handling(HANDLING_ADJUST_FINAL)
		asset = self._make_long_asset()
		ads0 = _active_schedule(asset.name)
		self.assertEqual(len(ads0.depreciation_schedule), 120)
		end0 = getdate(ads0.depreciation_schedule[-1].schedule_date)
		# Capture original ERPNext standards before any usage replan
		_submit_usage(asset, "2026-03-01", "Percentage", 30, to_date=None)
		ads = _active_schedule(asset.name)
		self.assertEqual(len(ads.depreciation_schedule), 120)
		self.assertEqual(getdate(ads.depreciation_schedule[-1].schedule_date), end0)
		_assert_whole_amounts(self, ads)

		amts = [to_depr_amount(r.depreciation_amount) for r in ads.depreciation_schedule]
		stds = [to_depr_amount(r.depreciation_amount) for r in ads0.depreciation_schedule]
		self.assertEqual(amts[0], stds[0])
		self.assertEqual(amts[1], stds[1])
		for i in range(2, 119):
			self.assertEqual(amts[i], to_depr_amount(stds[i] * 0.3), f"installment {i+1}")
		shortfall = sum(stds[i] - amts[i] for i in range(2, 119))
		self.assertEqual(amts[119], stds[119] + shortfall)
		self.assertEqual(sum(amts), self._remaining(asset))
		final_after_30 = amts[119]

		# Return to Normal at installment 10 (Oct 2026)
		_submit_usage(asset, "2026-10-01", "Normal", to_date=None)
		ads = _active_schedule(asset.name)
		amts = [to_depr_amount(r.depreciation_amount) for r in ads.depreciation_schedule]
		self.assertEqual(len(amts), 120)
		self.assertEqual(getdate(ads.depreciation_schedule[-1].schedule_date), end0)
		for i in range(2, 9):
			self.assertEqual(amts[i], to_depr_amount(stds[i] * 0.3), f"installment {i+1} stays 30%")
		for i in range(9, 119):
			self.assertEqual(amts[i], stds[i], f"installment {i+1} restored")
		self.assertLess(amts[119], final_after_30)
		shortfall_7 = sum(stds[i] - amts[i] for i in range(2, 9))
		self.assertEqual(amts[119], stds[119] + shortfall_7)
		self.assertEqual(sum(amts), self._remaining(asset))
		final_after_normal = amts[119]

		# 30% again from installment 30 (row index 29)
		from_30 = getdate(ads.depreciation_schedule[29].schedule_date)
		_submit_usage(asset, str(from_30), "Percentage", 30, to_date=None)
		ads = _active_schedule(asset.name)
		amts = [to_depr_amount(r.depreciation_amount) for r in ads.depreciation_schedule]
		self.assertEqual(len(amts), 120)
		self.assertEqual(getdate(ads.depreciation_schedule[-1].schedule_date), end0)
		for i in range(29, 119):
			self.assertEqual(amts[i], to_depr_amount(stds[i] * 0.3), f"installment {i+1} reduced again")
		self.assertGreater(amts[119], final_after_normal)
		self.assertEqual(sum(amts), self._remaining(asset))

	def test_mode_b_no_depr_then_normal_final_moves(self):
		_ensure_company_handling(HANDLING_ADJUST_FINAL)
		asset = _make_sl_asset(total_number_of_depreciations=12)
		ads0 = _active_schedule(asset.name)
		end0 = getdate(ads0.depreciation_schedule[-1].schedule_date)
		count0 = len(ads0.depreciation_schedule)

		_submit_usage(asset, "2026-01-01", "No Depreciation", to_date=None)
		ads = _active_schedule(asset.name)
		final_zero_period = to_depr_amount(ads.depreciation_schedule[-1].depreciation_amount)
		self.assertEqual(final_zero_period, self._remaining(asset))

		_submit_usage(asset, "2026-05-01", "Normal", to_date=None)
		ads = _active_schedule(asset.name)
		self.assertEqual(len(ads.depreciation_schedule), count0)
		self.assertEqual(getdate(ads.depreciation_schedule[-1].schedule_date), end0)
		final_after = to_depr_amount(ads.depreciation_schedule[-1].depreciation_amount)
		self.assertLess(final_after, final_zero_period)
		# Jan–Apr still zero (closed No Depreciation through Apr 30)
		for row in ads.depreciation_schedule:
			if getdate(row.schedule_date) <= getdate("2026-04-30"):
				self.assertEqual(to_depr_amount(row.depreciation_amount), 0)
		self.assertEqual(sum(to_depr_amount(a) for a in _unposted_amounts(ads)), self._remaining(asset))

	def test_mode_b_posted_excluded_from_retroactive_shortfall(self):
		_ensure_company_handling(HANDLING_ADJUST_FINAL)
		asset = _make_sl_asset()
		ads = _active_schedule(asset.name)
		d0 = getdate(ads.depreciation_schedule[0].schedule_date)
		make_depreciation_entry(ads.name, date=str(d0))
		ads = _active_schedule(asset.name)
		posted = [
			(getdate(r.schedule_date), flt(r.depreciation_amount), r.journal_entry)
			for r in ads.depreciation_schedule
			if r.journal_entry
		]
		self.assertEqual(len(posted), 1)
		std_jan = posted[0][1]

		# Retroactive 30% covering January — posted Jan unchanged
		_submit_usage(asset, "2026-01-01", "Percentage", 30, to_date=None)
		ads = _active_schedule(asset.name)
		posted_after = [
			(getdate(r.schedule_date), flt(r.depreciation_amount), r.journal_entry)
			for r in ads.depreciation_schedule
			if r.journal_entry
		]
		self.assertEqual(posted, posted_after)
		self.assertEqual(posted_after[0][1], std_jan)
		_assert_whole_amounts(self, ads)
		self.assertEqual(sum(to_depr_amount(a) for a in _unposted_amounts(ads)), self._remaining(asset))

	def test_mode_b_daily_prorata_balances_final(self):
		_ensure_company_handling(HANDLING_ADJUST_FINAL)
		asset = self._make_long_asset(periods=12, amount=120_000, daily=1)
		ads0 = _active_schedule(asset.name)
		count0 = len(ads0.depreciation_schedule)
		end0 = getdate(ads0.depreciation_schedule[-1].schedule_date)

		_submit_usage(asset, "2026-03-01", "Percentage", 30, to_date=None)
		ads = _active_schedule(asset.name)
		self.assertEqual(len(ads.depreciation_schedule), count0)
		self.assertEqual(getdate(ads.depreciation_schedule[-1].schedule_date), end0)
		_assert_whole_amounts(self, ads)
		self.assertEqual(sum(to_depr_amount(a) for a in _unposted_amounts(ads)), self._remaining(asset))

	def test_legacy_company_option_maps_to_adjust_final(self):
		from erpnext_extensions.asset_usage_depreciation.constants import HANDLING_REDISTRIBUTE_LEGACY
		from erpnext_extensions.asset_usage_depreciation.services.replan_service import (
			get_reduced_depreciation_handling,
		)

		ensure_custom_fields()
		company = _fx()["company"]
		# Temporarily store legacy label (may not be in Select options after migrate)
		frappe.db.set_value(
			"Company", company, COMPANY_FIELD_REDUCED_HANDLING, HANDLING_REDISTRIBUTE_LEGACY
		)
		self.assertEqual(get_reduced_depreciation_handling(company), HANDLING_ADJUST_FINAL)

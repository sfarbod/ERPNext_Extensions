# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

"""v5.7.46 — Asset Scrap must not emit zero Debit/Credit JE rows (IRR residual).

After disposal-day depreciation, Iran whole-IRR ADS normalization can leave a
sub-rial Asset VAD residual (e.g. 0.44). Core scrap JE generation then builds a
disposal P&L row for that residual; Journal Entry currency rounding (IRR
precision 0) collapses the row to zero Debit and Credit and throws::

    Row N: Both Debit and Credit values cannot be zero

These tests cover residual cleanup + scrap JE shape for:

1. Asset with accumulated depreciation (partial book value)
2. Zero remaining book value
3. Partially depreciated value with sub-precision residual (Asset 2061 shape)
4. No zero-value Debit/Credit rows
5. Balanced Journal Entry after scrap
6. Valid scrap scenarios unchanged

Run::

    bench --site development.localhost run-tests \\
        --module erpnext_extensions.asset_usage_depreciation.tests.test_scrap_sub_precision_vad_v5746 \\
        --skip-before-tests
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import frappe
from frappe.utils import flt

from erpnext.assets.doctype.asset.depreciation import get_gl_entries_on_asset_disposal

from erpnext_extensions.asset_usage_depreciation.services.scrap_restore_vad import (
	_scrap_vad_residual_threshold,
	clear_sub_precision_scrap_vad_residual,
)

ASSET_2061 = "2061"


def _je_row_amounts(entry: dict) -> tuple[float, float]:
	return flt(entry.get("debit") or 0), flt(entry.get("credit") or 0)


def _assert_no_zero_rows(test: unittest.TestCase, entries: list[dict]):
	for idx, entry in enumerate(entries, start=1):
		debit, credit = _je_row_amounts(entry)
		test.assertFalse(
			abs(debit) < 1e-9 and abs(credit) < 1e-9,
			f"Row {idx} has both Debit and Credit zero: {entry}",
		)


def _assert_balanced(test: unittest.TestCase, entries: list[dict]):
	total_debit = sum(_je_row_amounts(e)[0] for e in entries)
	total_credit = sum(_je_row_amounts(e)[1] for e in entries)
	test.assertAlmostEqual(total_debit, total_credit, places=6)


class TestScrapVadResidualThreshold(unittest.TestCase):
	def test_irr_threshold_is_half_rial(self):
		company = frappe.db.get_value("Company", {"default_currency": "IRR"}, "name")
		if not company:
			self.skipTest("No IRR company on site")
		self.assertEqual(_scrap_vad_residual_threshold(company), 0.5)


class TestClearSubPrecisionScrapVadResidual(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		cls.has_2061 = bool(frappe.db.exists("Asset", ASSET_2061))

	def test_clears_sub_threshold_vad_on_asset_and_finance_book(self):
		if not self.has_2061:
			self.skipTest(f"Asset {ASSET_2061} not on site")

		asset = frappe.get_doc("Asset", ASSET_2061)
		if asset.status == "Scrapped":
			self.skipTest(f"Asset {ASSET_2061} is Scrapped; restore first")

		threshold = _scrap_vad_residual_threshold(asset.company)
		residual = threshold * 0.8  # e.g. 0.4 for IRR
		fb_name = asset.finance_books[0].name
		prev_asset_vad = flt(asset.value_after_depreciation)
		prev_fb_vad = flt(
			frappe.db.get_value("Asset Finance Book", fb_name, "value_after_depreciation")
		)

		try:
			frappe.db.set_value(
				"Asset", ASSET_2061, "value_after_depreciation", residual, update_modified=False
			)
			frappe.db.set_value(
				"Asset Finance Book",
				fb_name,
				"value_after_depreciation",
				residual,
				update_modified=False,
			)

			report = clear_sub_precision_scrap_vad_residual(ASSET_2061)
			self.assertTrue(report["cleared"])
			self.assertAlmostEqual(report["vad_before"], residual, places=6)
			self.assertEqual(flt(frappe.db.get_value("Asset", ASSET_2061, "value_after_depreciation")), 0)
			self.assertEqual(
				flt(frappe.db.get_value("Asset Finance Book", fb_name, "value_after_depreciation")),
				0,
			)
			self.assertTrue(report["finance_books_cleared"])
		finally:
			frappe.db.set_value(
				"Asset",
				ASSET_2061,
				"value_after_depreciation",
				prev_asset_vad,
				update_modified=False,
			)
			frappe.db.set_value(
				"Asset Finance Book",
				fb_name,
				"value_after_depreciation",
				prev_fb_vad,
				update_modified=False,
			)
			frappe.db.rollback()

	def test_does_not_clear_meaningful_vad(self):
		if not self.has_2061:
			self.skipTest(f"Asset {ASSET_2061} not on site")

		asset = frappe.get_doc("Asset", ASSET_2061)
		if asset.status == "Scrapped":
			self.skipTest(f"Asset {ASSET_2061} is Scrapped; restore first")

		threshold = _scrap_vad_residual_threshold(asset.company)
		meaningful = threshold + 1000.0
		prev = flt(asset.value_after_depreciation)

		try:
			frappe.db.set_value(
				"Asset", ASSET_2061, "value_after_depreciation", meaningful, update_modified=False
			)
			report = clear_sub_precision_scrap_vad_residual(ASSET_2061)
			self.assertFalse(report["cleared"])
			self.assertEqual(report["reason"], "above_threshold")
			self.assertAlmostEqual(
				flt(frappe.db.get_value("Asset", ASSET_2061, "value_after_depreciation")),
				meaningful,
				places=2,
			)
		finally:
			frappe.db.set_value(
				"Asset", ASSET_2061, "value_after_depreciation", prev, update_modified=False
			)
			frappe.db.rollback()

	def test_already_zero_is_noop(self):
		if not self.has_2061:
			self.skipTest(f"Asset {ASSET_2061} not on site")

		asset = frappe.get_doc("Asset", ASSET_2061)
		if asset.status == "Scrapped":
			self.skipTest(f"Asset {ASSET_2061} is Scrapped; restore first")

		prev = flt(asset.value_after_depreciation)
		try:
			frappe.db.set_value(
				"Asset", ASSET_2061, "value_after_depreciation", 0, update_modified=False
			)
			report = clear_sub_precision_scrap_vad_residual(ASSET_2061)
			self.assertFalse(report["cleared"])
			self.assertEqual(report["reason"], "already_zero")
		finally:
			frappe.db.set_value(
				"Asset", ASSET_2061, "value_after_depreciation", prev, update_modified=False
			)
			frappe.db.rollback()


class TestScrapGlEntriesNoZeroRows(unittest.TestCase):
	"""Unit-level GL shape for scrap disposal with residual scenarios."""

	def _fake_asset(
		self,
		*,
		net_purchase: float,
		vad: float,
		company: str,
		fixed_asset_account: str,
		accum_account: str,
		disposal_account: str,
		cost_center: str,
	):
		asset = MagicMock()
		asset.name = "TEST-SCRAP-RESIDUAL"
		asset.company = company
		asset.net_purchase_amount = net_purchase
		asset.value_after_depreciation = vad
		asset.cost_center = cost_center
		asset.asset_category = "TEST-CAT"
		asset.doctype = "Asset"

		def get_gl_dict(args, item=None):
			row = dict(args)
			row.setdefault("debit", 0)
			row.setdefault("credit", 0)
			row.setdefault("debit_in_account_currency", row.get("debit", 0))
			row.setdefault("credit_in_account_currency", row.get("credit", 0))
			return row

		asset.get_gl_dict = get_gl_dict
		return asset

	def test_partial_depreciation_scrap_entries_balanced_no_zero(self):
		"""Case 1/3: partially depreciated asset — Cr FA, Dr Accum, Dr disposal loss."""
		company = frappe.db.get_value("Company", {"default_currency": "IRR"}, "name")
		if not company:
			self.skipTest("No IRR company")

		disposal_account, cost_center = frappe.get_cached_value(
			"Company", company, ["disposal_account", "depreciation_cost_center"]
		)
		if not disposal_account or not cost_center:
			self.skipTest("Company missing disposal account / cost center")

		net_purchase = 2_500_000.0
		vad = 500_000.0
		asset = self._fake_asset(
			net_purchase=net_purchase,
			vad=vad,
			company=company,
			fixed_asset_account="FA",
			accum_account="ACCUM",
			disposal_account=disposal_account,
			cost_center=cost_center,
		)
		disposal_values = {
			"value_after_depreciation": vad,
			"gross_asset_value": net_purchase,
		}

		with (
			patch(
				"erpnext.assets.doctype.asset.depreciation.get_depreciation_accounts",
				return_value=("FA", "ACCUM", "EXP"),
			),
			patch(
				"erpnext.assets.doctype.asset.depreciation.get_disposal_account_and_cost_center",
				return_value=(disposal_account, cost_center),
			),
		):
			entries = get_gl_entries_on_asset_disposal(
				asset, selling_amount=0, date="2026-11-15", disposal_values=disposal_values
			)

		_assert_no_zero_rows(self, entries)
		_assert_balanced(self, entries)
		self.assertEqual(len(entries), 3)
		self.assertAlmostEqual(flt(entries[0].get("credit")), net_purchase, places=2)
		self.assertAlmostEqual(flt(entries[1].get("debit")), net_purchase - vad, places=2)
		self.assertAlmostEqual(flt(entries[2].get("debit")), vad, places=2)

	def test_zero_book_value_scrap_two_line_balanced(self):
		"""Case 2: fully written off — Cr FA / Dr Accum only, no disposal P&L row."""
		company = frappe.db.get_value("Company", {"default_currency": "IRR"}, "name")
		if not company:
			self.skipTest("No IRR company")

		disposal_account, cost_center = frappe.get_cached_value(
			"Company", company, ["disposal_account", "depreciation_cost_center"]
		)
		net_purchase = 2_500_000.0
		asset = self._fake_asset(
			net_purchase=net_purchase,
			vad=0,
			company=company,
			fixed_asset_account="FA",
			accum_account="ACCUM",
			disposal_account=disposal_account or "DISP",
			cost_center=cost_center or "CC",
		)
		disposal_values = {
			"value_after_depreciation": 0,
			"gross_asset_value": net_purchase,
		}

		with (
			patch(
				"erpnext.assets.doctype.asset.depreciation.get_depreciation_accounts",
				return_value=("FA", "ACCUM", "EXP"),
			),
			patch(
				"erpnext.assets.doctype.asset.depreciation.get_disposal_account_and_cost_center",
				return_value=(disposal_account or "DISP", cost_center or "CC"),
			),
		):
			entries = get_gl_entries_on_asset_disposal(
				asset, selling_amount=0, date="2026-11-15", disposal_values=disposal_values
			)

		_assert_no_zero_rows(self, entries)
		_assert_balanced(self, entries)
		self.assertEqual(len(entries), 2)
		self.assertAlmostEqual(flt(entries[0].get("credit")), net_purchase, places=2)
		self.assertAlmostEqual(flt(entries[1].get("debit")), net_purchase, places=2)

	def test_sub_precision_residual_without_clear_would_create_tiny_pnl_row(self):
		"""Documents the 2061 failure shape before residual cleanup."""
		company = frappe.db.get_value("Company", {"default_currency": "IRR"}, "name")
		if not company:
			self.skipTest("No IRR company")

		disposal_account, cost_center = frappe.get_cached_value(
			"Company", company, ["disposal_account", "depreciation_cost_center"]
		)
		net_purchase = 2_500_000.0
		vad = 0.44  # post-depreciate residual that rounds away on JE
		asset = self._fake_asset(
			net_purchase=net_purchase,
			vad=vad,
			company=company,
			fixed_asset_account="FA",
			accum_account="ACCUM",
			disposal_account=disposal_account or "DISP",
			cost_center=cost_center or "CC",
		)
		disposal_values = {
			"value_after_depreciation": vad,
			"gross_asset_value": net_purchase,
		}

		with (
			patch(
				"erpnext.assets.doctype.asset.depreciation.get_depreciation_accounts",
				return_value=("FA", "ACCUM", "EXP"),
			),
			patch(
				"erpnext.assets.doctype.asset.depreciation.get_disposal_account_and_cost_center",
				return_value=(disposal_account or "DISP", cost_center or "CC"),
			),
		):
			entries = get_gl_entries_on_asset_disposal(
				asset, selling_amount=0, date="2026-11-15", disposal_values=disposal_values
			)

		self.assertEqual(len(entries), 3)
		debit3, credit3 = _je_row_amounts(entries[2])
		self.assertAlmostEqual(debit3, 0.44, places=6)
		# After currency precision 0 rounding this becomes the failing zero row.
		self.assertEqual(round(debit3), 0)
		self.assertEqual(round(credit3), 0)

	def test_after_clear_residual_scrap_has_no_zero_pnl_row(self):
		"""Case 5/6: clearing residual yields balanced two-line scrap JE."""
		company = frappe.db.get_value("Company", {"default_currency": "IRR"}, "name")
		if not company:
			self.skipTest("No IRR company")

		disposal_account, cost_center = frappe.get_cached_value(
			"Company", company, ["disposal_account", "depreciation_cost_center"]
		)
		net_purchase = 2_500_000.0
		# After clear_sub_precision_scrap_vad_residual
		vad = 0.0
		asset = self._fake_asset(
			net_purchase=net_purchase,
			vad=vad,
			company=company,
			fixed_asset_account="FA",
			accum_account="ACCUM",
			disposal_account=disposal_account or "DISP",
			cost_center=cost_center or "CC",
		)
		disposal_values = {
			"value_after_depreciation": vad,
			"gross_asset_value": net_purchase,
		}

		with (
			patch(
				"erpnext.assets.doctype.asset.depreciation.get_depreciation_accounts",
				return_value=("FA", "ACCUM", "EXP"),
			),
			patch(
				"erpnext.assets.doctype.asset.depreciation.get_disposal_account_and_cost_center",
				return_value=(disposal_account or "DISP", cost_center or "CC"),
			),
		):
			entries = get_gl_entries_on_asset_disposal(
				asset, selling_amount=0, date="2026-11-15", disposal_values=disposal_values
			)

		_assert_no_zero_rows(self, entries)
		_assert_balanced(self, entries)
		self.assertEqual(len(entries), 2)


class TestAsset2061ScrapIntegration(unittest.TestCase):
	"""Safe scrap+restore on Asset 2061 (rollback if anything fails mid-way)."""

	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		cls.has_asset = bool(frappe.db.exists("Asset", ASSET_2061))

	def test_scrap_then_restore_succeeds_without_zero_je_rows(self):
		if not self.has_asset:
			self.skipTest(f"Asset {ASSET_2061} not on site")

		from erpnext_extensions.asset_usage_depreciation import integration_hooks

		asset = frappe.get_doc("Asset", ASSET_2061)
		if asset.status == "Scrapped":
			integration_hooks.restore_asset(ASSET_2061)
			frappe.db.commit()
			asset.reload()

		self.assertNotEqual(asset.status, "Scrapped")
		# Scrap on today (must be >= last booked depreciation). Using the last
		# depreciation date itself can leave ADS with no unposted row for the
		# disposal-day write-off and fails Iran ADS normalize.
		from erpnext.assets.doctype.asset.depreciation import get_last_depreciation_date
		from frappe.utils import getdate, today

		scrap_date = getdate(today())
		last_depr = get_last_depreciation_date(ASSET_2061)
		if last_depr and scrap_date < getdate(last_depr):
			self.skipTest(
				f"Cannot scrap {ASSET_2061}: today {scrap_date} before last depreciation {last_depr}"
			)

		vad_before = flt(asset.value_after_depreciation)
		try:
			result = integration_hooks.scrap_asset(ASSET_2061, scrap_date)
			frappe.db.commit()

			asset.reload()
			self.assertEqual(asset.status, "Scrapped")
			je_name = asset.journal_entry_for_scrap
			self.assertTrue(je_name)
			cleanup = (result or {}).get("residual_cleanup") or {}
			# 2061-shaped: disposal-day whole-IRR write-off leaves sub-rial VAD
			self.assertTrue(
				cleanup.get("cleared") or cleanup.get("reason") in ("already_zero", "above_threshold"),
				f"unexpected residual cleanup: {cleanup}",
			)

			je = frappe.get_doc("Journal Entry", je_name)
			rows = [
				{"debit": flt(r.debit), "credit": flt(r.credit), "account": r.account}
				for r in je.accounts
			]
			_assert_no_zero_rows(self, rows)
			_assert_balanced(self, rows)
			self.assertGreaterEqual(len(rows), 2)

			# Fully written-off after residual clear: no sub-rial JE noise
			for row in rows:
				self.assertFalse(
					abs(flt(row["debit"])) < 0.5
					and abs(flt(row["credit"])) < 0.5
					and abs(flt(row["debit"])) + abs(flt(row["credit"])) > 0,
					"sub-rial JE amounts should not survive on IRR scrap",
				)

			integration_hooks.restore_asset(ASSET_2061)
			frappe.db.commit()
			asset.reload()
			self.assertNotEqual(asset.status, "Scrapped")
			self.assertFalse(asset.journal_entry_for_scrap)
			# Carrying value restored (sub-rial residual may remain cleared)
			self.assertAlmostEqual(flt(asset.value_after_depreciation), vad_before, delta=1.0)
		finally:
			asset = frappe.get_doc("Asset", ASSET_2061)
			if asset.status == "Scrapped":
				integration_hooks.restore_asset(ASSET_2061)
				frappe.db.commit()


if __name__ == "__main__":
	unittest.main()

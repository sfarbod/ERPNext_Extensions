# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

"""v5.7.46 — adjusted gross disposal after AVA (FA / Accum only; no surplus JE).

Run::

    bench --site development.localhost run-tests \\
        --module erpnext_extensions.asset_usage_depreciation.tests.test_disposal_revalued_gross_v5746 \\
        --skip-before-tests
"""

from __future__ import annotations

import unittest
from unittest import mock
from unittest.mock import MagicMock

import frappe
from frappe.utils import cstr, flt

from erpnext_extensions.asset_usage_depreciation.disposal_patch import apply as apply_disposal_patch
from erpnext_extensions.asset_usage_depreciation.services.disposal_revalued_gross import (
	get_adjusted_asset_disposal_values,
	get_adjusted_gross_asset_value,
	get_gl_entries_on_asset_disposal_adjusted,
	get_submitted_ava_difference_rows,
	sum_submitted_ava_differences,
)

ASSET_2061 = "2061"


class TestAdjustedGrossHelpers(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		apply_disposal_patch()
		cls.has_2061 = bool(frappe.db.exists("Asset", ASSET_2061))

	def test_no_ava_gross_equals_net_purchase_plus_additional(self):
		asset = MagicMock()
		asset.name = "NO-AVA"
		asset.net_purchase_amount = 1_000_000
		asset.additional_asset_cost = 50_000
		asset.get_value_after_depreciation = MagicMock(return_value=800_000)

		with mock.patch(
			"erpnext_extensions.asset_usage_depreciation.services.disposal_revalued_gross.get_submitted_ava_difference_rows",
			return_value=[],
		):
			adj = get_adjusted_gross_asset_value(asset)
		self.assertEqual(adj["gross_asset_value"], 1_050_000)
		self.assertEqual(adj["ava_difference_total"], 0)

	def test_sums_multiple_signed_avas_excludes_draft_cancelled(self):
		submitted = [
			frappe._dict(
				name="A1", difference_amount=100, finance_book=None, difference_account="S"
			),
			frappe._dict(
				name="A2", difference_amount=-30, finance_book=None, difference_account="S"
			),
		]
		with mock.patch(
			"erpnext_extensions.asset_usage_depreciation.services.disposal_revalued_gross.frappe.get_all",
			return_value=submitted,
		):
			total = sum_submitted_ava_differences("X")
		self.assertEqual(total, 70)

	def test_asset_2061_adjusted_gross(self):
		if not self.has_2061:
			self.skipTest(f"Asset {ASSET_2061} not on site")
		asset = frappe.get_doc("Asset", ASSET_2061)
		adj = get_adjusted_gross_asset_value(asset)
		self.assertAlmostEqual(adj["original_purchase_cost"], 2_500_000, places=2)
		self.assertAlmostEqual(adj["ava_difference_total"], 3_738_720, delta=1.0)
		self.assertAlmostEqual(adj["gross_asset_value"], 6_238_720, delta=1.0)
		self.assertIn("at3rrcumcn", adj["ava_names"])
		# Draft/cancelled AVAs must not appear
		for name in adj["ava_names"]:
			self.assertEqual(frappe.db.get_value("Asset Value Adjustment", name, "docstatus"), 1)

	def test_cancelled_ava_excluded(self):
		if not self.has_2061:
			self.skipTest(f"Asset {ASSET_2061} not on site")
		rows = get_submitted_ava_difference_rows(ASSET_2061)
		cancelled = frappe.get_all(
			"Asset Value Adjustment",
			filters={"asset": ASSET_2061, "docstatus": 2},
			pluck="name",
		)
		for c in cancelled:
			self.assertNotIn(c, [r.name for r in rows])

	def test_gl_uses_adjusted_gross_two_rows_only(self):
		if not self.has_2061:
			self.skipTest(f"Asset {ASSET_2061} not on site")
		asset = frappe.get_doc("Asset", ASSET_2061)
		if asset.status == "Scrapped":
			self.skipTest("Asset scrapped")
		dv = get_adjusted_asset_disposal_values(asset)
		dv = dict(dv)
		dv["value_after_depreciation"] = 0
		entries = get_gl_entries_on_asset_disposal_adjusted(
			asset, selling_amount=0, date="2026-11-16", disposal_values=dv, include_surplus_transfer=False
		)
		self.assertEqual(len(entries), 2)
		fa_credit = flt(entries[0].get("credit"))
		accum_debit = flt(entries[1].get("debit"))
		self.assertAlmostEqual(fa_credit, 6_238_720, delta=1.0)
		self.assertAlmostEqual(accum_debit, 6_238_720, delta=1.0)
		for e in entries:
			acct = cstr(e.get("account"))
			self.assertNotIn("311303", acct)
			self.assertNotIn("311301", acct)
			self.assertNotIn("622508", acct)
		total_d = sum(flt(e.get("debit")) for e in entries)
		total_c = sum(flt(e.get("credit")) for e in entries)
		self.assertAlmostEqual(total_d, total_c, places=2)


class TestAsset2061ScrapIntegrationAdjustedGross(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		apply_disposal_patch()
		cls.has_asset = bool(frappe.db.exists("Asset", ASSET_2061))

	def test_scrap_restore_scrap_uses_adjusted_gross(self):
		if not self.has_asset:
			self.skipTest(f"Asset {ASSET_2061} not on site")

		from erpnext.assets.doctype.asset.depreciation import get_last_depreciation_date
		from erpnext_extensions.asset_usage_depreciation import integration_hooks
		from frappe.utils import getdate, today

		asset = frappe.get_doc("Asset", ASSET_2061)
		if asset.status == "Scrapped":
			integration_hooks.restore_asset(ASSET_2061)
			frappe.db.commit()
			asset.reload()

		self.assertEqual(asset.status, "Submitted")
		self.assertFalse(asset.journal_entry_for_scrap)

		scrap_date = getdate(today())
		last_depr = get_last_depreciation_date(ASSET_2061)
		if last_depr and scrap_date < getdate(last_depr):
			self.skipTest("today before last depreciation")

		vad_before = flt(asset.value_after_depreciation)
		try:
			result = integration_hooks.scrap_asset(ASSET_2061, scrap_date)
			frappe.db.commit()
			asset.reload()
			self.assertEqual(asset.status, "Scrapped")
			je_name = asset.journal_entry_for_scrap
			self.assertTrue(je_name)

			dv = (result or {}).get("disposal_values") or {}
			self.assertAlmostEqual(flt(dv.get("gross_asset_value")), 6_238_720, delta=1.0)

			je = frappe.get_doc("Journal Entry", je_name)
			rows = list(je.accounts)
			self.assertEqual(len(rows), 2, f"Scrap JE must be exactly 2 rows, got {len(rows)}")
			fa_credit = sum(flt(r.credit) for r in rows if "121013" in (r.account or ""))
			accum_debit = sum(flt(r.debit) for r in rows if "121114" in (r.account or ""))
			self.assertAlmostEqual(fa_credit, 6_238_720, delta=1.0)
			self.assertAlmostEqual(accum_debit, 6_238_720, delta=1.0)
			for r in rows:
				acct = r.account or ""
				self.assertNotIn("311303", acct)
				self.assertNotIn("311301", acct)
				self.assertNotIn("622508", acct)
				self.assertFalse(
					abs(flt(r.debit)) < 1e-9 and abs(flt(r.credit)) < 1e-9,
					f"zero D/C row: {r.account}",
				)
			total_d = sum(flt(r.debit) for r in rows)
			total_c = sum(flt(r.credit) for r in rows)
			self.assertAlmostEqual(total_d, total_c, places=2)

			# Restore
			integration_hooks.restore_asset(ASSET_2061)
			frappe.db.commit()
			asset.reload()
			self.assertNotEqual(asset.status, "Scrapped")
			self.assertFalse(asset.journal_entry_for_scrap)
			self.assertAlmostEqual(flt(asset.value_after_depreciation), vad_before, delta=1.0)

			# Scrap again
			result2 = integration_hooks.scrap_asset(ASSET_2061, scrap_date)
			frappe.db.commit()
			asset.reload()
			self.assertEqual(asset.status, "Scrapped")
			je2 = frappe.get_doc("Journal Entry", asset.journal_entry_for_scrap)
			fa2 = sum(flt(r.credit) for r in je2.accounts if "121013" in (r.account or ""))
			self.assertAlmostEqual(fa2, 6_238_720, delta=1.0)
			self.assertAlmostEqual(
				flt((result2 or {}).get("disposal_values", {}).get("gross_asset_value")),
				6_238_720,
				delta=1.0,
			)
		finally:
			asset = frappe.get_doc("Asset", ASSET_2061)
			if asset.status == "Scrapped":
				integration_hooks.restore_asset(ASSET_2061)
				frappe.db.commit()


if __name__ == "__main__":
	unittest.main()

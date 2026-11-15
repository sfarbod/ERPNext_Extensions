# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""v5.7.40 — Asset Value Adjustment for non-depreciating assets.

Covers the production failure mode for land / right-of-use style assets where
``calculate_depreciation = 0`` (and orphan finance-book rows may remain).

Run::

    bench --site development.localhost run-tests \\
        --module erpnext_extensions.asset_usage_depreciation.tests.test_asset_value_adjustment_non_depreciating_v5740 \\
        --skip-before-tests
"""

from __future__ import annotations

import unittest

import frappe
from frappe.utils import flt, random_string

from erpnext.assets.doctype.asset_depreciation_schedule.asset_depreciation_schedule import (
	get_asset_depr_schedule_doc,
)

from erpnext_extensions.asset_usage_depreciation.tests.replan_fixtures import ensure_replan_fixtures
from erpnext_extensions.asset_usage_depreciation.tests.test_asset_usage_replan import _make_sl_asset

NON_DEPR_CATEGORY = "_AUD NonDepr AVA Category"
DIFF_ACCOUNT_NAME = "_AUD Revaluation Surplus"


def _fx() -> dict:
	return ensure_replan_fixtures()


def _unique(prefix: str) -> str:
	return f"{prefix}-{random_string(6)}"


def _ensure_non_depr_category(company: str, accounts: dict) -> str:
	"""Category marked non-depreciable so AVA JE only needs the fixed-asset account."""
	if frappe.db.exists("Asset Category", NON_DEPR_CATEGORY):
		cat = frappe.get_doc("Asset Category", NON_DEPR_CATEGORY)
		if not cint_bool(cat.non_depreciable_category):
			cat.non_depreciable_category = 1
			cat.save(ignore_permissions=True)
		if not any(row.company_name == company for row in (cat.accounts or [])):
			cat.append("accounts", {"company_name": company, **accounts})
			cat.save(ignore_permissions=True)
		return NON_DEPR_CATEGORY

	cat = frappe.get_doc(
		{
			"doctype": "Asset Category",
			"asset_category_name": NON_DEPR_CATEGORY,
			"enable_cwip_accounting": 0,
			"non_depreciable_category": 1,
			"accounts": [
				{
					"company_name": company,
					"fixed_asset_account": accounts["fixed_asset_account"],
					"accumulated_depreciation_account": accounts.get("accumulated_depreciation_account"),
					"depreciation_expense_account": accounts.get("depreciation_expense_account"),
				}
			],
		}
	)
	cat.insert(ignore_permissions=True)
	return cat.name


def cint_bool(value) -> bool:
	return bool(int(value or 0))


def _ensure_difference_account(company: str) -> str:
	existing = frappe.db.get_value(
		"Account",
		{"account_name": DIFF_ACCOUNT_NAME, "company": company},
		"name",
	)
	if existing:
		return existing

	parent = frappe.db.get_value(
		"Account",
		{"company": company, "root_type": "Equity", "is_group": 1},
		"name",
	) or frappe.db.get_value(
		"Account",
		{"company": company, "root_type": "Income", "is_group": 1},
		"name",
	)
	if not parent:
		frappe.throw(f"No Equity/Income parent account for {company}")

	doc = frappe.get_doc(
		{
			"doctype": "Account",
			"account_name": DIFF_ACCOUNT_NAME,
			"parent_account": parent,
			"company": company,
			"is_group": 0,
			"account_type": "",
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


def _ensure_non_depr_item(category: str) -> str:
	code = "_AUD NonDepr AVA Item"
	if frappe.db.exists("Item", code):
		frappe.db.set_value("Item", code, "asset_category", category)
		return code
	item_group = (
		"All Item Groups"
		if frappe.db.exists("Item Group", "All Item Groups")
		else (frappe.get_all("Item Group", pluck="name", limit=1) or [None])[0]
	)
	stock_uom = "Nos" if frappe.db.exists("UOM", "Nos") else frappe.get_all("UOM", pluck="name", limit=1)[0]
	item = frappe.get_doc(
		{
			"doctype": "Item",
			"item_code": code,
			"item_name": code,
			"item_group": item_group,
			"stock_uom": stock_uom,
			"is_stock_item": 0,
			"is_fixed_asset": 1,
			"is_grouped_asset": 0,
			"auto_create_assets": 0,
			"asset_category": category,
		}
	)
	item.insert(ignore_permissions=True)
	return item.name


def _make_non_depr_asset(*, purchase_amount: float = 15_000_000_000.0, with_orphan_fb: bool = True):
	"""Create a submitted non-depreciating asset; optionally plant orphan FB rows.

	Orphan finance-book children match production land assets where
	``calculate_depreciation=0`` but ``tabAsset Finance Book`` still has rows —
	the condition that made core AVA fail before v5.7.40.
	"""
	fx = _fx()
	company = fx["company"]
	category = _ensure_non_depr_category(company, fx["accounts"])
	item_code = _ensure_non_depr_item(category)
	asset_name = _unique("AUD-NonDepr")

	payload = {
		"doctype": "Asset",
		"asset_name": asset_name,
		"asset_category": category,
		"item_code": item_code,
		"company": company,
		"purchase_date": "2026-01-01",
		"available_for_use_date": "2026-01-01",
		"calculate_depreciation": 0,
		"net_purchase_amount": purchase_amount,
		"purchase_amount": purchase_amount,
		"location": fx["location"],
		"asset_owner": "Company",
		"asset_type": "Existing Asset",
		"asset_quantity": 1,
	}
	meta = frappe.get_meta("Asset")
	if meta.has_field("cost_center"):
		payload["cost_center"] = fx["cost_center"]

	asset = frappe.get_doc(payload)
	if (meta.autoname or "") == "prompt":
		asset.name = asset_name
		asset.flags.name_set = True
	asset.flags.ignore_permissions = True
	asset.insert(ignore_permissions=True)
	asset.submit()

	# Ensure header VAD equals purchase amount (Existing Asset path).
	frappe.db.set_value(
		"Asset",
		asset.name,
		"value_after_depreciation",
		purchase_amount,
		update_modified=False,
	)

	if with_orphan_fb:
		_plant_orphan_finance_book(asset.name, value=purchase_amount)

	asset.reload()
	return asset


def _plant_orphan_finance_book(asset_name: str, *, value: float) -> None:
	"""Insert a stale finance-book child without enabling depreciation."""
	if frappe.db.exists("Asset Finance Book", {"parent": asset_name}):
		return
	fb = frappe.get_doc(
		{
			"doctype": "Asset Finance Book",
			"parent": asset_name,
			"parenttype": "Asset",
			"parentfield": "finance_books",
			"finance_book": None,
			"depreciation_method": "Straight Line",
			"total_number_of_depreciations": 12,
			"frequency_of_depreciation": 1,
			"depreciation_start_date": "2026-01-31",
			"expected_value_after_useful_life": 0,
			"value_after_depreciation": value,
			"idx": 1,
		}
	)
	fb.insert(ignore_permissions=True)
	# Keep calculate_depreciation disabled — do not save Asset (that would clear FB).
	frappe.db.set_value("Asset", asset_name, "calculate_depreciation", 0, update_modified=False)


def _make_ava(*, asset, new_value: float, date: str = "2026-07-23", finance_book=None):
	fx = _fx()
	asset.reload()
	current = flt(asset.value_after_depreciation)
	doc = frappe.get_doc(
		{
			"doctype": "Asset Value Adjustment",
			"company": asset.company,
			"asset": asset.name,
			"date": date,
			"current_asset_value": current,
			"new_asset_value": new_value,
			"cost_center": fx["cost_center"],
			"difference_account": _ensure_difference_account(asset.company),
			"finance_book": finance_book,
		}
	)
	doc.insert(ignore_permissions=True)
	return doc


def _ads_count(asset_name: str) -> int:
	return frappe.db.count(
		"Asset Depreciation Schedule",
		{"asset": asset_name, "docstatus": ["<", 2]},
	)


def _active_ads_count(asset_name: str) -> int:
	return frappe.db.count(
		"Asset Depreciation Schedule",
		{"asset": asset_name, "docstatus": 1},
	)


def _je_gl_balance(je_name: str) -> tuple[float, float]:
	rows = frappe.db.sql(
		"""
		select coalesce(sum(debit), 0), coalesce(sum(credit), 0)
		from `tabGL Entry`
		where voucher_type='Journal Entry' and voucher_no=%s and is_cancelled=0
		""",
		je_name,
	)[0]
	return flt(rows[0]), flt(rows[1])


class TestAssetValueAdjustmentNonDepreciatingV5740(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		fx = ensure_replan_fixtures()
		_ensure_non_depr_category(fx["company"], fx["accounts"])
		_ensure_difference_account(fx["company"])
		# Confirm override is active in this process
		from frappe.model.base_document import get_controller

		ctrl = get_controller("Asset Value Adjustment")
		cls.override_active = ctrl.__module__.endswith("asset_value_adjustment_override")

	def setUp(self):
		if not self.override_active:
			self.skipTest("Asset Value Adjustment override not loaded; clear-cache / restart workers")

	def test_01_upward_revaluation_non_depreciating(self):
		asset = _make_non_depr_asset(purchase_amount=15_000_000_000.0, with_orphan_fb=True)
		self.assertEqual(cint_bool(asset.calculate_depreciation), False)
		self.assertGreaterEqual(
			frappe.db.count("Asset Finance Book", {"parent": asset.name}),
			1,
		)

		new_value = 405_000_000_000.0  # mirrors production ن/89 scale
		ava = _make_ava(asset=asset, new_value=new_value)
		ava.submit()

		asset.reload()
		self.assertEqual(cint_bool(asset.calculate_depreciation), False)
		self.assertAlmostEqual(flt(asset.value_after_depreciation), new_value, places=2)
		self.assertEqual(_active_ads_count(asset.name), 0)
		self.assertTrue(ava.journal_entry)
		debit, credit = _je_gl_balance(ava.journal_entry)
		self.assertAlmostEqual(debit, credit, places=2)
		self.assertAlmostEqual(debit, flt(new_value - 15_000_000_000.0), places=2)
		self.assertAlmostEqual(flt(ava.difference_amount), new_value - 15_000_000_000.0, places=2)

	def test_02_downward_revaluation_non_depreciating(self):
		asset = _make_non_depr_asset(purchase_amount=100_000_000.0, with_orphan_fb=True)
		new_value = 40_000_000.0
		ava = _make_ava(asset=asset, new_value=new_value)
		ava.submit()

		asset.reload()
		self.assertEqual(cint_bool(asset.calculate_depreciation), False)
		self.assertAlmostEqual(flt(asset.value_after_depreciation), new_value, places=2)
		self.assertEqual(_active_ads_count(asset.name), 0)
		debit, credit = _je_gl_balance(ava.journal_entry)
		self.assertAlmostEqual(debit, credit, places=2)
		self.assertAlmostEqual(flt(ava.difference_amount), new_value - 100_000_000.0, places=2)

	def test_03_cancel_reverses_non_depreciating(self):
		asset = _make_non_depr_asset(purchase_amount=50_000_000.0, with_orphan_fb=True)
		before = flt(asset.value_after_depreciation)
		ava = _make_ava(asset=asset, new_value=80_000_000.0)
		ava.submit()
		je = ava.journal_entry
		ava.cancel()

		asset.reload()
		self.assertAlmostEqual(flt(asset.value_after_depreciation), before, places=2)
		self.assertEqual(cint_bool(asset.calculate_depreciation), False)
		self.assertEqual(_active_ads_count(asset.name), 0)
		# Cancelled JE must leave no active GL
		active = frappe.db.count(
			"GL Entry",
			{"voucher_type": "Journal Entry", "voucher_no": je, "is_cancelled": 0},
		)
		self.assertEqual(active, 0)

	def test_04_depreciating_asset_regression(self):
		"""Existing AVA + ADS rebuild path must remain unchanged."""
		asset = _make_sl_asset(
			asset_name=_unique("AUD-DeprAVA"),
			net_purchase_amount=120_000,
			purchase_amount=120_000,
			total_number_of_depreciations=12,
			frequency_of_depreciation=1,
			depreciation_start_date="2026-01-31",
		)
		before_ads = get_asset_depr_schedule_doc(asset.name, "Active")
		self.assertIsNotNone(before_ads)

		current = flt(asset.value_after_depreciation)
		ava = _make_ava(asset=asset, new_value=current + 10_000)
		# Match finance book on depreciating assets when present
		if asset.finance_books and asset.finance_books[0].finance_book:
			ava.finance_book = asset.finance_books[0].finance_book
			ava.save(ignore_permissions=True)
		ava.submit()

		asset.reload()
		self.assertEqual(cint_bool(asset.calculate_depreciation), True)
		self.assertAlmostEqual(flt(asset.value_after_depreciation), current + 10_000, places=2)
		after_ads = get_asset_depr_schedule_doc(asset.name, "Active")
		self.assertIsNotNone(after_ads)
		self.assertNotEqual(before_ads.name, after_ads.name)
		before_ads.reload()
		self.assertEqual(before_ads.docstatus, 2)

	def test_05_cancel_depreciating_asset(self):
		asset = _make_sl_asset(
			asset_name=_unique("AUD-DeprCancel"),
			net_purchase_amount=90_000,
			purchase_amount=90_000,
		)
		before = flt(asset.value_after_depreciation)
		ava = _make_ava(asset=asset, new_value=before + 5_000)
		if asset.finance_books and asset.finance_books[0].finance_book:
			ava.finance_book = asset.finance_books[0].finance_book
			ava.save(ignore_permissions=True)
		ava.submit()
		je = ava.journal_entry
		ava.cancel()

		asset.reload()
		self.assertAlmostEqual(flt(asset.value_after_depreciation), before, places=2)
		active = frappe.db.count(
			"GL Entry",
			{"voucher_type": "Journal Entry", "voucher_no": je, "is_cancelled": 0},
		)
		self.assertEqual(active, 0)
		self.assertIsNotNone(get_asset_depr_schedule_doc(asset.name, "Active"))

	def test_06_no_duplicate_je_on_single_submit(self):
		asset = _make_non_depr_asset(purchase_amount=12_000_000.0, with_orphan_fb=True)
		ava = _make_ava(asset=asset, new_value=18_000_000.0)
		ava.submit()
		jes = frappe.get_all(
			"Journal Entry",
			filters={"name": ava.journal_entry},
			fields=["name", "docstatus"],
		)
		self.assertEqual(len(jes), 1)
		self.assertEqual(jes[0].docstatus, 1)
		# Remark uniqueness: only one revaluation JE linked
		linked = frappe.db.count("Asset Value Adjustment", {"journal_entry": ava.journal_entry})
		self.assertEqual(linked, 1)

	def test_07_finance_book_and_cost_center(self):
		fx = _fx()
		asset = _make_non_depr_asset(purchase_amount=25_000_000.0, with_orphan_fb=True)
		ava = _make_ava(
			asset=asset,
			new_value=30_000_000.0,
			finance_book=fx["finance_book"],
		)
		self.assertEqual(ava.cost_center, fx["cost_center"])
		ava.submit()
		je = frappe.get_doc("Journal Entry", ava.journal_entry)
		self.assertEqual(je.finance_book, fx["finance_book"])
		for row in je.accounts:
			self.assertEqual(row.cost_center, fx["cost_center"])
		asset.reload()
		self.assertAlmostEqual(flt(asset.value_after_depreciation), 30_000_000.0, places=2)
		self.assertEqual(_active_ads_count(asset.name), 0)

	def test_08_production_failure_reproduction_shape(self):
		"""Representative fixture for ن/89 / ن/444 / ن/447: calc_dep=0 + orphan FB."""
		asset = _make_non_depr_asset(purchase_amount=3_617_110_000.0, with_orphan_fb=True)
		self.assertFalse(cint_bool(asset.calculate_depreciation))
		self.assertGreaterEqual(frappe.db.count("Asset Finance Book", {"parent": asset.name}), 1)
		self.assertEqual(_ads_count(asset.name), 0)

		# Core path would throw here without the override.
		ava = _make_ava(asset=asset, new_value=44_078_369_559.0)
		ava.submit()

		asset.reload()
		self.assertFalse(cint_bool(asset.calculate_depreciation))
		self.assertEqual(_active_ads_count(asset.name), 0)
		self.assertAlmostEqual(flt(asset.value_after_depreciation), 44_078_369_559.0, places=2)
		debit, credit = _je_gl_balance(ava.journal_entry)
		self.assertAlmostEqual(debit, credit, places=2)

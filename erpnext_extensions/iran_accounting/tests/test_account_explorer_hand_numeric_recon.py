# Copyright (c) 2026, Farbod Siyahpoosh and contributors
"""Hand-calculated stock↔GL numeric contract fixtures (v5.1.1).

Expected values are derived from the scenario description — NOT from AE output.
"""

from __future__ import annotations

import unittest
from datetime import timedelta

import frappe
from erpnext.stock import get_warehouse_account_map
from frappe.utils import flt, getdate

from erpnext_extensions.iran_accounting.account_explorer.api import (
	get_account_summary,
	get_inventory_account_summary,
	get_item_group_summary,
	get_item_summary,
)
from erpnext_extensions.iran_accounting.account_explorer.inventory_account_measures import (
	get_inventory_account_attribution,
)
from erpnext_extensions.iran_accounting.account_explorer.query_spec import (
	AccountExplorerQuerySpec_from_client,
)
from erpnext_extensions.iran_accounting.tests.test_account_explorer_fixtures import (
	build_payload,
	current_fiscal_year,
)
from erpnext_extensions.iran_accounting.tests.test_account_explorer_inventory_axes import (
	_ensure_item_group,
	_ensure_stock_item,
	_insert_sle,
	enable_inventory_analysis,
	getdate_safe,
	require_inventory_company,
)


class TestHandCalculatedStockGlNumericContract(unittest.TestCase):
	"""Opening 100 + Receipt 50 + Issue 30 + value-only +10 + return +5.

	Hand stock (warehouse inventory account W):
	  Opening value 100 → rolled into Inward
	  Period inward 50+10+5 = 65
	  Period outward 30
	  Displayed Inward = 100+65 = 165
	  Displayed Outward = 30
	  Balance = 135

	Hand GL semantics (asset inventory account, ERPNext-like):
	  Opening net debit 100
	  Period debit 65 (receipts / value-up / return)
	  Period credit 30 (issue)
	  Closing net debit 135

	Note: This fixture posts SLE only via _insert_sle (no GL). Case A Account
	Levels (sle_scoped_stock) and Item Group must still match the hand stock
	numbers. A separate multi-account SLE mapping test covers Σ accounts = Item Group.
	"""

	@classmethod
	def setUpClass(cls):
		cls.company = require_inventory_company(cls)
		enable_inventory_analysis()
		fy = current_fiscal_year(cls.company)
		if not fy:
			raise unittest.SkipTest("No fiscal year")
		cls.fiscal_year, cls.fy_start, cls.fy_end = fy
		cls.from_date = str(getdate_safe(cls.fy_start) + timedelta(days=10))
		cls.to_date = str(getdate_safe(cls.fy_start) + timedelta(days=40))
		pre = str(getdate_safe(cls.fy_start) + timedelta(days=2))
		mid = str(getdate_safe(cls.fy_start) + timedelta(days=20))

		suffix = frappe.generate_hash(length=6)
		cls.group = _ensure_item_group(f"AE Hand IG {suffix}", "All Item Groups", is_group=0)
		warehouses = frappe.get_all(
			"Warehouse",
			filters={"company": cls.company, "is_group": 0},
			pluck="name",
			order_by="name",
		)
		# Prefer ordinary ERPNext warehouses over forensic/unmapped fixtures
		preferred = [w for w in warehouses if not w.startswith("AE ")] or warehouses
		if not preferred:
			raise unittest.SkipTest("No warehouse")
		cls.wh = preferred[0]
		wh_map = get_warehouse_account_map(cls.company) or {}
		info = wh_map.get(cls.wh)
		if not info or not getattr(info, "account", None):
			raise unittest.SkipTest("Warehouse has no inventory account")
		cls.inv_account = info.account
		cls.item = _ensure_stock_item(f"AE-HAND-{suffix}", cls.group, cls.company, cls.wh)

		# Hand scenario SLEs (values only — qty mirrors value for simplicity)
		_insert_sle(
			company=cls.company,
			item_code=cls.item,
			warehouse=cls.wh,
			posting_date=pre,
			actual_qty=1,
			stock_value_difference=100,
			voucher_suffix=f"HAND-OPEN-{suffix}",
		)
		_insert_sle(
			company=cls.company,
			item_code=cls.item,
			warehouse=cls.wh,
			posting_date=mid,
			actual_qty=1,
			stock_value_difference=50,
			voucher_suffix=f"HAND-RCP-{suffix}",
		)
		_insert_sle(
			company=cls.company,
			item_code=cls.item,
			warehouse=cls.wh,
			posting_date=mid,
			actual_qty=-1,
			stock_value_difference=-30,
			voucher_suffix=f"HAND-ISS-{suffix}",
		)
		_insert_sle(
			company=cls.company,
			item_code=cls.item,
			warehouse=cls.wh,
			posting_date=mid,
			actual_qty=0,
			stock_value_difference=10,
			voucher_suffix=f"HAND-VAL-{suffix}",
		)
		_insert_sle(
			company=cls.company,
			item_code=cls.item,
			warehouse=cls.wh,
			posting_date=mid,
			actual_qty=1,
			stock_value_difference=5,
			voucher_suffix=f"HAND-RET-{suffix}",
		)

		# Hand-calculated expected (NOT from AE)
		cls.exp_opening = 100.0
		cls.exp_period_in = 50.0 + 10.0 + 5.0
		cls.exp_period_out = 30.0
		cls.exp_inward = cls.exp_opening + cls.exp_period_in  # 165
		cls.exp_outward = cls.exp_period_out  # 30
		cls.exp_balance = cls.exp_inward - cls.exp_outward  # 135

	def _payload(self, axis: str) -> dict:
		raw = build_payload(
			self.company,
			self.fiscal_year,
			self.from_date,
			self.to_date,
			analysis={"view_axis": axis, "page_size": 200},
			document={
				"hide_zero_rows": 0,
				"inventory": {"item_group": self.group},
				"status": {
					"include_opening_entries": 1,
					"include_cancelled_entries": 0,
					"include_default_finance_book_entries": 1,
					"include_period_closing_vouchers": 0,
				},
			},
		)
		payload = frappe.parse_json(raw)
		payload["prepared_mode"] = "live"
		return payload

	def test_hand_stock_item_group_inward_outward_balance(self):
		res = get_item_group_summary(self._payload("item_group"))
		t = res["totals"]
		self.assertAlmostEqual(flt(t["inward_value"]), self.exp_inward, places=2)
		self.assertAlmostEqual(flt(t["outward_value"]), self.exp_outward, places=2)
		self.assertAlmostEqual(flt(t["balance_value"]), self.exp_balance, places=2)

	def test_hand_stock_inventory_account_matches(self):
		res = get_inventory_account_summary(self._payload("inventory_account"))
		t = res["totals"]
		self.assertAlmostEqual(flt(t["inward_value"]), self.exp_inward, places=2)
		self.assertAlmostEqual(flt(t["outward_value"]), self.exp_outward, places=2)
		self.assertAlmostEqual(flt(t["balance_value"]), self.exp_balance, places=2)
		row = next(
			(r for r in res["rows"] if r.get("inventory_account") == self.inv_account),
			None,
		)
		self.assertIsNotNone(row)
		self.assertAlmostEqual(flt(row["inward_value"]), self.exp_inward, places=2)

	def test_hand_item_equals_item_group(self):
		ig = get_item_group_summary(self._payload("item_group"))["totals"]
		it = get_item_summary(self._payload("item"))["totals"]
		for k in ("inward_value", "outward_value", "balance_value"):
			self.assertAlmostEqual(flt(ig[k]), flt(it[k]), places=2)

	def test_account_axis_case_a_equals_item_group(self):
		"""Case A: Account under Item Group filter equals hand stock totals (sle_scoped)."""
		ig = get_item_group_summary(self._payload("item_group"))["totals"]
		ac = get_account_summary(self._payload("account_level"))["totals"]
		self.assertEqual(
			get_account_summary(self._payload("account_level")).get("account_fact_engine"),
			"sle_scoped_stock",
		)
		self.assertAlmostEqual(flt(ig["inward_value"]), flt(ac["period_debit"]), places=2)
		self.assertAlmostEqual(flt(ig["outward_value"]), flt(ac["period_credit"]), places=2)
		self.assertAlmostEqual(flt(ig["balance_value"]), flt(ac["net_balance"]), places=2)
		self.assertAlmostEqual(flt(ac["period_debit"]), self.exp_inward, places=2)
		self.assertAlmostEqual(flt(ac["period_credit"]), self.exp_outward, places=2)

	def test_account_axis_case_b_cleared_filter_is_posted_gl(self):
		"""Case B: clearing inventory filter returns posted_gl (no reverse equality)."""
		cleared = self._payload("account_level")
		cleared["document_scope"]["inventory"] = {
			"item_group": None,
			"item": None,
			"warehouse": None,
		}
		res = get_account_summary(cleared)
		self.assertEqual(res.get("account_fact_engine"), "posted_gl")
		from erpnext_extensions.iran_accounting.account_explorer.cross_tab_numeric_contract import (
			relation,
		)

		self.assertEqual(relation("account_level", "item_group", "period_debit"), "RECONCILABLE")


class TestMultiAccountSleMappedEqualsItemGroup(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.company = require_inventory_company(cls)
		enable_inventory_analysis()
		fy = current_fiscal_year(cls.company)
		if not fy:
			raise unittest.SkipTest("No FY")
		cls.fiscal_year, cls.fy_start, cls.fy_end = fy
		cls.from_date = str(getdate_safe(cls.fy_start) + timedelta(days=5))
		cls.to_date = str(getdate_safe(cls.fy_start) + timedelta(days=50))
		mid = str(getdate_safe(cls.fy_start) + timedelta(days=15))

		suffix = frappe.generate_hash(length=6)
		cls.group = _ensure_item_group(f"AE MultiAcct IG {suffix}", "All Item Groups", is_group=0)
		warehouses = frappe.get_all(
			"Warehouse",
			filters={"company": cls.company, "is_group": 0},
			pluck="name",
			order_by="name",
		)
		preferred = [w for w in warehouses if not str(w).startswith("AE ")] or warehouses
		if len(preferred) < 2:
			raise unittest.SkipTest("Need 2 warehouses")
		stock_accounts = frappe.get_all(
			"Account",
			filters={"company": cls.company, "account_type": "Stock", "is_group": 0},
			pluck="name",
			order_by="name",
		)
		if len(stock_accounts) < 2:
			raise unittest.SkipTest("Need 2 stock accounts")
		cls.wh_a, cls.wh_b = preferred[0], preferred[1]
		cls.acct_a, cls.acct_b = stock_accounts[0], stock_accounts[1]
		frappe.db.set_value("Warehouse", cls.wh_a, "account", cls.acct_a, update_modified=False)
		frappe.db.set_value("Warehouse", cls.wh_b, "account", cls.acct_b, update_modified=False)
		frappe.db.commit()
		frappe.clear_cache()
		frappe.flags.pop("warehouse_account_map", None)

		cls.item_a = _ensure_stock_item(f"AE-MA-{suffix}-A", cls.group, cls.company, cls.wh_a)
		cls.item_b = _ensure_stock_item(f"AE-MA-{suffix}-B", cls.group, cls.company, cls.wh_b)
		# Hand: A inward 200 out 40 bal 160; B inward 90 out 20 bal 70; IG 290/60/230
		_insert_sle(
			company=cls.company,
			item_code=cls.item_a,
			warehouse=cls.wh_a,
			posting_date=mid,
			actual_qty=2,
			stock_value_difference=200,
			voucher_suffix=f"MA-A-IN-{suffix}",
		)
		_insert_sle(
			company=cls.company,
			item_code=cls.item_a,
			warehouse=cls.wh_a,
			posting_date=mid,
			actual_qty=-1,
			stock_value_difference=-40,
			voucher_suffix=f"MA-A-OUT-{suffix}",
		)
		_insert_sle(
			company=cls.company,
			item_code=cls.item_b,
			warehouse=cls.wh_b,
			posting_date=mid,
			actual_qty=1,
			stock_value_difference=90,
			voucher_suffix=f"MA-B-IN-{suffix}",
		)
		_insert_sle(
			company=cls.company,
			item_code=cls.item_b,
			warehouse=cls.wh_b,
			posting_date=mid,
			actual_qty=-1,
			stock_value_difference=-20,
			voucher_suffix=f"MA-B-OUT-{suffix}",
		)
		cls.exp = {"inward": 290.0, "outward": 60.0, "balance": 230.0}

	def _payload(self, axis: str) -> dict:
		raw = build_payload(
			self.company,
			self.fiscal_year,
			self.from_date,
			self.to_date,
			analysis={"view_axis": axis, "page_size": 200},
			document={"hide_zero_rows": 0, "inventory": {"item_group": self.group}},
		)
		payload = frappe.parse_json(raw)
		payload["prepared_mode"] = "live"
		return payload

	def test_item_group_equals_sum_inventory_accounts(self):
		ig = get_item_group_summary(self._payload("item_group"))["totals"]
		inv = get_inventory_account_summary(self._payload("inventory_account"))
		self.assertAlmostEqual(flt(ig["inward_value"]), self.exp["inward"], places=2)
		self.assertAlmostEqual(flt(ig["outward_value"]), self.exp["outward"], places=2)
		self.assertAlmostEqual(flt(ig["balance_value"]), self.exp["balance"], places=2)
		self.assertAlmostEqual(flt(inv["totals"]["inward_value"]), self.exp["inward"], places=2)
		self.assertAlmostEqual(flt(inv["totals"]["outward_value"]), self.exp["outward"], places=2)
		self.assertAlmostEqual(flt(inv["totals"]["balance_value"]), self.exp["balance"], places=2)
		codes = {r.get("inventory_account") for r in inv["rows"]}
		self.assertIn(self.acct_a, codes)
		self.assertIn(self.acct_b, codes)

	def test_sle_attribution_per_account_hand_values(self):
		spec = AccountExplorerQuerySpec_from_client(self._payload("inventory_account"), require_dates=True)
		attr = get_inventory_account_attribution(spec)
		a = attr.rows_by_account[self.acct_a]
		b = attr.rows_by_account[self.acct_b]
		self.assertAlmostEqual(flt(a["inward_value"]), 200.0, places=2)
		self.assertAlmostEqual(flt(a["outward_value"]), 40.0, places=2)
		self.assertAlmostEqual(flt(a["balance_value"]), 160.0, places=2)
		self.assertAlmostEqual(flt(b["inward_value"]), 90.0, places=2)
		self.assertAlmostEqual(flt(b["outward_value"]), 20.0, places=2)
		self.assertAlmostEqual(flt(b["balance_value"]), 70.0, places=2)

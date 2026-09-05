# Copyright (c) 2026, Farbod Siyahpoosh and contributors
"""Shared inventory account: Case A Account breakdown isolates by Item Group / Item.

Hand-defined fixture (sign-off §11):
  Group A + Group B both post SLE onto the SAME warehouse inventory account.
  Filter A must exclude peer B; unfiltered sums A+B.
  Case A Account under Item Group filter = SLE-scoped stock (EQUAL to IG).
  Case B Account without inventory filter remains posted GL (reverse not EQUAL).
"""

from __future__ import annotations

import unittest
from datetime import timedelta

import frappe
from erpnext.stock import get_warehouse_account_map
from frappe.utils import flt

from erpnext_extensions.iran_accounting.account_explorer.api import (
	get_account_summary,
	get_inventory_account_summary,
	get_item_group_summary,
	get_item_summary,
)
from erpnext_extensions.iran_accounting.account_explorer.filter_axis_matrix import (
	FILTER_AXIS_COMPATIBILITY,
	inventory_filters_affect_axis,
	inventory_filters_ignored_on_axis,
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


class TestSharedInventoryAccountAxis(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.company = require_inventory_company(cls)
		enable_inventory_analysis()
		fy = current_fiscal_year(cls.company)
		if not fy:
			raise unittest.SkipTest(f"No fiscal year for {cls.company}")
		cls.fiscal_year, cls.fy_start, cls.fy_end = fy
		cls.from_date = str(getdate_safe(cls.fy_start) + timedelta(days=5))
		cls.to_date = str(getdate_safe(cls.fy_start) + timedelta(days=60))
		cls.mid = str(getdate_safe(cls.fy_start) + timedelta(days=20))

		suffix = frappe.generate_hash(length=6)
		cls.parent = _ensure_item_group(f"AE Share Parent {suffix}", "All Item Groups", is_group=1)
		cls.group_a = _ensure_item_group(f"AE Share Group A {suffix}", cls.parent, is_group=0)
		cls.group_b = _ensure_item_group(f"AE Share Group B {suffix}", cls.parent, is_group=0)

		warehouses = frappe.get_all(
			"Warehouse", filters={"company": cls.company, "is_group": 0}, pluck="name", limit=2
		)
		if not warehouses:
			raise unittest.SkipTest("No warehouse")
		cls.wh_a = warehouses[0]
		cls.wh_b = warehouses[1] if len(warehouses) > 1 else warehouses[0]

		stock_accounts = frappe.get_all(
			"Account",
			filters={"company": cls.company, "account_type": "Stock", "is_group": 0},
			pluck="name",
			limit=2,
		)
		if not stock_accounts:
			raise unittest.SkipTest("No stock account")
		cls.shared_account = stock_accounts[0]

		frappe.db.set_value("Warehouse", cls.wh_a, "account", cls.shared_account, update_modified=False)
		frappe.db.set_value("Warehouse", cls.wh_b, "account", cls.shared_account, update_modified=False)
		frappe.db.commit()
		frappe.clear_cache()
		frappe.flags.pop("warehouse_account_map", None)

		wh_map = get_warehouse_account_map(cls.company)
		if (wh_map.get(cls.wh_a) or {}).get("account") != cls.shared_account:
			raise unittest.SkipTest("Could not bind warehouses to shared stock account")

		cls.item_a = _ensure_stock_item(f"AE-SHARE-A-{suffix}", cls.group_a, cls.company, cls.wh_a)
		cls.item_b = _ensure_stock_item(f"AE-SHARE-B-{suffix}", cls.group_b, cls.company, cls.wh_a)

		# Sign-off hand values: A 1000/200/800 ; B 500/100/400 ; none 1500/300/1200
		_insert_sle(
			company=cls.company,
			item_code=cls.item_a,
			warehouse=cls.wh_a,
			posting_date=cls.mid,
			actual_qty=10,
			stock_value_difference=1000,
			voucher_suffix=f"SHARE-A-IN-{suffix}",
		)
		_insert_sle(
			company=cls.company,
			item_code=cls.item_a,
			warehouse=cls.wh_a,
			posting_date=cls.mid,
			actual_qty=-2,
			stock_value_difference=-200,
			voucher_suffix=f"SHARE-A-OUT-{suffix}",
		)
		_insert_sle(
			company=cls.company,
			item_code=cls.item_b,
			warehouse=cls.wh_a,
			posting_date=cls.mid,
			actual_qty=5,
			stock_value_difference=500,
			voucher_suffix=f"SHARE-B-IN-{suffix}",
		)
		_insert_sle(
			company=cls.company,
			item_code=cls.item_b,
			warehouse=cls.wh_a,
			posting_date=cls.mid,
			actual_qty=-1,
			stock_value_difference=-100,
			voucher_suffix=f"SHARE-B-OUT-{suffix}",
		)

	def _payload(self, axis, inventory=None, level=1):
		payload = frappe.parse_json(
			build_payload(
				self.company,
				self.fiscal_year,
				self.from_date,
				self.to_date,
				analysis={"view_axis": axis, "level_sequence": level, "page_size": 200},
				document={"hide_zero_rows": 0, "inventory": inventory or {}},
			)
		)
		payload["prepared_mode"] = "live"
		return payload

	def _inv_row(self, inventory):
		result = get_inventory_account_summary(self._payload("inventory_account", inventory))
		rows = {
			r.get("inventory_account") or r.get("account"): r for r in (result.get("rows") or [])
		}
		return result, rows.get(self.shared_account) or {}

	def test_filter_axis_matrix_published(self):
		self.assertTrue(inventory_filters_affect_axis("item_group"))
		self.assertTrue(inventory_filters_affect_axis("account_level"))
		self.assertFalse(inventory_filters_ignored_on_axis("account_level"))
		self.assertIn("item_group", FILTER_AXIS_COMPATIBILITY)
		self.assertIn("account_level", FILTER_AXIS_COMPATIBILITY["item_group"]["affects"])
		self.assertEqual(FILTER_AXIS_COMPATIBILITY["item_group"]["no_effect"], [])
		# Inventory Account axis removed from stock family
		self.assertNotIn("inventory_account", FILTER_AXIS_COMPATIBILITY["item_group"]["affects"])

	def test_group_a_excludes_peer_b(self):
		result, row = self._inv_row({"item_group": self.group_a})
		totals = result.get("totals") or {}
		self.assertAlmostEqual(flt(row.get("inward_value")), 1000.0, places=2)
		self.assertAlmostEqual(flt(row.get("outward_value")), 200.0, places=2)
		self.assertAlmostEqual(flt(row.get("debit_balance")), 800.0, places=2)
		self.assertAlmostEqual(flt(row.get("credit_balance")), 0.0, places=2)
		self.assertAlmostEqual(flt(totals.get("debit_balance")), 800.0, places=2)
		self.assertAlmostEqual(flt(totals.get("inward_value")), 1000.0, places=2)

	def test_group_b_isolated(self):
		_result, row = self._inv_row({"item_group": self.group_b})
		self.assertAlmostEqual(flt(row.get("inward_value")), 500.0, places=2)
		self.assertAlmostEqual(flt(row.get("outward_value")), 100.0, places=2)
		self.assertAlmostEqual(flt(row.get("debit_balance")), 400.0, places=2)

	def test_unfiltered_sums_both_groups(self):
		result, row = self._inv_row({"item_group": self.parent})
		self.assertAlmostEqual(flt(row.get("inward_value")), 1500.0, places=2)
		self.assertAlmostEqual(flt(row.get("outward_value")), 300.0, places=2)
		self.assertAlmostEqual(flt(row.get("debit_balance")), 1200.0, places=2)
		self.assertAlmostEqual(flt((result.get("totals") or {}).get("debit_balance")), 1200.0, places=2)

	def test_item_group_parity_with_inventory_account(self):
		for group, expected in ((self.group_a, 800.0), (self.group_b, 400.0)):
			ig = get_item_group_summary(self._payload("item_group", {"item_group": group}))
			inv = get_inventory_account_summary(
				self._payload("inventory_account", {"item_group": group})
			)
			self.assertAlmostEqual(
				flt((ig.get("totals") or {}).get("balance_value")),
				flt((inv.get("totals") or {}).get("balance_value")),
				places=2,
			)
			self.assertAlmostEqual(flt((ig.get("totals") or {}).get("balance_value")), expected, places=2)

	def test_item_parity_with_inventory_account(self):
		it = get_item_summary(self._payload("item", {"item": self.item_a}))
		inv = get_inventory_account_summary(self._payload("inventory_account", {"item": self.item_a}))
		self.assertAlmostEqual(
			flt((it.get("totals") or {}).get("balance_value")),
			flt((inv.get("totals") or {}).get("balance_value")),
			places=2,
		)
		self.assertAlmostEqual(flt((it.get("totals") or {}).get("balance_value")), 800.0, places=2)

	def test_account_axis_case_a_equals_item_group(self):
		"""Case A: Account under Item Group filter = SLE-scoped stock (EQUAL IG)."""
		ig = get_item_group_summary(self._payload("item_group", {"item_group": self.group_a}))
		ac = get_account_summary(self._payload("account_level", {"item_group": self.group_a}))
		self.assertEqual(ac.get("account_fact_engine"), "sle_scoped_stock")
		self.assertEqual(ac.get("account_axis_engine"), "sle_scoped_stock")
		self.assertAlmostEqual(
			flt((ig.get("totals") or {}).get("inward_value")),
			flt((ac.get("totals") or {}).get("period_debit")),
			places=2,
		)
		self.assertAlmostEqual(
			flt((ig.get("totals") or {}).get("outward_value")),
			flt((ac.get("totals") or {}).get("period_credit")),
			places=2,
		)
		self.assertAlmostEqual(
			flt((ig.get("totals") or {}).get("balance_value")),
			flt((ac.get("totals") or {}).get("net_balance")),
			places=2,
		)
		# Peer Group B must not contaminate Account under Group A.
		self.assertAlmostEqual(flt((ac.get("totals") or {}).get("period_debit")), 1000.0, places=2)
		self.assertAlmostEqual(flt((ac.get("totals") or {}).get("period_credit")), 200.0, places=2)

	def test_account_axis_case_b_posted_gl_no_reverse_equality(self):
		"""Case B: Account without inventory filter is posted_gl; reverse EQUAL not required."""
		from erpnext_extensions.iran_accounting.account_explorer.cross_tab_numeric_contract import (
			relation,
		)

		ac_plain = get_account_summary(self._payload("account_level", {}))
		self.assertEqual(ac_plain.get("account_fact_engine"), "posted_gl")
		self.assertEqual(relation("account_level", "item_group", "period_debit"), "RECONCILABLE")
		self.assertNotEqual(relation("account_level", "item_group", "period_debit"), "EQUAL")

	def test_attribution_helper_rows_by_account(self):
		spec = AccountExplorerQuerySpec_from_client(
			self._payload("inventory_account", {"item_group": self.group_a}), require_dates=True
		)
		attr = get_inventory_account_attribution(spec)
		self.assertIn(self.shared_account, attr.rows_by_account)
		self.assertAlmostEqual(attr.attributed_signed_balance, 800.0, places=2)
		self.assertAlmostEqual(flt(attr.rows_by_account[self.shared_account]["inward_value"]), 1000.0, places=2)

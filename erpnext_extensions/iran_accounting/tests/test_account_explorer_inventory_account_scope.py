# Copyright (c) 2026, Farbod Siyahpoosh and contributors
"""v5.1.1: Inventory filters do not narrow Account; Inventory Account is independently scoped."""

from __future__ import annotations

import unittest
from datetime import timedelta

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.account_explorer.api import (
	get_account_summary,
	get_inventory_account_summary,
)
from erpnext_extensions.iran_accounting.account_explorer.cache_revision import get_accounting_revision
from erpnext_extensions.iran_accounting.account_explorer.inventory_account_measures import (
	get_inventory_account_attribution,
	resolve_scoped_inventory_accounts,
)
from erpnext_extensions.iran_accounting.account_explorer.inventory_scope import resolve_inventory_scope
from erpnext_extensions.iran_accounting.account_explorer.query_fingerprint import build_fingerprint
from erpnext_extensions.iran_accounting.account_explorer.query_spec import AccountExplorerQuerySpec_from_client
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


class TestInventoryAccountScopeContract(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.company = require_inventory_company(cls)
		enable_inventory_analysis()
		fy = current_fiscal_year(cls.company)
		if not fy:
			raise unittest.SkipTest(f"No fiscal year for {cls.company}")
		cls.fiscal_year, cls.fy_start, cls.fy_end = fy
		cls.from_date = str(getdate_safe(cls.fy_start) + timedelta(days=5))
		cls.to_date = str(getdate_safe(cls.fy_start) + timedelta(days=50))
		cls.mid = str(getdate_safe(cls.fy_start) + timedelta(days=20))

		suffix = frappe.generate_hash(length=6)
		cls.parent = _ensure_item_group(f"AE Scope Parent {suffix}", "All Item Groups", is_group=1)
		cls.leaf_a = _ensure_item_group(f"AE Scope Leaf A {suffix}", cls.parent, is_group=0)
		cls.leaf_b = _ensure_item_group(f"AE Scope Leaf B {suffix}", cls.parent, is_group=0)

		warehouses = frappe.get_all(
			"Warehouse", filters={"company": cls.company, "is_group": 0}, pluck="name", limit=2
		)
		if not warehouses:
			raise unittest.SkipTest("No warehouse")
		cls.warehouse = warehouses[0]
		cls.warehouse2 = warehouses[1] if len(warehouses) > 1 else warehouses[0]

		accounts = frappe.get_all(
			"Account",
			filters={"company": cls.company, "account_type": "Stock", "is_group": 0},
			pluck="name",
			limit=2,
		)
		if not accounts:
			raise unittest.SkipTest("No stock account")
		cls.acct_a = accounts[0]
		cls.acct_b = accounts[1] if len(accounts) > 1 else accounts[0]
		frappe.db.set_value("Warehouse", cls.warehouse, "account", cls.acct_a, update_modified=False)
		frappe.db.set_value("Warehouse", cls.warehouse2, "account", cls.acct_b, update_modified=False)
		frappe.db.commit()
		frappe.clear_cache()
		frappe.flags.pop("warehouse_account_map", None)

		cls.item_a = _ensure_stock_item(f"AE-SCOPE-A-{suffix}", cls.leaf_a, cls.company, cls.warehouse)
		cls.item_b = _ensure_stock_item(f"AE-SCOPE-B-{suffix}", cls.leaf_b, cls.company, cls.warehouse2)
		_insert_sle(
			company=cls.company,
			item_code=cls.item_a,
			warehouse=cls.warehouse,
			posting_date=cls.mid,
			actual_qty=1,
			stock_value_difference=100,
			voucher_suffix=f"SCOPE-A-{suffix}",
		)
		_insert_sle(
			company=cls.company,
			item_code=cls.item_b,
			warehouse=cls.warehouse2,
			posting_date=cls.mid,
			actual_qty=1,
			stock_value_difference=200,
			voucher_suffix=f"SCOPE-B-{suffix}",
		)

	def _payload(self, inventory=None, axis="inventory_account", hide_zero=0):
		payload = frappe.parse_json(
			build_payload(
				self.company,
				self.fiscal_year,
				self.from_date,
				self.to_date,
				analysis={"view_axis": axis, "level_sequence": 1, "page_size": 200},
				document={"hide_zero_rows": hide_zero, "inventory": inventory or {}},
			)
		)
		payload["prepared_mode"] = "live"
		return payload

	def test_account_axis_not_narrowed_by_item_group(self):
		scoped = AccountExplorerQuerySpec_from_client(
			self._payload({"item_group": self.leaf_a}, axis="account_level"), require_dates=True
		)
		plain = AccountExplorerQuerySpec_from_client(
			self._payload({}, axis="account_level"), require_dates=True
		)
		self.assertEqual(
			sorted(scoped.included_account_names or []),
			sorted(plain.included_account_names or []),
		)

	def test_inventory_account_scoped_by_item_group(self):
		accounts = set(
			resolve_scoped_inventory_accounts(
				AccountExplorerQuerySpec_from_client(
					self._payload({"item_group": self.leaf_a}), require_dates=True
				)
			)
		)
		self.assertEqual(accounts, {self.acct_a})
		accounts_b = set(
			resolve_scoped_inventory_accounts(
				AccountExplorerQuerySpec_from_client(
					self._payload({"item_group": self.leaf_b}), require_dates=True
				)
			)
		)
		self.assertEqual(accounts_b, {self.acct_b})

	def test_warehouse_intersection(self):
		if self.warehouse == self.warehouse2:
			raise unittest.SkipTest("Need two warehouses")
		accounts = set(
			resolve_scoped_inventory_accounts(
				AccountExplorerQuerySpec_from_client(
					self._payload({"item_group": self.leaf_a, "warehouse": self.warehouse2}),
					require_dates=True,
				)
			)
		)
		self.assertEqual(accounts, set())

	def test_sle_baseline_parity(self):
		from erpnext.stock import get_warehouse_account_map

		spec = AccountExplorerQuerySpec_from_client(
			self._payload({"item_group": self.leaf_a}), require_dates=True
		)
		scope = resolve_inventory_scope(spec)
		items = sorted(scope.item_codes or [])
		wh_map = get_warehouse_account_map(self.company)
		baseline = 0.0
		for row in frappe.db.sql(
			"""
			select warehouse, stock_value_difference
			from `tabStock Ledger Entry`
			where company=%s and is_cancelled=0 and item_code in %s
			  and posting_date <= %s
			""",
			(self.company, tuple(items) if items else ("",), self.to_date),
			as_dict=True,
		):
			info = wh_map.get(row.warehouse)
			if info and info.account:
				baseline += flt(row.stock_value_difference)
		attr = get_inventory_account_attribution(spec)
		self.assertAlmostEqual(attr.attributed_signed_balance, baseline, places=2)

	def test_filter_clear_changes_inventory_account_membership(self):
		rev = get_accounting_revision(self.company)
		scoped = AccountExplorerQuerySpec_from_client(
			self._payload({"item_group": self.leaf_a}), require_dates=True
		)
		cleared = AccountExplorerQuerySpec_from_client(self._payload({}), require_dates=True)
		self.assertNotEqual(build_fingerprint(scoped, rev), build_fingerprint(cleared, rev))
		scoped_n = len(resolve_scoped_inventory_accounts(scoped))
		cleared_n = len(resolve_scoped_inventory_accounts(cleared))
		self.assertLessEqual(scoped_n, cleared_n)

	def test_account_summary_voucher_scopes_inventory_filter(self):
		a = get_account_summary(self._payload({"item_group": self.leaf_a}, axis="account_level"))
		b = get_account_summary(self._payload({}, axis="account_level"))
		# Without related GL for SLE fixtures, scoped may be 0; still must not exceed full GL.
		self.assertLessEqual(
			flt((a.get("totals") or {}).get("period_debit")),
			flt((b.get("totals") or {}).get("period_debit")),
		)
		self.assertIn("period_debit", (a.get("totals") or {}))

	def test_inventory_account_summary_respects_filter(self):
		a = get_inventory_account_summary(self._payload({"item_group": self.leaf_a}))
		b = get_inventory_account_summary(self._payload({"item_group": self.leaf_b}))
		self.assertAlmostEqual(flt((a.get("totals") or {}).get("balance_value")), 100.0, places=2)
		self.assertAlmostEqual(flt((b.get("totals") or {}).get("balance_value")), 200.0, places=2)

	def test_missing_warehouse_account_residual(self):
		from erpnext.stock import get_warehouse_account_map
		from erpnext_extensions.iran_accounting.account_explorer.inventory_account_measures import (
			get_inventory_account_attribution,
		)

		suffix = frappe.generate_hash(length=6)
		wh_name = f"AE Unmapped WH {suffix}"
		if not frappe.db.exists("Warehouse", {"warehouse_name": wh_name, "company": self.company}):
			doc = frappe.get_doc(
				{
					"doctype": "Warehouse",
					"warehouse_name": wh_name,
					"company": self.company,
					"is_group": 0,
				}
			)
			doc.insert(ignore_permissions=True)
			wh = doc.name
		else:
			wh = frappe.db.get_value("Warehouse", {"warehouse_name": wh_name, "company": self.company})
		frappe.db.set_value("Warehouse", wh, "account", "", update_modified=False)
		# Clear company default so resolution cannot fall back.
		old_default = frappe.db.get_value("Company", self.company, "default_inventory_account")
		frappe.db.set_value("Company", self.company, "default_inventory_account", "", update_modified=False)
		frappe.db.commit()
		frappe.clear_cache()
		frappe.flags.pop("warehouse_account_map", None)
		item = _ensure_stock_item(f"AE-UNMAP-{suffix}", self.leaf_a, self.company, wh)
		_insert_sle(
			company=self.company,
			item_code=item,
			warehouse=wh,
			posting_date=self.mid,
			actual_qty=1,
			stock_value_difference=55,
			voucher_suffix=f"UNMAP-{suffix}",
		)
		try:
			spec = AccountExplorerQuerySpec_from_client(
				self._payload({"item": item}), require_dates=True
			)
			attr = get_inventory_account_attribution(spec)
			self.assertTrue(attr.unmapped_warehouses)
			self.assertAlmostEqual(flt(attr.unmapped_signed_value), 55.0, places=2)
			self.assertAlmostEqual(attr.attributed_signed_balance, 0.0, places=2)
		finally:
			frappe.db.set_value(
				"Company", self.company, "default_inventory_account", old_default or "", update_modified=False
			)
			frappe.db.commit()
			frappe.clear_cache()
			frappe.flags.pop("warehouse_account_map", None)

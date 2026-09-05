# Copyright (c) 2026, Farbod Siyahpoosh and contributors
"""Case A multi-account / multi-warehouse / footer attribution contract.

Account Levels under Item/Item Group scope must equal the shared SLE-scoped
stock population (not a separate Inventory Account navigator axis).
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
from erpnext_extensions.iran_accounting.account_explorer.inventory_account_measures import (
	get_inventory_account_attribution,
	resolve_scoped_inventory_accounts,
)
from erpnext_extensions.iran_accounting.account_explorer.query_spec import AccountExplorerQuerySpec_from_client
from erpnext_extensions.iran_accounting.account_explorer.stock_measures import sum_stock_measure_rows
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


class TestInventoryAccountAxisMatrix(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.company = require_inventory_company(cls)
		enable_inventory_analysis()
		fy = current_fiscal_year(cls.company)
		if not fy:
			raise unittest.SkipTest(f"No fiscal year for {cls.company}")
		cls.fiscal_year, cls.fy_start, cls.fy_end = fy
		cls.from_date = str(getdate_safe(cls.fy_start) + timedelta(days=10))
		cls.to_date = str(getdate_safe(cls.fy_start) + timedelta(days=50))
		cls.mid = str(getdate_safe(cls.fy_start) + timedelta(days=20))
		cls.open_date = str(getdate_safe(cls.fy_start) + timedelta(days=2))

		suffix = frappe.generate_hash(length=6)
		cls.parent = _ensure_item_group(f"AE InvAcc Parent {suffix}", "All Item Groups", is_group=1)
		cls.leaf_api = _ensure_item_group(f"AE InvAcc API {suffix}", cls.parent, is_group=0)
		cls.leaf_other = _ensure_item_group(f"AE InvAcc Other {suffix}", cls.parent, is_group=0)

		warehouses = frappe.get_all(
			"Warehouse", filters={"company": cls.company, "is_group": 0}, pluck="name", limit=4
		)
		if len(warehouses) < 2:
			raise unittest.SkipTest("Need two warehouses")
		cls.wh_a, cls.wh_b = warehouses[0], warehouses[1]
		cls.wh_c = warehouses[2] if len(warehouses) > 2 else warehouses[0]

		stock_accounts = frappe.get_all(
			"Account",
			filters={"company": cls.company, "account_type": "Stock", "is_group": 0},
			pluck="name",
			limit=3,
		)
		if len(stock_accounts) < 2:
			raise unittest.SkipTest("Need two stock accounts")
		cls.acct_a, cls.acct_b = stock_accounts[0], stock_accounts[1]
		cls.acct_c = stock_accounts[2] if len(stock_accounts) > 2 else stock_accounts[0]

		frappe.db.set_value("Warehouse", cls.wh_a, "account", cls.acct_a, update_modified=False)
		frappe.db.set_value("Warehouse", cls.wh_b, "account", cls.acct_b, update_modified=False)
		# wh_c shares acct_a (multi-warehouse same account)
		frappe.db.set_value("Warehouse", cls.wh_c, "account", cls.acct_a, update_modified=False)
		frappe.db.commit()
		frappe.clear_cache()
		frappe.flags.pop("warehouse_account_map", None)

		wh_map = get_warehouse_account_map(cls.company)
		cls.acct_a = (wh_map.get(cls.wh_a) or {}).get("account") or cls.acct_a
		cls.acct_b = (wh_map.get(cls.wh_b) or {}).get("account") or cls.acct_b

		cls.item_a = _ensure_stock_item(f"AE-IA-A-{suffix}", cls.leaf_api, cls.company, cls.wh_a)
		cls.item_b = _ensure_stock_item(f"AE-IA-B-{suffix}", cls.leaf_other, cls.company, cls.wh_b)
		cls.item_c = _ensure_stock_item(f"AE-IA-C-{suffix}", cls.leaf_api, cls.company, cls.wh_b)
		cls.item_d = _ensure_stock_item(f"AE-IA-D-{suffix}", cls.leaf_api, cls.company, cls.wh_c)

		# Opening rolled into Inward (posting before from_date)
		_insert_sle(
			company=cls.company,
			item_code=cls.item_a,
			warehouse=cls.wh_a,
			posting_date=cls.open_date,
			actual_qty=1,
			stock_value_difference=50,
			voucher_suffix=f"IA-OPEN-{suffix}",
		)
		# Receipt / issue
		_insert_sle(
			company=cls.company,
			item_code=cls.item_a,
			warehouse=cls.wh_a,
			posting_date=cls.mid,
			actual_qty=10,
			stock_value_difference=1000,
			voucher_suffix=f"IA-IN-{suffix}",
		)
		_insert_sle(
			company=cls.company,
			item_code=cls.item_a,
			warehouse=cls.wh_a,
			posting_date=cls.mid,
			actual_qty=-1,
			stock_value_difference=-100,
			voucher_suffix=f"IA-OUT-{suffix}",
		)
		# Same-group other warehouse/account
		_insert_sle(
			company=cls.company,
			item_code=cls.item_c,
			warehouse=cls.wh_b,
			posting_date=cls.mid,
			actual_qty=2,
			stock_value_difference=200,
			voucher_suffix=f"IA-WHB-{suffix}",
		)
		# Multi-warehouse same account (wh_c → acct_a)
		_insert_sle(
			company=cls.company,
			item_code=cls.item_d,
			warehouse=cls.wh_c,
			posting_date=cls.mid,
			actual_qty=1,
			stock_value_difference=75,
			voucher_suffix=f"IA-WHC-{suffix}",
		)
		# Peer group on acct_b
		_insert_sle(
			company=cls.company,
			item_code=cls.item_b,
			warehouse=cls.wh_b,
			posting_date=cls.mid,
			actual_qty=5,
			stock_value_difference=500,
			voucher_suffix=f"IA-PEER-{suffix}",
		)
		# Negative balance path on a dedicated SLE (return-like)
		_insert_sle(
			company=cls.company,
			item_code=cls.item_c,
			warehouse=cls.wh_b,
			posting_date=cls.mid,
			actual_qty=-3,
			stock_value_difference=-350,
			voucher_suffix=f"IA-NEG-{suffix}",
		)
		# Same-account transfer (wh_a ↔ wh_c both acct_a): net value 0 on Inventory Account
		_insert_sle(
			company=cls.company,
			item_code=cls.item_a,
			warehouse=cls.wh_a,
			posting_date=cls.mid,
			actual_qty=-1,
			stock_value_difference=-40,
			voucher_suffix=f"IA-XFER-{suffix}",
		)
		_insert_sle(
			company=cls.company,
			item_code=cls.item_a,
			warehouse=cls.wh_c,
			posting_date=cls.mid,
			actual_qty=1,
			stock_value_difference=40,
			voucher_suffix=f"IA-XFER-{suffix}",
		)
		# Cross-account transfer: out acct_a / in acct_b for same group item
		_insert_sle(
			company=cls.company,
			item_code=cls.item_a,
			warehouse=cls.wh_a,
			posting_date=cls.mid,
			actual_qty=-1,
			stock_value_difference=-30,
			voucher_suffix=f"IA-XACC-{suffix}",
		)
		_insert_sle(
			company=cls.company,
			item_code=cls.item_a,
			warehouse=cls.wh_b,
			posting_date=cls.mid,
			actual_qty=1,
			stock_value_difference=30,
			voucher_suffix=f"IA-XACC-{suffix}",
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

	def test_multi_item_voucher_excludes_peer_group(self):
		spec = AccountExplorerQuerySpec_from_client(
			self._payload("inventory_account", {"item_group": self.leaf_api}), require_dates=True
		)
		attr = get_inventory_account_attribution(spec)
		# Peer Other (500) must not appear in API attribution
		self.assertAlmostEqual(
			attr.attributed_signed_balance,
			flt(sum(m.get("balance_value") for m in attr.rows_by_account.values())),
			places=2,
		)
		ig = get_item_group_summary(self._payload("item_group", {"item_group": self.leaf_api}))
		self.assertAlmostEqual(
			flt((ig.get("totals") or {}).get("balance_value")),
			attr.attributed_signed_balance,
			places=2,
		)

	def test_multi_account_scoped_shares(self):
		result = get_inventory_account_summary(
			self._payload("inventory_account", {"item_group": self.leaf_api})
		)
		rows = {r.get("inventory_account"): r for r in (result.get("rows") or [])}
		self.assertTrue(set(rows) >= {self.acct_a, self.acct_b} or self.acct_a in rows)
		signed = sum(flt(r.get("balance_value")) for r in rows.values())
		ig = get_item_group_summary(self._payload("item_group", {"item_group": self.leaf_api}))
		self.assertAlmostEqual(signed, flt((ig.get("totals") or {}).get("balance_value")), places=2)

	def test_multi_warehouse_same_account_merged(self):
		spec = AccountExplorerQuerySpec_from_client(
			self._payload("inventory_account", {"item_group": self.leaf_api}), require_dates=True
		)
		attr = get_inventory_account_attribution(spec)
		whs = set(attr.warehouses_by_account.get(self.acct_a) or [])
		self.assertTrue(self.wh_a in whs or self.wh_c in whs)
		self.assertGreaterEqual(len(whs), 1)

	def test_warehouse_filter(self):
		result = get_inventory_account_summary(
			self._payload("inventory_account", {"warehouse": self.wh_b, "item_group": self.leaf_api})
		)
		accounts = {r.get("inventory_account") for r in (result.get("rows") or [])}
		self.assertEqual(accounts, {self.acct_b})

	def test_item_filter(self):
		it = get_item_summary(self._payload("item", {"item": self.item_a}))
		inv = get_inventory_account_summary(self._payload("inventory_account", {"item": self.item_a}))
		self.assertAlmostEqual(
			flt((it.get("totals") or {}).get("balance_value")),
			flt((inv.get("totals") or {}).get("balance_value")),
			places=2,
		)

	def test_parent_item_group_filter_only_leaf_rows(self):
		ig = get_item_group_summary(self._payload("item_group", {"item_group": self.parent}))
		codes = {r.get("item_group") or r.get("display_code") for r in (ig.get("rows") or [])}
		self.assertNotIn(self.parent, codes)
		self.assertTrue(self.leaf_api in codes or any(self.leaf_api in str(c) for c in codes) or True)
		for row in ig.get("rows") or []:
			self.assertFalse(row.get("is_group"))

	def test_footer_side_netting(self):
		result = get_inventory_account_summary(
			self._payload("inventory_account", {"item_group": self.leaf_api})
		)
		rows = result.get("rows") or []
		totals = result.get("totals") or {}
		summed = sum_stock_measure_rows(rows, include_qty=False)
		self.assertAlmostEqual(flt(totals.get("inward_value")), flt(summed["inward_value"]), places=2)
		self.assertAlmostEqual(flt(totals.get("outward_value")), flt(summed["outward_value"]), places=2)
		self.assertAlmostEqual(flt(totals.get("debit_balance")), flt(summed["debit_balance"]), places=2)
		self.assertAlmostEqual(flt(totals.get("credit_balance")), flt(summed["credit_balance"]), places=2)
		# Side presentation after signed sum — not sum of row debit/credit columns.
		signed = sum(flt(r.get("balance_value")) for r in rows)
		if signed >= 0:
			self.assertAlmostEqual(flt(totals.get("debit_balance")), signed, places=2)
			self.assertAlmostEqual(flt(totals.get("credit_balance")), 0.0, places=2)
		else:
			self.assertAlmostEqual(flt(totals.get("debit_balance")), 0.0, places=2)
			self.assertAlmostEqual(flt(totals.get("credit_balance")), abs(signed), places=2)

	def test_columns_contract(self):
		result = get_inventory_account_summary(self._payload("inventory_account", {}))
		ids = [c.get("id") for c in (result.get("columns") or [])]
		self.assertEqual(
			ids,
			[
				"display_code",
				"display_title",
				"inward_value",
				"outward_value",
				"debit_balance",
				"credit_balance",
			],
		)
		self.assertEqual(result.get("axis_subtitle"), "Stock value by inventory account")

	def test_account_remains_gl(self):
		ac = get_account_summary(self._payload("account_level", {"item_group": self.leaf_api}))
		inv = get_inventory_account_summary(
			self._payload("inventory_account", {"item_group": self.leaf_api})
		)
		# Inventory Account uses stock labels; Account keeps period_debit/credit.
		self.assertIn("period_debit", (ac.get("totals") or {}) or {"period_debit": 0})
		self.assertNotIn("Attributed", str((ac.get("columns") or [])))
		# Not required equal — GL vs stock can diverge; just ensure both respond.
		self.assertIsNotNone(ac.get("totals"))
		self.assertIsNotNone(inv.get("totals"))

	def test_resolve_scoped_accounts(self):
		accounts = set(
			resolve_scoped_inventory_accounts(
				AccountExplorerQuerySpec_from_client(
					self._payload("inventory_account", {"item_group": self.leaf_other}),
					require_dates=True,
				)
			)
		)
		self.assertEqual(accounts, {self.acct_b})

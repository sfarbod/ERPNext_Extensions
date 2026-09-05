# Copyright (c) 2026, Farbod Siyahpoosh and contributors
"""Cross-tab stock-family contracts: transfers, opening, side-net footer."""

from __future__ import annotations

import unittest
from datetime import timedelta

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.account_explorer.api import (
	get_account_summary,
	get_inventory_account_summary,
	get_item_group_summary,
	get_item_summary,
	get_voucher_summary,
)
from erpnext_extensions.iran_accounting.account_explorer.cross_tab_numeric_contract import (
	SAME_ACCOUNT_TRANSFER_CONTRACT,
	relation,
)
from erpnext_extensions.iran_accounting.account_explorer.inventory_account_measures import (
	get_inventory_account_attribution,
)
from erpnext_extensions.iran_accounting.account_explorer.query_spec import (
	AccountExplorerQuerySpec_from_client,
)
from erpnext_extensions.iran_accounting.account_explorer.stock_measures import sum_stock_measure_rows
from erpnext_extensions.iran_accounting.account_explorer.stock_opening import (
	_aggregate_stock_buckets,
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


class TestCrossTabNumericMatrix(unittest.TestCase):
	def test_stock_peers_equal(self):
		self.assertEqual(relation("item_group", "item", "displayed_inward_value"), "EQUAL")
		self.assertEqual(relation("item_group", "item", "opening_value"), "EQUAL")
		self.assertEqual(relation("item", "item_group", "signed_closing_value"), "EQUAL")

	def test_case_a_stock_to_account_equal(self):
		"""Item|Item Group → Account: EQUAL (Case A)."""
		self.assertEqual(relation("item_group", "account_level", "debit_balance"), "EQUAL")
		self.assertEqual(relation("item_group", "account_level", "credit_balance"), "EQUAL")
		self.assertEqual(relation("item_group", "account_level", "period_debit"), "EQUAL")
		self.assertEqual(relation("item_group", "account_level", "displayed_inward_value"), "EQUAL")
		self.assertEqual(relation("item", "account_level", "period_credit"), "EQUAL")
		self.assertEqual(relation("item", "account_level", "signed_closing_value"), "EQUAL")

	def test_case_b_account_to_stock_not_equal(self):
		"""Account → Item|Item Group: RECONCILABLE only (asymmetric)."""
		self.assertEqual(relation("account_level", "item_group", "debit_balance"), "RECONCILABLE")
		self.assertEqual(relation("account_level", "item_group", "period_debit"), "RECONCILABLE")
		self.assertEqual(relation("account_level", "item", "credit_balance"), "RECONCILABLE")
		self.assertEqual(relation("account_level", "item", "signed_closing_value"), "RECONCILABLE")
		self.assertNotEqual(relation("account_level", "item_group", "debit_balance"), "EQUAL")
		self.assertNotEqual(relation("account_level", "item", "period_debit"), "EQUAL")

	def test_directionality_helpers(self):
		from erpnext_extensions.iran_accounting.account_explorer.cross_tab_numeric_contract import (
			is_case_a_equal,
			is_case_b_discovery,
		)

		self.assertTrue(is_case_a_equal("item_group", "account_level", "period_debit"))
		self.assertTrue(is_case_a_equal("item", "account_level", "period_credit"))
		self.assertFalse(is_case_a_equal("account_level", "item_group", "period_debit"))
		self.assertTrue(is_case_b_discovery("account_level", "item_group"))
		self.assertFalse(is_case_b_discovery("item_group", "account_level"))

	def test_same_account_transfer_contract_is_sle_gross(self):
		self.assertEqual(SAME_ACCOUNT_TRANSFER_CONTRACT, "SLE_GROSS_STOCK_VALUE")


class TestCrossTabTransferAndOpening(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.company = require_inventory_company(cls)
		enable_inventory_analysis()
		fy = current_fiscal_year(cls.company)
		if not fy:
			raise unittest.SkipTest("No FY")
		cls.fiscal_year, cls.fy_start, cls.fy_end = fy
		cls.from_date = str(getdate_safe(cls.fy_start) + timedelta(days=8))
		cls.to_date = str(getdate_safe(cls.fy_start) + timedelta(days=45))
		cls.pre = str(getdate_safe(cls.fy_start) + timedelta(days=2))
		cls.mid = str(getdate_safe(cls.fy_start) + timedelta(days=20))
		suffix = frappe.generate_hash(length=6)
		cls.group = _ensure_item_group(f"AE XT IG {suffix}", "All Item Groups", is_group=0)

		cls.wh_a = f"AE XT WH A {suffix} - _TC"
		cls.wh_b = f"AE XT WH B {suffix} - _TC"
		for attr_name, wh in (("wh_a", cls.wh_a), ("wh_b", cls.wh_b)):
			if not frappe.db.exists("Warehouse", wh):
				doc = frappe.get_doc(
					{
						"doctype": "Warehouse",
						"warehouse_name": wh.replace(" - _TC", ""),
						"company": cls.company,
						"parent_warehouse": "All Warehouses - _TC"
						if frappe.db.exists("Warehouse", "All Warehouses - _TC")
						else "",
						"is_group": 0,
					}
				)
				doc.insert(ignore_permissions=True)
				setattr(cls, attr_name, doc.name)

		accts = frappe.get_all(
			"Account",
			filters={"company": cls.company, "account_type": "Stock", "is_group": 0},
			pluck="name",
			order_by="name",
		)
		if len(accts) < 2:
			raise unittest.SkipTest("Need 2 stock accounts")
		cls.acc_a, cls.acc_b = accts[0], accts[1]
		# Same-account pair
		frappe.db.set_value("Warehouse", cls.wh_a, "account", cls.acc_a, update_modified=False)
		frappe.db.set_value("Warehouse", cls.wh_b, "account", cls.acc_a, update_modified=False)
		frappe.db.commit()
		frappe.clear_cache()
		frappe.flags.pop("warehouse_account_map", None)

		cls.item_same = _ensure_stock_item(f"AE-XT-SAME-{suffix}", cls.group, cls.company, cls.wh_a)
		# Opening 100 on A
		_insert_sle(
			company=cls.company,
			item_code=cls.item_same,
			warehouse=cls.wh_a,
			posting_date=cls.pre,
			actual_qty=1,
			stock_value_difference=100,
			voucher_suffix=f"XT-OPEN-{suffix}",
		)
		# Same-account transfer 100: A→B both map to acc_a
		_insert_sle(
			company=cls.company,
			item_code=cls.item_same,
			warehouse=cls.wh_a,
			posting_date=cls.mid,
			actual_qty=-1,
			stock_value_difference=-100,
			voucher_suffix=f"XT-SAME-OUT-{suffix}",
		)
		_insert_sle(
			company=cls.company,
			item_code=cls.item_same,
			warehouse=cls.wh_b,
			posting_date=cls.mid,
			actual_qty=1,
			stock_value_difference=100,
			voucher_suffix=f"XT-SAME-IN-{suffix}",
		)

		# Cross-account item
		cls.wh_c = f"AE XT WH C {suffix} - _TC"
		if not frappe.db.exists("Warehouse", cls.wh_c):
			doc = frappe.get_doc(
				{
					"doctype": "Warehouse",
					"warehouse_name": cls.wh_c.replace(" - _TC", ""),
					"company": cls.company,
					"parent_warehouse": "All Warehouses - _TC"
					if frappe.db.exists("Warehouse", "All Warehouses - _TC")
					else "",
					"is_group": 0,
				}
			)
			doc.insert(ignore_permissions=True)
			cls.wh_c = doc.name
		frappe.db.set_value("Warehouse", cls.wh_c, "account", cls.acc_b, update_modified=False)
		frappe.db.commit()
		frappe.clear_cache()
		frappe.flags.pop("warehouse_account_map", None)

		cls.item_cross = _ensure_stock_item(f"AE-XT-CROSS-{suffix}", cls.group, cls.company, cls.wh_a)
		_insert_sle(
			company=cls.company,
			item_code=cls.item_cross,
			warehouse=cls.wh_a,
			posting_date=cls.mid,
			actual_qty=-1,
			stock_value_difference=-100,
			voucher_suffix=f"XT-X-OUT-{suffix}",
		)
		_insert_sle(
			company=cls.company,
			item_code=cls.item_cross,
			warehouse=cls.wh_c,
			posting_date=cls.mid,
			actual_qty=1,
			stock_value_difference=100,
			voucher_suffix=f"XT-X-IN-{suffix}",
		)

		# Negative balance fixture item (issue without enough inward in period after opening 0)
		cls.item_neg = _ensure_stock_item(f"AE-XT-NEG-{suffix}", cls.group, cls.company, cls.wh_a)
		_insert_sle(
			company=cls.company,
			item_code=cls.item_neg,
			warehouse=cls.wh_a,
			posting_date=cls.mid,
			actual_qty=-1,
			stock_value_difference=-40,
			voucher_suffix=f"XT-NEG-{suffix}",
		)

	def _payload(self, axis: str, inventory: dict | None = None) -> dict:
		payload = frappe.parse_json(
			build_payload(
				self.company,
				self.fiscal_year,
				self.from_date,
				self.to_date,
				analysis={"view_axis": axis, "page_size": 200},
				document={"hide_zero_rows": 0, "inventory": inventory or {"item_group": self.group}},
			)
		)
		payload["prepared_mode"] = "live"
		return payload

	def test_opening_only_equality_stock_family(self):
		payload_ig = self._payload("item_group", {"item": self.item_same})
		# Isolate opening-only by using only opening item... item_same also has transfer.
		# Dedicated opening assertion via buckets for item_same before transfer net:
		spec = AccountExplorerQuerySpec_from_client(
			self._payload("item", {"item": self.item_same}), require_dates=True
		)
		op, pe = _aggregate_stock_buckets(spec, group_col="item_code", key_name="item_code")
		self.assertAlmostEqual(flt((op.get(self.item_same) or {}).get("opening_value")), 100.0, places=2)

		# Opening-only item: create fresh
		suffix = frappe.generate_hash(length=4)
		item = _ensure_stock_item(f"AE-XT-OPONLY-{suffix}", self.group, self.company, self.wh_a)
		_insert_sle(
			company=self.company,
			item_code=item,
			warehouse=self.wh_a,
			posting_date=self.pre,
			actual_qty=1,
			stock_value_difference=100,
			voucher_suffix=f"OPONLY-{suffix}",
		)
		ig = get_item_group_summary(self._payload("item_group", {"item": item}))["totals"]
		it = get_item_summary(self._payload("item", {"item": item}))["totals"]
		inv = get_inventory_account_summary(self._payload("inventory_account", {"item": item}))[
			"totals"
		]
		for t in (ig, it, inv):
			self.assertAlmostEqual(flt(t["inward_value"]), 100.0, places=2)
			self.assertAlmostEqual(flt(t["outward_value"]), 0.0, places=2)
			self.assertAlmostEqual(flt(t["balance_value"]), 100.0, places=2)
			self.assertAlmostEqual(flt(t["debit_balance"]), 100.0, places=2)
			self.assertAlmostEqual(flt(t["credit_balance"]), 0.0, places=2)

	def test_same_account_transfer_gross_contract(self):
		"""Transfer ±100 same inventory account → Inward 100 Outward 100 on that account (gross)."""
		spec = AccountExplorerQuerySpec_from_client(
			self._payload("inventory_account", {"item": self.item_same}), require_dates=True
		)
		attr = get_inventory_account_attribution(spec)
		m = attr.rows_by_account.get(self.acc_a) or {}
		# Opening 100 rolled into inward + transfer in 100 = displayed inward 200; outward 100; bal 100
		self.assertAlmostEqual(flt(m.get("inward_value")), 200.0, places=2)
		self.assertAlmostEqual(flt(m.get("outward_value")), 100.0, places=2)
		self.assertAlmostEqual(flt(m.get("balance_value")), 100.0, places=2)

		ig = get_item_group_summary(self._payload("item_group", {"item": self.item_same}))["totals"]
		it = get_item_summary(self._payload("item", {"item": self.item_same}))["totals"]
		inv = get_inventory_account_summary(self._payload("inventory_account", {"item": self.item_same}))[
			"totals"
		]
		for k in ("inward_value", "outward_value", "balance_value", "debit_balance", "credit_balance"):
			self.assertAlmostEqual(flt(ig[k]), flt(it[k]), places=2)
			self.assertAlmostEqual(flt(ig[k]), flt(inv[k]), places=2)

	def test_cross_account_transfer(self):
		spec = AccountExplorerQuerySpec_from_client(
			self._payload("inventory_account", {"item": self.item_cross}), require_dates=True
		)
		attr = get_inventory_account_attribution(spec)
		ma = attr.rows_by_account.get(self.acc_a) or {}
		mb = attr.rows_by_account.get(self.acc_b) or {}
		self.assertAlmostEqual(flt(ma.get("outward_value")), 100.0, places=2)
		self.assertAlmostEqual(flt(mb.get("inward_value")), 100.0, places=2)
		inv = get_inventory_account_summary(self._payload("inventory_account", {"item": self.item_cross}))
		t = inv["totals"]
		self.assertAlmostEqual(flt(t["inward_value"]), 100.0, places=2)
		self.assertAlmostEqual(flt(t["outward_value"]), 100.0, places=2)
		self.assertAlmostEqual(flt(t["balance_value"]), 0.0, places=2)
		ig = get_item_group_summary(self._payload("item_group", {"item": self.item_cross}))["totals"]
		self.assertAlmostEqual(flt(ig["balance_value"]), 0.0, places=2)

	def test_footer_side_net_positive_and_negative(self):
		# Group filter includes same + cross + neg → mixed signs possible per account
		inv = get_inventory_account_summary(self._payload("inventory_account", {"item_group": self.group}))
		rows = inv["rows"]
		signed = sum(flt(r.get("balance_value")) for r in rows)
		totals = inv["totals"]
		expect_debit = max(signed, 0.0)
		expect_credit = abs(min(signed, 0.0))
		self.assertAlmostEqual(flt(totals["debit_balance"]), expect_debit, places=2)
		self.assertAlmostEqual(flt(totals["credit_balance"]), expect_credit, places=2)
		# Not Σ row debit/credit
		sum_debit_cols = sum(flt(r.get("debit_balance")) for r in rows)
		sum_credit_cols = sum(flt(r.get("credit_balance")) for r in rows)
		recomputed = sum_stock_measure_rows(rows, include_qty=False)
		self.assertAlmostEqual(flt(totals["debit_balance"]), flt(recomputed["debit_balance"]), places=2)
		self.assertAlmostEqual(flt(totals["credit_balance"]), flt(recomputed["credit_balance"]), places=2)
		# Side-netting: footer debit/credit come from Σ signed, not Σ column debit/credit
		self.assertAlmostEqual(flt(totals["balance_value"]), signed, places=2)
		if sum_credit_cols > 0:
			self.assertNotEqual(flt(sum_debit_cols), flt(totals["debit_balance"]))
	def test_account_sle_scope_under_inventory(self):
		with_f = self._payload("account_level", {"item_group": self.group})
		without = self._payload("account_level", {})
		without["document_scope"]["inventory"] = {"item_group": None, "item": None, "warehouse": None}
		a = get_account_summary(with_f)
		b = get_account_summary(without)
		self.assertEqual(a.get("account_fact_engine"), "sle_scoped_stock")
		self.assertEqual(b.get("account_fact_engine"), "posted_gl")
		from erpnext_extensions.iran_accounting.account_explorer.api import get_item_group_summary

		ig = get_item_group_summary(self._payload("item_group", {"item_group": self.group}))
		self.assertAlmostEqual(
			flt(a["totals"].get("debit_balance")),
			flt(ig["totals"].get("debit_balance")),
			places=2,
		)

	def test_item_qty_footer_suppressed(self):
		res = get_item_summary(self._payload("item", {"item_group": self.group}))
		totals = res["totals"]
		self.assertNotIn("in_qty", totals)
		self.assertEqual(totals.get("qty_footer_policy"), "suppressed_mixed_uom")
		# Row qty identity still holds
		for r in res["rows"]:
			self.assertAlmostEqual(
				flt(r["balance_qty"]), flt(r["in_qty"]) - flt(r["out_qty"]), places=3
			)

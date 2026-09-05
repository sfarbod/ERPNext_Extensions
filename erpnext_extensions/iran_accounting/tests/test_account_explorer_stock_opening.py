# Copyright (c) 2026, Farbod Siyahpoosh and contributors
"""Stock opening semantics tests — Opening Stock SR, value-only recon, opening-only rows."""

from __future__ import annotations

import unittest
from datetime import timedelta
from decimal import Decimal

import frappe
from frappe.utils import flt, now

from erpnext_extensions.iran_accounting.account_explorer.api import get_item_group_summary, get_item_summary
from erpnext_extensions.iran_accounting.tests.test_account_explorer_fixtures import (
	build_payload,
	current_fiscal_year,
)
from erpnext_extensions.iran_accounting.tests.test_account_explorer_inventory_axes import (
	_ensure_item_group,
	_ensure_stock_item,
	enable_inventory_analysis,
	getdate_safe,
	require_inventory_company,
)


def _insert_sle_raw(
	*,
	company: str,
	item_code: str,
	warehouse: str,
	posting_date: str,
	actual_qty: float,
	qty_after_transaction: float,
	stock_value: float,
	stock_value_difference: float,
	voucher_type: str,
	voucher_no: str,
	posting_time: str = "12:00:00",
) -> str:
	existing = frappe.db.exists(
		"Stock Ledger Entry",
		{
			"voucher_no": voucher_no,
			"item_code": item_code,
			"warehouse": warehouse,
			"company": company,
			"is_cancelled": 0,
		},
	)
	if existing:
		return existing
	from frappe.utils import get_datetime

	name = f"aesle-op-{frappe.generate_hash(length=8)}"
	valuation_rate = abs(stock_value / qty_after_transaction) if qty_after_transaction else 0
	frappe.db.sql(
		"""
		insert into `tabStock Ledger Entry`
			(name, creation, modified, modified_by, owner, docstatus, idx,
			 company, item_code, warehouse, posting_date, posting_time, posting_datetime,
			 voucher_type, voucher_no, actual_qty, qty_after_transaction,
			 stock_value_difference, valuation_rate, stock_value, is_cancelled)
		values
			(%s, %s, %s, %s, %s, 0, 0,
			 %s, %s, %s, %s, %s, %s,
			 %s, %s, %s, %s,
			 %s, %s, %s, 0)
		""",
		(
			name,
			now(),
			now(),
			frappe.session.user,
			frappe.session.user,
			company,
			item_code,
			warehouse,
			posting_date,
			posting_time,
			get_datetime(f"{posting_date} {posting_time}"),
			voucher_type,
			voucher_no,
			actual_qty,
			qty_after_transaction,
			stock_value_difference,
			valuation_rate,
			stock_value,
		),
	)
	frappe.db.commit()
	return name


def _ensure_opening_stock_sr(name: str, company: str, posting_date: str) -> str:
	if frappe.db.exists("Stock Reconciliation", name):
		return name
	frappe.db.sql(
		"""
		insert into `tabStock Reconciliation`
			(name, creation, modified, modified_by, owner, docstatus, idx,
			 company, posting_date, posting_time, purpose, naming_series)
		values
			(%s, %s, %s, %s, %s, 1, 0,
			 %s, %s, %s, 'Opening Stock', 'MAT-RECO-.YYYY.-')
		""",
		(name, now(), now(), frappe.session.user, frappe.session.user, company, posting_date, "10:00:00"),
	)
	frappe.db.commit()
	return name


def _dec(value) -> Decimal:
	return Decimal(str(flt(value)))


class TestAccountExplorerStockOpening(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.company = require_inventory_company(cls)
		enable_inventory_analysis()
		fy = current_fiscal_year(cls.company)
		if not fy:
			raise unittest.SkipTest(f"No fiscal year for {cls.company}")
		cls.fiscal_year, cls.fy_start, cls.fy_end = fy
		suffix = frappe.generate_hash(length=6)
		cls.parent = _ensure_item_group(f"AE OP Parent {suffix}", "All Item Groups", is_group=1)
		cls.leaf = _ensure_item_group(f"AE OP Leaf {suffix}", cls.parent, is_group=0)
		warehouses = frappe.get_all(
			"Warehouse", filters={"company": cls.company, "is_group": 0}, pluck="name", limit=2
		)
		if not warehouses:
			raise unittest.SkipTest("No warehouse")
		cls.warehouse = warehouses[0]
		cls.warehouse2 = warehouses[1] if len(warehouses) > 1 else warehouses[0]

		cls.item_open = _ensure_stock_item(f"AE-OP-OPEN-{suffix}", cls.leaf, cls.company, cls.warehouse)
		cls.item_move = _ensure_stock_item(f"AE-OP-MOVE-{suffix}", cls.leaf, cls.company, cls.warehouse)
		cls.item_value = _ensure_stock_item(f"AE-OP-VAL-{suffix}", cls.leaf, cls.company, cls.warehouse)
		cls.item_wh2 = _ensure_stock_item(f"AE-OP-WH2-{suffix}", cls.leaf, cls.company, cls.warehouse2)

		cls.from_date = str(getdate_safe(cls.fy_start) + timedelta(days=10))
		cls.to_date = str(getdate_safe(cls.fy_start) + timedelta(days=40))
		cls.open_date = str(getdate_safe(cls.fy_start) + timedelta(days=2))
		cls.mid_date = str(getdate_safe(cls.fy_start) + timedelta(days=20))
		cls.boundary_date = cls.from_date

		sr_before = f"AE-OP-SR-BEFORE-{cls.item_open}"
		_ensure_opening_stock_sr(sr_before, cls.company, cls.open_date)
		_insert_sle_raw(
			company=cls.company,
			item_code=cls.item_open,
			warehouse=cls.warehouse,
			posting_date=cls.open_date,
			actual_qty=0,
			qty_after_transaction=100,
			stock_value=10000,
			stock_value_difference=10000,
			voucher_type="Stock Reconciliation",
			voucher_no=sr_before,
		)

		sr_boundary = f"AE-OP-SR-BOUND-{cls.item_move}"
		_ensure_opening_stock_sr(sr_boundary, cls.company, cls.boundary_date)
		_insert_sle_raw(
			company=cls.company,
			item_code=cls.item_move,
			warehouse=cls.warehouse,
			posting_date=cls.boundary_date,
			actual_qty=0,
			qty_after_transaction=50,
			stock_value=5000,
			stock_value_difference=5000,
			voucher_type="Stock Reconciliation",
			voucher_no=sr_boundary,
			posting_time="08:00:00",
		)
		_insert_sle_raw(
			company=cls.company,
			item_code=cls.item_move,
			warehouse=cls.warehouse,
			posting_date=cls.mid_date,
			actual_qty=10,
			qty_after_transaction=60,
			stock_value=6000,
			stock_value_difference=1000,
			voucher_type="Stock Entry",
			voucher_no=f"AE-OP-SE-IN-{cls.item_move}",
		)
		_insert_sle_raw(
			company=cls.company,
			item_code=cls.item_move,
			warehouse=cls.warehouse,
			posting_date=cls.mid_date,
			actual_qty=-4,
			qty_after_transaction=56,
			stock_value=5600,
			stock_value_difference=-400,
			voucher_type="Stock Entry",
			voucher_no=f"AE-OP-SE-OUT-{cls.item_move}",
		)

		_insert_sle_raw(
			company=cls.company,
			item_code=cls.item_value,
			warehouse=cls.warehouse,
			posting_date=cls.open_date,
			actual_qty=20,
			qty_after_transaction=20,
			stock_value=2000,
			stock_value_difference=2000,
			voucher_type="Stock Entry",
			voucher_no=f"AE-OP-SE-OPEN-{cls.item_value}",
		)
		_insert_sle_raw(
			company=cls.company,
			item_code=cls.item_value,
			warehouse=cls.warehouse,
			posting_date=cls.mid_date,
			actual_qty=0,
			qty_after_transaction=20,
			stock_value=2500,
			stock_value_difference=500,
			voucher_type="Stock Reconciliation",
			voucher_no=f"AE-OP-SR-VAL-{cls.item_value}",
		)

		sr_wh2 = f"AE-OP-SR-WH2-{cls.item_wh2}"
		_ensure_opening_stock_sr(sr_wh2, cls.company, cls.open_date)
		_insert_sle_raw(
			company=cls.company,
			item_code=cls.item_wh2,
			warehouse=cls.warehouse2,
			posting_date=cls.open_date,
			actual_qty=0,
			qty_after_transaction=7,
			stock_value=700,
			stock_value_difference=700,
			voucher_type="Stock Reconciliation",
			voucher_no=sr_wh2,
		)

	def _item_payload(self, **inventory):
		return build_payload(
			self.company,
			self.fiscal_year,
			self.from_date,
			self.to_date,
			analysis={"view_axis": "item", "page_size": 200, "sort_field": "display_code"},
			document={"hide_zero_rows": 0, "inventory": inventory},
		)

	def test_opening_recon_before_from_date(self):
		result = get_item_summary(self._item_payload(item=self.item_open))
		rows = {r["item_code"]: r for r in result["rows"]}
		row = rows[self.item_open]
		# Opening 100/10000 rolled into In / Inward
		self.assertNotIn("opening_qty", row)
		self.assertNotIn("opening_value", row)
		self.assertEqual(_dec(row["in_qty"]), Decimal("100"))
		self.assertEqual(_dec(row["out_qty"]), Decimal("0"))
		self.assertEqual(_dec(row["balance_qty"]), Decimal("100"))
		self.assertEqual(_dec(row["inward_value"]), Decimal("10000"))
		self.assertEqual(_dec(row["outward_value"]), Decimal("0"))
		self.assertEqual(_dec(row["balance_value"]), Decimal("10000"))
		cols = {c["id"] for c in result.get("columns") or []}
		if cols:
			self.assertNotIn("opening_qty", cols)
			self.assertNotIn("opening_value", cols)

	def test_opening_recon_on_from_date_counts_as_opening(self):
		rows = {r["item_code"]: r for r in get_item_summary(self._item_payload(item=self.item_move))["rows"]}
		row = rows[self.item_move]
		# Opening 50 + period in 10 - out 4 → In 60, Out 4, Balance 56
		self.assertEqual(_dec(row["in_qty"]), Decimal("60"))
		self.assertEqual(_dec(row["out_qty"]), Decimal("4"))
		self.assertEqual(_dec(row["balance_qty"]), Decimal("56"))
		self.assertEqual(_dec(row["inward_value"]), Decimal("6000"))  # 5000 + 1000
		self.assertEqual(_dec(row["outward_value"]), Decimal("400"))
		self.assertEqual(_dec(row["balance_value"]), Decimal("5600"))
		self.assertEqual(_dec(row["balance_qty"]), _dec(row["in_qty"]) - _dec(row["out_qty"]))
		self.assertEqual(_dec(row["balance_value"]), _dec(row["inward_value"]) - _dec(row["outward_value"]))

	def test_opening_only_item_visible_without_period_movement(self):
		payload = build_payload(
			self.company,
			self.fiscal_year,
			self.from_date,
			self.to_date,
			analysis={"view_axis": "item", "page_size": 200},
			document={"hide_zero_rows": 1, "inventory": {"item": self.item_open}},
		)
		rows = {r["item_code"]: r for r in get_item_summary(payload)["rows"]}
		self.assertIn(self.item_open, rows)
		row = rows[self.item_open]
		self.assertEqual(_dec(row["in_qty"]), Decimal("100"))
		self.assertEqual(_dec(row["balance_qty"]), Decimal("100"))
		self.assertEqual(_dec(row["inward_value"]), Decimal("10000"))
		self.assertEqual(_dec(row["balance_value"]), Decimal("10000"))

	def test_value_only_reconciliation_in_period(self):
		rows = {r["item_code"]: r for r in get_item_summary(self._item_payload(item=self.item_value))["rows"]}
		row = rows[self.item_value]
		# Opening qty/value 20/2000 + period value-only +500
		self.assertEqual(_dec(row["in_qty"]), Decimal("20"))
		self.assertEqual(_dec(row["out_qty"]), Decimal("0"))
		self.assertEqual(_dec(row["balance_qty"]), Decimal("20"))
		self.assertEqual(_dec(row["inward_value"]), Decimal("2500"))
		self.assertEqual(_dec(row["outward_value"]), Decimal("0"))
		self.assertEqual(_dec(row["balance_value"]), Decimal("2500"))

	def test_warehouse_filter_scopes_opening(self):
		rows = {
			r["item_code"]: r
			for r in get_item_summary(self._item_payload(warehouse=self.warehouse2))["rows"]
		}
		self.assertIn(self.item_wh2, rows)
		self.assertNotIn(self.item_open, rows)
		self.assertEqual(_dec(rows[self.item_wh2]["in_qty"]), Decimal("7"))
		self.assertEqual(_dec(rows[self.item_wh2]["balance_qty"]), Decimal("7"))

	def test_item_group_opening_equals_item_sum(self):
		group_payload = build_payload(
			self.company,
			self.fiscal_year,
			self.from_date,
			self.to_date,
			analysis={"view_axis": "item_group", "page_size": 200},
			document={"hide_zero_rows": 0, "inventory": {"item_group": self.leaf}},
		)
		item_payload = self._item_payload(item_group=self.leaf)
		group_totals = get_item_group_summary(group_payload)["totals"]
		item_totals = get_item_summary(item_payload)["totals"]
		self.assertAlmostEqual(flt(group_totals.get("inward_value")), flt(item_totals.get("inward_value")), places=2)
		self.assertAlmostEqual(flt(group_totals.get("balance_value")), flt(item_totals.get("balance_value")), places=2)
		self.assertNotIn("opening_value", group_totals)
		groups = {r["item_group"] for r in get_item_group_summary(group_payload)["rows"]}
		self.assertNotIn(self.parent, groups)
		self.assertIn(self.leaf, groups)

	def test_parent_group_filter_leaf_opening(self):
		payload = build_payload(
			self.company,
			self.fiscal_year,
			self.from_date,
			self.to_date,
			analysis={"view_axis": "item_group", "page_size": 200},
			document={"hide_zero_rows": 0, "inventory": {"item_group": self.parent}},
		)
		groups = {r["item_group"] for r in get_item_group_summary(payload)["rows"]}
		self.assertIn(self.leaf, groups)
		self.assertNotIn(self.parent, groups)

	def test_api_columns_exclude_opening(self):
		from erpnext_extensions.iran_accounting.account_explorer.item_group_summary import ITEM_GROUP_COLUMNS
		from erpnext_extensions.iran_accounting.account_explorer.item_summary import ITEM_COLUMNS

		item_ids = {c["id"] for c in ITEM_COLUMNS}
		group_ids = {c["id"] for c in ITEM_GROUP_COLUMNS}
		for banned in ("opening_qty", "opening_value", "closing_qty", "closing_value"):
			self.assertNotIn(banned, item_ids)
			self.assertNotIn(banned, group_ids)
		self.assertTrue(
			{"in_qty", "out_qty", "balance_qty", "inward_value", "outward_value", "debit_balance", "credit_balance"}
			<= item_ids
		)
		self.assertTrue(
			{"inward_value", "outward_value", "debit_balance", "credit_balance"} <= group_ids
		)
		self.assertNotIn("balance_value", item_ids)
		self.assertNotIn("balance_value", group_ids)

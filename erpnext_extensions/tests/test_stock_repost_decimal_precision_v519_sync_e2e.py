# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Integration: v5.1.9 schema-drift repair + production overflow regression."""

from __future__ import annotations

import unittest
from decimal import Decimal

import frappe
from frappe.utils import nowdate, nowtime

from erpnext_extensions.approved_decimal_precision import read_column_schema
from erpnext_extensions.patches.post_model_sync.ensure_stock_repost_decimal_precision_v519 import (
	execute as ensure_stock_repost_decimal_precision_v519_execute,
)
from erpnext_extensions.stock_repost_decimal_precision_v519 import (
	CRITICAL_STOCK_ENTRY_DETAIL_FIELDS,
	after_migrate,
	assert_stock_repost_decimal_schema,
	get_stock_repost_precision_status,
	repair_stock_repost_decimal_schema,
)

# Real production overflow values from Repost Item Valuation failure.
PROD_BASIC_RATE = Decimal("-4626689860")
PROD_VALUATION_RATE = Decimal("-4626276180")
PROD_BASIC_AMOUNT = Decimal("-1647101590164.27")
PROD_AMOUNT = Decimal("-1646954320184")


def _col(table: str, column: str) -> tuple[int | None, int | None, str | None]:
	info = read_column_schema(table, column)
	if not info:
		return None, None, None
	return info.get("NUMERIC_PRECISION"), info.get("NUMERIC_SCALE"), info.get("COLUMN_TYPE")


def _as_decimal(value) -> Decimal:
	return Decimal(str(value))


def _force_decimal(table: str, column: str, precision: int) -> None:
	info = read_column_schema(table, column)
	null_sql = "NULL" if str(info.get("IS_NULLABLE")).upper() == "YES" else "NOT NULL"
	default = info.get("COLUMN_DEFAULT")
	default_sql = f" DEFAULT {frappe.db.escape(str(default))}" if default is not None else ""
	frappe.db.sql_ddl(
		f"ALTER TABLE `{table}` MODIFY `{column}` DECIMAL({precision},9) {null_sql}{default_sql}"
	)


class TestStockRepostDecimalPrecisionV519SyncE2E(unittest.TestCase):
	def test_updatedb_narrows_without_property_setter_and_repair_heals(self):
		"""Exact class of regression that allowed production failure after v5.1.8."""
		ps_filters = {
			"doc_type": "Stock Entry Detail",
			"field_name": "basic_amount",
			"property": "length",
			"doctype_or_field": "DocField",
		}
		ps_name = frappe.db.get_value("Property Setter", ps_filters, "name")
		self.assertTrue(ps_name, "v5.1.8 Property Setter must exist before drift test")

		_force_decimal("tabStock Entry Detail", "basic_amount", 30)
		self.assertEqual(_col("tabStock Entry Detail", "basic_amount")[2], "decimal(30,9)")

		# Remove Property Setter → updatedb must narrow (Frappe default DECIMAL(21,9)).
		frappe.delete_doc("Property Setter", ps_name, force=True, ignore_permissions=True)
		frappe.clear_cache(doctype="Stock Entry Detail")
		frappe.db.commit()
		frappe.db.updatedb("Stock Entry Detail")
		self.assertEqual(
			_col("tabStock Entry Detail", "basic_amount"),
			(21, 9, "decimal(21,9)"),
			"updatedb without length=30 Property Setter must narrow basic_amount",
		)

		# v5.1.9 repair restores metadata + SQL and asserts.
		result = repair_stock_repost_decimal_schema(run_completeness_guard=False)
		self.assertIn("tabStock Entry Detail.basic_amount", result["repaired"])
		self.assertEqual(_col("tabStock Entry Detail", "basic_amount"), (30, 9, "decimal(30,9)"))
		assert_stock_repost_decimal_schema()

		# updatedb must NOT revert when Property Setter is restored.
		frappe.clear_cache(doctype="Stock Entry Detail")
		frappe.db.updatedb("Stock Entry Detail")
		self.assertEqual(_col("tabStock Entry Detail", "basic_amount"), (30, 9, "decimal(30,9)"))

		# Second repair is a no-op.
		second = repair_stock_repost_decimal_schema(run_completeness_guard=False)
		self.assertEqual(second["repaired"], [])
		self.assertEqual(second["after"]["incorrect"], 0)

	def test_after_migrate_heals_forced_drift(self):
		_force_decimal("tabStock Entry Detail", "basic_amount", 21)
		self.assertEqual(_col("tabStock Entry Detail", "basic_amount")[0], 21)
		after_migrate()
		self.assertEqual(_col("tabStock Entry Detail", "basic_amount"), (30, 9, "decimal(30,9)"))
		status = get_stock_repost_precision_status()
		self.assertEqual(status["incorrect"], 0)

	def test_patch_execute_and_critical_fields(self):
		ensure_stock_repost_decimal_precision_v519_execute()
		for field in CRITICAL_STOCK_ENTRY_DETAIL_FIELDS:
			self.assertEqual(_col("tabStock Entry Detail", field), (30, 9, "decimal(30,9)"), field)
		for field in (
			"stock_value",
			"stock_value_difference",
			"incoming_rate",
			"outgoing_rate",
			"valuation_rate",
		):
			self.assertEqual(_col("tabStock Ledger Entry", field), (30, 9, "decimal(30,9)"), field)

	def test_production_overflow_values_db_update(self):
		ensure_stock_repost_decimal_precision_v519_execute()

		company = frappe.db.get_value("Company", {}, "name")
		item = frappe.db.get_value("Item", {"disabled": 0, "is_stock_item": 1}, "name")
		warehouse = (
			frappe.db.get_value("Warehouse", {"company": company, "is_group": 0}, "name") if company else None
		)
		if not company or not item or not warehouse:
			self.skipTest("Missing Company/Item/Warehouse")

		se = frappe.new_doc("Stock Entry")
		se.company = company
		se.stock_entry_type = (
			frappe.db.get_value("Stock Entry Type", {"purpose": "Material Receipt"}, "name")
			or "Material Receipt"
		)
		se.purpose = "Material Receipt"
		se.posting_date = nowdate()
		se.posting_time = nowtime()
		se.append(
			"items",
			{"item_code": item, "qty": 1, "t_warehouse": warehouse, "basic_rate": 1, "valuation_rate": 1},
		)
		se.flags.ignore_mandatory = True
		se.flags.ignore_validate = True
		se.insert(ignore_permissions=True)
		frappe.db.commit()

		row_name = se.items[0].name
		frappe.db.sql(
			"""
			UPDATE `tabStock Entry Detail`
			SET basic_amount=%s, amount=%s, basic_rate=%s, valuation_rate=%s
			WHERE name=%s
			""",
			(str(PROD_BASIC_AMOUNT), str(PROD_AMOUNT), str(PROD_BASIC_RATE), str(PROD_VALUATION_RATE), row_name),
		)
		frappe.db.commit()

		raw = frappe.db.sql(
			"""
			SELECT CAST(basic_amount AS CHAR), CAST(amount AS CHAR),
			       CAST(basic_rate AS CHAR), CAST(valuation_rate AS CHAR)
			FROM `tabStock Entry Detail` WHERE name=%s
			""",
			row_name,
		)[0]
		self.assertEqual(_as_decimal(raw[0]), PROD_BASIC_AMOUNT)
		self.assertEqual(_as_decimal(raw[1]), PROD_AMOUNT)
		self.assertEqual(_as_decimal(raw[2]), PROD_BASIC_RATE)
		self.assertEqual(_as_decimal(raw[3]), PROD_VALUATION_RATE)

		row = frappe.get_doc("Stock Entry Detail", row_name)
		row.basic_amount = float(PROD_BASIC_AMOUNT)
		row.amount = float(PROD_AMOUNT)
		row.flags.ignore_validate = True
		row.db_update()
		frappe.db.commit()
		again = frappe.db.sql(
			"SELECT CAST(basic_amount AS CHAR) FROM `tabStock Entry Detail` WHERE name=%s",
			row_name,
		)[0][0]
		self.assertEqual(_as_decimal(again), PROD_BASIC_AMOUNT)

		from erpnext.stock.stock_ledger import update_entries_after

		obj = update_entries_after.__new__(update_entries_after)
		try:
			obj.recalculate_amounts_in_stock_entry(se.name, row_name)
			frappe.db.commit()
		except Exception as exc:
			msg = str(exc)
			if "1264" in msg or "Out of range" in msg:
				raise
			frappe.db.rollback()

		self.assertEqual(_col("tabStock Entry Detail", "basic_amount"), (30, 9, "decimal(30,9)"))
		frappe.delete_doc("Stock Entry", se.name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def test_sle_updatedb_non_revert(self):
		ensure_stock_repost_decimal_precision_v519_execute()
		frappe.clear_cache(doctype="Stock Ledger Entry")
		frappe.db.updatedb("Stock Ledger Entry")
		for field in ("stock_value", "stock_value_difference", "valuation_rate"):
			self.assertEqual(_col("tabStock Ledger Entry", field), (30, 9, "decimal(30,9)"), field)

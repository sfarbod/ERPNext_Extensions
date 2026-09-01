# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Integration: Sales Order amount_eligible_for_commission DECIMAL(30,9) + large IRR regression."""

from __future__ import annotations

import unittest
from decimal import Decimal

import frappe
from frappe.utils import nowdate

from erpnext_extensions.patches.post_model_sync.expand_sales_order_amount_eligible_for_commission_precision import (
	execute as expand_sales_order_amount_eligible_for_commission_precision_execute,
)
from erpnext_extensions.patches.pre_model_sync.set_sales_order_amount_eligible_for_commission_decimal_metadata import (
	execute as set_sales_order_amount_eligible_for_commission_decimal_metadata_execute,
)
from erpnext_extensions.sales_order_commission_decimal_precision import (
	TARGET_FIELD,
	assert_schema_target,
	sales_order_commission_field_target,
)

LARGE_IRR = Decimal("1445552233069")
TARGET_TABLE = "tabSales Order"
SPOT_CHECK_OTHER_FIELDS = ("grand_total", "base_grand_total", "total_commission")


def _read_column(table: str, column: str) -> tuple[int | None, int | None, str | None]:
	db_name = frappe.db.sql("SELECT DATABASE()")[0][0]
	row = frappe.db.sql(
		"""
		SELECT NUMERIC_PRECISION, NUMERIC_SCALE, COLUMN_TYPE
		FROM INFORMATION_SCHEMA.COLUMNS
		WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND COLUMN_NAME=%s
		""",
		(db_name, table, column),
		as_dict=True,
	)
	if not row:
		return None, None, None
	return (
		row[0].get("NUMERIC_PRECISION"),
		row[0].get("NUMERIC_SCALE"),
		row[0].get("COLUMN_TYPE"),
	)


def _as_decimal(value) -> Decimal:
	return Decimal(str(value))


class TestSalesOrderCommissionDecimalPrecisionSyncE2E(unittest.TestCase):
	def test_patch_updatedb_schema_guard_and_idempotency(self):
		set_sales_order_amount_eligible_for_commission_decimal_metadata_execute()
		expand_sales_order_amount_eligible_for_commission_precision_execute()

		precision, scale, column_type = _read_column(TARGET_TABLE, TARGET_FIELD)
		self.assertEqual((precision, scale, column_type), (30, 9, "decimal(30,9)"))

		target = sales_order_commission_field_target()
		value = frappe.db.get_value(
			"Property Setter",
			{
				"doc_type": target.doctype,
				"field_name": target.fieldname,
				"property": "length",
				"doctype_or_field": "DocField",
			},
			"value",
		)
		self.assertEqual(value, "30")

		frappe.clear_cache(doctype=target.doctype)
		frappe.db.updatedb(target.doctype)

		precision, scale, column_type = _read_column(TARGET_TABLE, TARGET_FIELD)
		self.assertEqual((precision, scale, column_type), (30, 9, "decimal(30,9)"), "updatedb must not revert")

		before = _read_column(TARGET_TABLE, TARGET_FIELD)
		other_snapshots = {
			field: _read_column(TARGET_TABLE, field) for field in SPOT_CHECK_OTHER_FIELDS
		}
		set_sales_order_amount_eligible_for_commission_decimal_metadata_execute()
		expand_sales_order_amount_eligible_for_commission_precision_execute()
		after = _read_column(TARGET_TABLE, TARGET_FIELD)
		self.assertEqual(before, after)
		assert_schema_target()

		for field, before_type in other_snapshots.items():
			if before_type[0] is None:
				continue
			after_type = _read_column(TARGET_TABLE, field)
			self.assertEqual(before_type, after_type, f"unchanged non-target field {field}")

	def test_large_irr_sales_order_round_trip(self):
		set_sales_order_amount_eligible_for_commission_decimal_metadata_execute()
		expand_sales_order_amount_eligible_for_commission_precision_execute()

		company = frappe.db.get_value("Company", {}, "name")
		customer = frappe.db.get_value("Customer", {}, "name")
		item = frappe.db.get_value("Item", {"disabled": 0, "is_sales_item": 1}, "name") or frappe.db.get_value(
			"Item", {}, "name"
		)
		if not company or not customer or not item:
			self.skipTest("Missing Company, Customer, or Item for Sales Order regression")

		amount = float(LARGE_IRR)
		so = frappe.new_doc("Sales Order")
		so.company = company
		so.customer = customer
		so.transaction_date = nowdate()
		so.delivery_date = nowdate()
		so.append(
			"items",
			{
				"item_code": item,
				"qty": 1,
				"rate": 1,
				"grant_commission": 0,
			},
		)
		so.set(TARGET_FIELD, amount)
		so.flags.ignore_mandatory = True
		so.flags.ignore_validate = True
		so.flags.ignore_links = True
		so.insert(ignore_permissions=True)
		frappe.db.commit()

		# Persist large commission-eligible total via save (not item rate) — the failing column.
		reloaded = frappe.get_doc("Sales Order", so.name)
		reloaded.set(TARGET_FIELD, amount)
		reloaded.flags.ignore_validate = True
		reloaded.flags.ignore_mandatory = True
		reloaded.save(ignore_permissions=True)
		frappe.db.commit()

		reloaded = frappe.get_doc("Sales Order", so.name)
		self.assertEqual(_as_decimal(reloaded.get(TARGET_FIELD)), LARGE_IRR)
		raw = frappe.db.sql(
			f"SELECT `{TARGET_FIELD}` FROM `{TARGET_TABLE}` WHERE name=%s",
			so.name,
		)[0][0]
		self.assertEqual(_as_decimal(raw), LARGE_IRR)
		self.assertNotIn("e", str(raw).lower())

		precision, scale, column_type = _read_column(TARGET_TABLE, TARGET_FIELD)
		self.assertEqual((precision, scale, column_type), (30, 9, "decimal(30,9)"))

		frappe.delete_doc("Sales Order", so.name, force=True, ignore_permissions=True)
		frappe.db.commit()

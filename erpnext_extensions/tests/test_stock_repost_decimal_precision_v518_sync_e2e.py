# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Integration: stock repost DECIMAL(30,9) v5.1.8 + basic_amount overflow regression."""

from __future__ import annotations

import unittest
from decimal import Decimal

import frappe
from frappe.utils import nowdate, nowtime

from erpnext_extensions.patches.post_model_sync.expand_stock_repost_amount_precision_v518 import (
	execute as expand_stock_repost_amount_precision_v518_execute,
)
from erpnext_extensions.patches.pre_model_sync.set_stock_repost_amount_decimal_metadata_v518 import (
	execute as set_stock_repost_amount_decimal_metadata_v518_execute,
)
from erpnext_extensions.stock_repost_decimal_precision_v518 import (
	EXCLUDED_RATE_PERCENT_QTY_FIELDS_BY_DOCTYPE,
	REPOST_MONETARY_FIELDS_BY_DOCTYPE,
	assert_repost_field_classification_completeness,
	assert_repost_monetary_schema_targets,
	repost_field_targets,
)

# Reported overflow values from production Repost Item Valuation failure.
FAILING_BASIC_AMOUNT = Decimal("-10277543566399.46")
FAILING_AMOUNT = Decimal("-10277541091774")
FAILING_BASIC_RATE = Decimal("-5105585478")
FAILING_VALUATION_RATE = Decimal("-5105584248")
LARGE_IRR_FRACTION = Decimal("1682808518031.123456789")
LARGE_POSITIVE = Decimal("10277543566399.460000000")


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
	return row[0].get("NUMERIC_PRECISION"), row[0].get("NUMERIC_SCALE"), row[0].get("COLUMN_TYPE")


def _as_decimal(value) -> Decimal:
	return Decimal(str(value))


class TestStockRepostDecimalPrecisionV518SyncE2E(unittest.TestCase):
	def test_patch_updatedb_schema_guard_and_idempotency(self):
		set_stock_repost_amount_decimal_metadata_v518_execute()
		expand_stock_repost_amount_precision_v518_execute()

		for target in repost_field_targets():
			if not frappe.db.exists("DocType", target.doctype):
				continue
			if not frappe.get_meta(target.doctype).get_field(target.fieldname):
				continue
			precision, scale, column_type = _read_column(target.table, target.fieldname)
			if precision is None:
				continue
			self.assertEqual(
				(precision, scale, column_type),
				(30, 9, "decimal(30,9)"),
				f"{target.table}.{target.fieldname}",
			)

		for doctype in ("Stock Entry", "Stock Entry Detail", "Stock Ledger Entry", "Stock Reconciliation"):
			frappe.clear_cache(doctype=doctype)
			frappe.db.updatedb(doctype)

		precision, scale, column_type = _read_column("tabStock Entry Detail", "basic_amount")
		self.assertEqual((precision, scale, column_type), (30, 9, "decimal(30,9)"))

		before = {
			(t.table, t.fieldname): _read_column(t.table, t.fieldname)
			for t in repost_field_targets()
			if frappe.db.exists("DocType", t.doctype)
			and frappe.get_meta(t.doctype).get_field(t.fieldname)
			and _read_column(t.table, t.fieldname)[0]
		}
		set_stock_repost_amount_decimal_metadata_v518_execute()
		expand_stock_repost_amount_precision_v518_execute()
		after = {
			(t.table, t.fieldname): _read_column(t.table, t.fieldname)
			for t in repost_field_targets()
			if frappe.db.exists("DocType", t.doctype)
			and frappe.get_meta(t.doctype).get_field(t.fieldname)
			and _read_column(t.table, t.fieldname)[0]
		}
		self.assertEqual(before, after)
		assert_repost_monetary_schema_targets()
		assert_repost_field_classification_completeness()

		# Qty fields remain default width.
		for field in ("qty", "actual_qty", "transfer_qty"):
			precision, scale, _ = _read_column("tabStock Entry Detail", field)
			if precision is None:
				continue
			self.assertEqual((precision, scale), (21, 9), field)

	def test_reported_basic_amount_overflow_round_trip(self):
		"""Prove the exact production failure value can persist on Stock Entry Detail."""
		set_stock_repost_amount_decimal_metadata_v518_execute()
		expand_stock_repost_amount_precision_v518_execute()

		precision, scale, column_type = _read_column("tabStock Entry Detail", "basic_amount")
		self.assertEqual((precision, scale, column_type), (30, 9, "decimal(30,9)"))

		company = frappe.db.get_value("Company", {}, "name")
		item = frappe.db.get_value("Item", {"disabled": 0, "is_stock_item": 1}, "name") or frappe.db.get_value(
			"Item", {}, "name"
		)
		warehouse = (
			frappe.db.get_value("Warehouse", {"company": company, "is_group": 0}, "name") if company else None
		)
		if not company or not item or not warehouse:
			self.skipTest("Missing Company/Item/Warehouse")

		se = frappe.new_doc("Stock Entry")
		se.company = company
		se.stock_entry_type = frappe.db.get_value("Stock Entry Type", {"purpose": "Material Receipt"}, "name") or "Material Receipt"
		se.purpose = "Material Receipt"
		se.posting_date = nowdate()
		se.posting_time = nowtime()
		se.append(
			"items",
			{
				"item_code": item,
				"qty": 1,
				"t_warehouse": warehouse,
				"basic_rate": 1,
				"basic_amount": 1,
				"amount": 1,
				"valuation_rate": 1,
			},
		)
		se.flags.ignore_mandatory = True
		se.flags.ignore_validate = True
		se.flags.ignore_permissions = True
		se.insert(ignore_permissions=True)
		frappe.db.commit()

		row_name = se.items[0].name
		# Direct SQL write path mirrors repost db_update with large IRR values.
		frappe.db.sql(
			"""
			UPDATE `tabStock Entry Detail`
			SET basic_amount=%s, amount=%s, basic_rate=%s, valuation_rate=%s
			WHERE name=%s
			""",
			(
				str(FAILING_BASIC_AMOUNT),
				str(FAILING_AMOUNT),
				str(FAILING_BASIC_RATE),
				str(FAILING_VALUATION_RATE),
				row_name,
			),
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
		self.assertEqual(_as_decimal(raw[0]), FAILING_BASIC_AMOUNT)
		self.assertEqual(_as_decimal(raw[1]), FAILING_AMOUNT)
		self.assertEqual(_as_decimal(raw[2]), FAILING_BASIC_RATE)
		self.assertEqual(_as_decimal(raw[3]), FAILING_VALUATION_RATE)

		# Document-level reload + db_update (repost path analogue).
		doc = frappe.get_doc("Stock Entry", se.name)
		row = doc.items[0]
		row.basic_amount = float(FAILING_BASIC_AMOUNT)
		row.amount = float(FAILING_AMOUNT)
		row.basic_rate = float(FAILING_BASIC_RATE)
		row.valuation_rate = float(FAILING_VALUATION_RATE)
		row.db_update()
		frappe.db.commit()

		reloaded = frappe.get_doc("Stock Entry", se.name)
		self.assertEqual(_as_decimal(reloaded.items[0].basic_amount), FAILING_BASIC_AMOUNT)

		frappe.delete_doc("Stock Entry", se.name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def test_sle_and_fractional_round_trip(self):
		set_stock_repost_amount_decimal_metadata_v518_execute()
		expand_stock_repost_amount_precision_v518_execute()

		for table, field, value in (
			("tabStock Ledger Entry", "stock_value_difference", LARGE_POSITIVE),
			("tabStock Ledger Entry", "stock_value", LARGE_IRR_FRACTION),
			("tabStock Entry Detail", "basic_amount", LARGE_IRR_FRACTION),
		):
			precision, scale, column_type = _read_column(table, field)
			self.assertEqual((precision, scale, column_type), (30, 9, "decimal(30,9)"), f"{table}.{field}")

		# Temporary SLE row via insert if possible; otherwise schema-only coverage above.
		company = frappe.db.get_value("Company", {}, "name")
		item = frappe.db.get_value("Item", {"disabled": 0, "is_stock_item": 1}, "name")
		warehouse = (
			frappe.db.get_value("Warehouse", {"company": company, "is_group": 0}, "name") if company else None
		)
		if not (company and item and warehouse):
			self.skipTest("Missing fixtures for SLE insert")

		# Use existing SLE if any — update stock_value via SQL then restore.
		existing = frappe.db.sql(
			"SELECT name, CAST(stock_value AS CHAR) FROM `tabStock Ledger Entry` LIMIT 1"
		)
		if not existing:
			self.skipTest("No SLE rows available")
		name, original = existing[0]
		frappe.db.sql(
			"UPDATE `tabStock Ledger Entry` SET stock_value=%s, stock_value_difference=%s WHERE name=%s",
			(str(LARGE_IRR_FRACTION), str(LARGE_POSITIVE), name),
		)
		frappe.db.commit()
		raw = frappe.db.sql(
			"SELECT CAST(stock_value AS CHAR), CAST(stock_value_difference AS CHAR) FROM `tabStock Ledger Entry` WHERE name=%s",
			name,
		)[0]
		self.assertEqual(_as_decimal(raw[0]), LARGE_IRR_FRACTION)
		self.assertEqual(_as_decimal(raw[1]), LARGE_POSITIVE)
		frappe.db.sql(
			"UPDATE `tabStock Ledger Entry` SET stock_value=%s WHERE name=%s",
			(original, name),
		)
		frappe.db.commit()

	def test_recalculate_amounts_in_stock_entry_path(self):
		"""Exercise ERPNext recalculate_amounts_in_stock_entry without DataError 1264."""
		set_stock_repost_amount_decimal_metadata_v518_execute()
		expand_stock_repost_amount_precision_v518_execute()

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
			{
				"item_code": item,
				"qty": 1,
				"t_warehouse": warehouse,
				"basic_rate": 1,
				"valuation_rate": 1,
			},
		)
		se.flags.ignore_mandatory = True
		se.flags.ignore_validate = True
		se.insert(ignore_permissions=True)
		frappe.db.commit()

		# Seed large amounts (including negative IRR valuation) then invoke the exact stock_ledger helper.
		frappe.db.sql(
			"""
			UPDATE `tabStock Entry Detail`
			SET basic_amount=%s, amount=%s, basic_rate=%s, valuation_rate=%s
			WHERE parent=%s
			""",
			(
				str(FAILING_BASIC_AMOUNT),
				str(FAILING_AMOUNT),
				str(FAILING_BASIC_RATE),
				str(FAILING_VALUATION_RATE),
				se.name,
			),
		)
		frappe.db.commit()

		from erpnext.stock.stock_ledger import update_entries_after

		obj = update_entries_after.__new__(update_entries_after)
		try:
			obj.recalculate_amounts_in_stock_entry(se.name, se.items[0].name)
			frappe.db.commit()
		except Exception as exc:
			# Legitimate negative-stock / valuation validation is allowed; DataError 1264 is not.
			msg = str(exc)
			if "1264" in msg or "Out of range" in msg:
				raise
			frappe.db.rollback()

		precision, scale, column_type = _read_column("tabStock Entry Detail", "basic_amount")
		self.assertEqual((precision, scale, column_type), (30, 9, "decimal(30,9)"))

		# Direct db_update of the seeded overflow values must succeed (repost write path).
		row = frappe.get_doc("Stock Entry Detail", se.items[0].name)
		row.basic_amount = float(FAILING_BASIC_AMOUNT)
		row.amount = float(FAILING_AMOUNT)
		row.flags.ignore_validate = True
		row.db_update()
		frappe.db.commit()
		raw = frappe.db.sql(
			"SELECT CAST(basic_amount AS CHAR) FROM `tabStock Entry Detail` WHERE name=%s",
			se.items[0].name,
		)[0][0]
		self.assertEqual(_as_decimal(raw), FAILING_BASIC_AMOUNT)

		frappe.delete_doc("Stock Entry", se.name, force=True, ignore_permissions=True)
		frappe.db.commit()

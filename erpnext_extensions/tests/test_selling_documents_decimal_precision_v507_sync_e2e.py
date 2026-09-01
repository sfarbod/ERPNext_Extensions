# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Integration: selling documents DECIMAL(30,9) v5.0.7 schema + large IRR regression."""

from __future__ import annotations

import unittest
from decimal import Decimal

import frappe
from frappe.utils import nowdate

from erpnext_extensions.patches.post_model_sync.expand_selling_documents_amount_precision_v507 import (
	execute as expand_selling_documents_amount_precision_v507_execute,
)
from erpnext_extensions.patches.pre_model_sync.set_selling_documents_amount_decimal_metadata_v507 import (
	execute as set_selling_documents_amount_decimal_metadata_v507_execute,
)
from erpnext_extensions.selling_documents_decimal_precision_v507 import (
	EXCLUDED_RATE_PERCENT_FIELDS_BY_DOCTYPE,
	SELLING_AMOUNT_FIELDS_BY_DOCTYPE,
	SELLING_ROOT_DOCTYPES,
	assert_field_classification_completeness,
	assert_schema_targets,
	selling_field_targets,
)

LARGE_IRR_A = Decimal("1445552233069")
LARGE_IRR_B = Decimal("1682808518031")
LARGE_IRR_FRACTION = Decimal("1682808518031.123456789")

TARGET_COLUMNS: dict[str, tuple[str, ...]] = {
	f"tab{doctype}": fields for doctype, fields in SELLING_AMOUNT_FIELDS_BY_DOCTYPE.items()
}


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


def _fixtures():
	company = frappe.db.get_value("Company", {}, "name")
	customer = frappe.db.get_value("Customer", {}, "name")
	item = frappe.db.get_value("Item", {"disabled": 0, "is_sales_item": 1}, "name") or frappe.db.get_value(
		"Item", {}, "name"
	)
	warehouse = frappe.db.get_value("Warehouse", {"is_group": 0, "company": company}, "name") if company else None
	income_account = (
		frappe.db.get_value("Account", {"company": company, "account_type": "Income Account", "is_group": 0}, "name")
		if company
		else None
	)
	if not company or not customer or not item:
		return None
	return company, customer, item, warehouse, income_account


def _apply_flags(doc):
	doc.flags.ignore_mandatory = True
	doc.flags.ignore_validate = True
	doc.flags.ignore_links = True


class TestSellingDocumentsDecimalPrecisionV507SyncE2E(unittest.TestCase):
	def test_patch_updatedb_schema_guard_and_idempotency(self):
		set_selling_documents_amount_decimal_metadata_v507_execute()
		expand_selling_documents_amount_precision_v507_execute()

		for target in selling_field_targets():
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

		for doctype in SELLING_ROOT_DOCTYPES:
			frappe.clear_cache(doctype=doctype)
			frappe.db.updatedb(doctype)

		for target in selling_field_targets():
			if not frappe.get_meta(target.doctype).get_field(target.fieldname):
				continue
			precision, scale, column_type = _read_column(target.table, target.fieldname)
			if precision is None:
				continue
			self.assertEqual(
				(precision, scale, column_type),
				(30, 9, "decimal(30,9)"),
				f"sync {target.table}.{target.fieldname}",
			)

		before = {
			(t.table, t.fieldname): _read_column(t.table, t.fieldname)
			for t in selling_field_targets()
			if frappe.get_meta(t.doctype).get_field(t.fieldname) and _read_column(t.table, t.fieldname)[0]
		}
		set_selling_documents_amount_decimal_metadata_v507_execute()
		expand_selling_documents_amount_precision_v507_execute()
		after = {
			(t.table, t.fieldname): _read_column(t.table, t.fieldname)
			for t in selling_field_targets()
			if frappe.get_meta(t.doctype).get_field(t.fieldname) and _read_column(t.table, t.fieldname)[0]
		}
		self.assertEqual(before, after)
		assert_schema_targets()
		assert_field_classification_completeness()

		for doctype, fields in EXCLUDED_RATE_PERCENT_FIELDS_BY_DOCTYPE.items():
			table = f"tab{doctype}"
			for field in fields:
				precision, scale, column_type = _read_column(table, field)
				if precision is None:
					continue
				self.assertEqual(
					(precision, scale),
					(21, 9),
					f"excluded rate field unchanged {table}.{field}={column_type}",
				)

	def test_large_irr_sales_order_round_trip(self):
		set_selling_documents_amount_decimal_metadata_v507_execute()
		expand_selling_documents_amount_precision_v507_execute()
		fixtures = _fixtures()
		if not fixtures:
			self.skipTest("Missing Company/Customer/Item")
		company, customer, item, _warehouse, _income = fixtures
		amount = float(LARGE_IRR_A)

		so = frappe.new_doc("Sales Order")
		so.company = company
		so.customer = customer
		so.transaction_date = nowdate()
		so.delivery_date = nowdate()
		so.append("items", {"item_code": item, "qty": 1, "rate": 1, "grant_commission": 0})
		_apply_flags(so)
		so.insert(ignore_permissions=True)
		frappe.db.commit()

		doc = frappe.get_doc("Sales Order", so.name)
		doc.set("grand_total", amount)
		doc.set("amount_eligible_for_commission", amount)
		doc.set("total", amount)
		if doc.items:
			doc.items[0].amount = amount
			doc.items[0].base_amount = amount
		_apply_flags(doc)
		doc.save(ignore_permissions=True)
		frappe.db.commit()

		reloaded = frappe.get_doc("Sales Order", so.name)
		self.assertEqual(_as_decimal(reloaded.grand_total), LARGE_IRR_A)
		self.assertEqual(_as_decimal(reloaded.amount_eligible_for_commission), LARGE_IRR_A)
		self.assertEqual(_as_decimal(reloaded.items[0].amount), LARGE_IRR_A)

		frappe.db.sql(
			"UPDATE `tabSales Order` SET grand_total=%s WHERE name=%s",
			(str(LARGE_IRR_FRACTION), so.name),
		)
		frappe.db.commit()
		raw = frappe.db.sql("SELECT CAST(grand_total AS CHAR) FROM `tabSales Order` WHERE name=%s", so.name)[0][0]
		self.assertEqual(_as_decimal(raw), LARGE_IRR_FRACTION)

		frappe.delete_doc("Sales Order", so.name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def test_large_irr_delivery_note_round_trip(self):
		set_selling_documents_amount_decimal_metadata_v507_execute()
		expand_selling_documents_amount_precision_v507_execute()
		fixtures = _fixtures()
		if not fixtures:
			self.skipTest("Missing Company/Customer/Item")
		company, customer, item, warehouse, _income = fixtures
		if not warehouse:
			self.skipTest("Missing Warehouse")
		amount = float(LARGE_IRR_B)

		dn = frappe.new_doc("Delivery Note")
		dn.company = company
		dn.customer = customer
		dn.posting_date = nowdate()
		dn.append("items", {"item_code": item, "qty": 1, "rate": 1, "warehouse": warehouse})
		_apply_flags(dn)
		dn.insert(ignore_permissions=True)
		frappe.db.commit()

		doc = frappe.get_doc("Delivery Note", dn.name)
		doc.set("grand_total", amount)
		doc.set("total", amount)
		if doc.items:
			doc.items[0].amount = amount
			doc.items[0].base_amount = amount
		_apply_flags(doc)
		doc.save(ignore_permissions=True)
		frappe.db.commit()

		reloaded = frappe.get_doc("Delivery Note", dn.name)
		self.assertEqual(_as_decimal(reloaded.grand_total), LARGE_IRR_B)
		self.assertEqual(_as_decimal(reloaded.items[0].amount), LARGE_IRR_B)

		frappe.delete_doc("Delivery Note", dn.name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def test_large_irr_sales_invoice_round_trip(self):
		set_selling_documents_amount_decimal_metadata_v507_execute()
		expand_selling_documents_amount_precision_v507_execute()
		fixtures = _fixtures()
		if not fixtures:
			self.skipTest("Missing Company/Customer/Item")
		company, customer, item, _warehouse, income_account = fixtures
		amount = float(LARGE_IRR_A)

		si = frappe.new_doc("Sales Invoice")
		si.company = company
		si.customer = customer
		si.posting_date = nowdate()
		si.append("items", {"item_code": item, "qty": 1, "rate": 1, "income_account": income_account})
		_apply_flags(si)
		si.insert(ignore_permissions=True)
		frappe.db.commit()

		doc = frappe.get_doc("Sales Invoice", si.name)
		doc.set("grand_total", amount)
		doc.set("outstanding_amount", amount)
		doc.set("total", amount)
		if doc.items:
			doc.items[0].amount = amount
			doc.items[0].base_amount = amount
		_apply_flags(doc)
		doc.save(ignore_permissions=True)
		frappe.db.commit()

		reloaded = frappe.get_doc("Sales Invoice", si.name)
		self.assertEqual(_as_decimal(reloaded.grand_total), LARGE_IRR_A)
		self.assertEqual(_as_decimal(reloaded.outstanding_amount), LARGE_IRR_A)
		self.assertEqual(_as_decimal(reloaded.items[0].amount), LARGE_IRR_A)

		frappe.db.sql(
			"UPDATE `tabSales Invoice` SET outstanding_amount=%s WHERE name=%s",
			(str(LARGE_IRR_FRACTION), si.name),
		)
		frappe.db.commit()
		raw = frappe.db.sql(
			"SELECT CAST(outstanding_amount AS CHAR) FROM `tabSales Invoice` WHERE name=%s",
			si.name,
		)[0][0]
		self.assertEqual(_as_decimal(raw), LARGE_IRR_FRACTION)

		frappe.delete_doc("Sales Invoice", si.name, force=True, ignore_permissions=True)
		frappe.db.commit()

# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Regression: Sales Invoice custom gross/discount DECIMAL(30,9) overflow fix (v5.7.9)."""

from __future__ import annotations

import unittest
from decimal import Decimal

import frappe
from frappe.utils import nowdate

from erpnext_extensions.approved_decimal_precision import read_column_schema
from erpnext_extensions.decimal_precision_after_migrate import after_migrate as coordinator_after_migrate
from erpnext_extensions.patches.post_model_sync.expand_sales_invoice_custom_amount_precision_v579 import (
	execute as expand_v579_execute,
)
from erpnext_extensions.patches.pre_model_sync.set_sales_invoice_custom_amount_decimal_metadata_v579 import (
	execute as set_metadata_v579_execute,
)
from erpnext_extensions.selling_documents_decimal_precision_v507 import (
	V579_CRITICAL_SALES_INVOICE_FIELDS,
	V579_CRITICAL_SALES_INVOICE_ITEM_FIELDS,
	assert_field_classification_completeness,
	assert_schema_targets,
	get_selling_precision_status,
	repair_selling_documents_decimal_schema,
)

# Production overflow value from MAT-DN-2026-00049 → Sales Invoice.
PROD_GROSS_TOTAL = Decimal("1818483388563")
PROD_TOTAL_DISCOUNT = Decimal("381268484274")
PROD_BASE_TOTAL = Decimal("1437214904289")
PROD_GRAND_TOTAL = Decimal("1559506909551")


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


def _apply_flags(doc):
	doc.flags.ignore_mandatory = True
	doc.flags.ignore_validate = True
	doc.flags.ignore_links = True
	doc.flags.ignore_permissions = True


class TestSalesInvoiceCustomAmountPrecisionV579(unittest.TestCase):
	def test_critical_fields_in_registry(self):
		from erpnext_extensions.selling_documents_decimal_precision_v507 import (
			SELLING_AMOUNT_FIELDS_BY_DOCTYPE,
		)

		for field in V579_CRITICAL_SALES_INVOICE_FIELDS:
			self.assertIn(field, SELLING_AMOUNT_FIELDS_BY_DOCTYPE["Sales Invoice"])
		for field in V579_CRITICAL_SALES_INVOICE_ITEM_FIELDS:
			self.assertIn(field, SELLING_AMOUNT_FIELDS_BY_DOCTYPE["Sales Invoice Item"])

	def test_patch_expands_schema_and_is_idempotent(self):
		set_metadata_v579_execute()
		expand_v579_execute()

		for field in V579_CRITICAL_SALES_INVOICE_FIELDS:
			self.assertEqual(_col("tabSales Invoice", field), (30, 9, "decimal(30,9)"), field)
			cf_len = frappe.db.get_value(
				"Custom Field", {"dt": "Sales Invoice", "fieldname": field}, "length"
			)
			self.assertGreaterEqual(int(cf_len or 0), 30, field)
			ps = frappe.db.exists(
				"Property Setter",
				{
					"doc_type": "Sales Invoice",
					"field_name": field,
					"property": "length",
					"doctype_or_field": "DocField",
				},
			)
			self.assertTrue(ps, f"Property Setter missing for Sales Invoice.{field}")

		for field in V579_CRITICAL_SALES_INVOICE_ITEM_FIELDS:
			self.assertEqual(_col("tabSales Invoice Item", field), (30, 9, "decimal(30,9)"), field)

		second = repair_selling_documents_decimal_schema(run_completeness_guard=True)
		self.assertEqual(second["repaired"], [])
		assert_schema_targets()
		assert_field_classification_completeness()
		status = get_selling_precision_status()
		self.assertEqual(status["incorrect"], 0)
		self.assertEqual(status["critical_sales_invoice"]["custom_gross_total"], "decimal(30,9)")

	def test_updatedb_does_not_narrow_after_metadata(self):
		set_metadata_v579_execute()
		expand_v579_execute()
		frappe.clear_cache(doctype="Sales Invoice")
		frappe.db.updatedb("Sales Invoice")
		frappe.clear_cache(doctype="Sales Invoice Item")
		frappe.db.updatedb("Sales Invoice Item")
		self.assertEqual(_col("tabSales Invoice", "custom_gross_total"), (30, 9, "decimal(30,9)"))
		self.assertEqual(_col("tabSales Invoice", "custom_total_discount"), (30, 9, "decimal(30,9)"))
		self.assertEqual(_col("tabSales Invoice Item", "custom_gross_amount"), (30, 9, "decimal(30,9)"))

	def test_coordinator_heals_forced_drift(self):
		set_metadata_v579_execute()
		expand_v579_execute()
		_force_decimal("tabSales Invoice", "custom_gross_total", 21)
		self.assertEqual(_col("tabSales Invoice", "custom_gross_total")[0], 21)
		coordinator_after_migrate()
		self.assertEqual(_col("tabSales Invoice", "custom_gross_total"), (30, 9, "decimal(30,9)"))

	def test_large_irr_custom_amounts_persist(self):
		set_metadata_v579_execute()
		expand_v579_execute()

		company = frappe.db.get_value("Company", {}, "name")
		customer = frappe.db.get_value("Customer", {}, "name")
		item = frappe.db.get_value("Item", {"disabled": 0, "is_sales_item": 1}, "name") or frappe.db.get_value(
			"Item", {}, "name"
		)
		if not company or not customer or not item:
			self.skipTest("Missing Company/Customer/Item")

		si = frappe.new_doc("Sales Invoice")
		si.company = company
		si.customer = customer
		si.posting_date = nowdate()
		si.append("items", {"item_code": item, "qty": 1, "rate": 1})
		_apply_flags(si)
		si.insert(ignore_permissions=True)
		frappe.db.commit()

		doc = frappe.get_doc("Sales Invoice", si.name)
		doc.custom_gross_total = float(PROD_GROSS_TOTAL)
		doc.custom_total_discount = float(PROD_TOTAL_DISCOUNT)
		doc.base_total = float(PROD_BASE_TOTAL)
		doc.total = float(PROD_BASE_TOTAL)
		doc.net_total = float(PROD_BASE_TOTAL)
		doc.base_net_total = float(PROD_BASE_TOTAL)
		doc.grand_total = float(PROD_GRAND_TOTAL)
		doc.base_grand_total = float(PROD_GRAND_TOTAL)
		if doc.items:
			doc.items[0].custom_gross_amount = float(PROD_GROSS_TOTAL)
		_apply_flags(doc)
		doc.save(ignore_permissions=True)
		frappe.db.commit()

		reloaded = frappe.get_doc("Sales Invoice", si.name)
		self.assertEqual(_as_decimal(reloaded.custom_gross_total), PROD_GROSS_TOTAL)
		self.assertEqual(_as_decimal(reloaded.custom_total_discount), PROD_TOTAL_DISCOUNT)
		self.assertEqual(_as_decimal(reloaded.items[0].custom_gross_amount), PROD_GROSS_TOTAL)
		# IRR integer valuation: stored scale may be .000000000 but integer part exact.
		self.assertEqual(_as_decimal(reloaded.custom_gross_total) % 1, Decimal("0"))

		frappe.delete_doc("Sales Invoice", si.name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def test_delivery_note_to_sales_invoice_large_totals(self):
		"""Reproduce DN → SI path for MAT-DN-2026-00049 style large IRR totals."""
		set_metadata_v579_execute()
		expand_v579_execute()

		dn_name = "MAT-DN-2026-00049"
		if not frappe.db.exists("Delivery Note", dn_name):
			self.skipTest(f"Missing Delivery Note {dn_name}")

		dn = frappe.get_doc("Delivery Note", dn_name)
		self.assertEqual(dn.docstatus, 1)

		# Prefer ERPNext make_sales_invoice mapping; fall back to manual draft SI.
		si = None
		try:
			from erpnext.stock.doctype.delivery_note.delivery_note import make_sales_invoice

			si = make_sales_invoice(dn_name)
		except Exception:
			si = None

		if si is None:
			company = dn.company
			customer = dn.customer
			item = dn.items[0].item_code if dn.items else None
			if not item:
				self.skipTest("DN has no items")
			si = frappe.new_doc("Sales Invoice")
			si.company = company
			si.customer = customer
			si.posting_date = dn.posting_date or nowdate()
			si.append(
				"items",
				{
					"item_code": item,
					"qty": 1,
					"rate": float(PROD_BASE_TOTAL),
					"delivery_note": dn_name,
				},
			)

		_apply_flags(si)
		# Client script would compute these; set explicitly for DB capacity proof.
		si.custom_gross_total = float(PROD_GROSS_TOTAL)
		si.custom_total_discount = float(PROD_TOTAL_DISCOUNT)
		if si.meta.get_field("custom_total_foc_amount"):
			si.custom_total_foc_amount = 0
		if si.meta.get_field("custom_total_raw_discount_amount"):
			si.custom_total_raw_discount_amount = 0
		if si.items and si.items[0].meta.get_field("custom_gross_amount"):
			si.items[0].custom_gross_amount = float(PROD_GROSS_TOTAL)

		si.insert(ignore_permissions=True)
		frappe.db.commit()

		reloaded = frappe.get_doc("Sales Invoice", si.name)
		self.assertEqual(_as_decimal(reloaded.custom_gross_total), PROD_GROSS_TOTAL)
		self.assertEqual(_as_decimal(reloaded.custom_total_discount), PROD_TOTAL_DISCOUNT)

		frappe.delete_doc("Sales Invoice", si.name, force=True, ignore_permissions=True)
		frappe.db.commit()

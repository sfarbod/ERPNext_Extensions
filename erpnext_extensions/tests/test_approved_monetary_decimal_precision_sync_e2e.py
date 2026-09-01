from __future__ import annotations

import unittest

import frappe

from erpnext_extensions.approved_decimal_precision import APPROVED_FIELDS_BY_DOCTYPE
from erpnext_extensions.patches.post_model_sync.expand_approved_monetary_amount_precision import (
	execute as expand_approved_monetary_amount_precision_execute,
)
from erpnext_extensions.patches.pre_model_sync.set_approved_monetary_decimal_metadata import (
	execute as set_approved_monetary_decimal_metadata_execute,
)

TARGET_COLUMNS: dict[str, tuple[str, ...]] = {
	"tabFacility": APPROVED_FIELDS_BY_DOCTYPE["Facility"],
	"tabFacility Repayment": APPROVED_FIELDS_BY_DOCTYPE["Facility Repayment"],
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


class TestApprovedMonetaryDecimalPrecisionSyncE2E(unittest.TestCase):
	def test_patch_and_updatedb_are_idempotent(self):
		set_approved_monetary_decimal_metadata_execute()
		expand_approved_monetary_amount_precision_execute()

		for table, columns in TARGET_COLUMNS.items():
			for column in columns:
				precision, scale, column_type = _read_column(table, column)
				self.assertEqual((precision, scale, column_type), (30, 9, "decimal(30,9)"), f"{table}.{column}")

		target_property_setters = {
			(row.doc_type, row.field_name): row.value
			for row in frappe.get_all(
				"Property Setter",
				filters={
					"doc_type": ("in", list(APPROVED_FIELDS_BY_DOCTYPE)),
					"property": "length",
					"field_name": ("in", sorted({f for fields in APPROVED_FIELDS_BY_DOCTYPE.values() for f in fields})),
				},
				fields=["doc_type", "field_name", "value"],
			)
		}
		for doctype, fields in APPROVED_FIELDS_BY_DOCTYPE.items():
			for fieldname in fields:
				self.assertEqual(target_property_setters.get((doctype, fieldname)), "30", f"{doctype}.{fieldname}")

		for doctype in APPROVED_FIELDS_BY_DOCTYPE:
			frappe.clear_cache(doctype=doctype)
			frappe.db.updatedb(doctype)

		for table, columns in TARGET_COLUMNS.items():
			for column in columns:
				precision, scale, column_type = _read_column(table, column)
				self.assertEqual((precision, scale, column_type), (30, 9, "decimal(30,9)"), f"sync {table}.{column}")

		before_second_run = {
			(table, column): _read_column(table, column) for table, cols in TARGET_COLUMNS.items() for column in cols
		}
		set_approved_monetary_decimal_metadata_execute()
		expand_approved_monetary_amount_precision_execute()
		after_second_run = {
			(table, column): _read_column(table, column) for table, cols in TARGET_COLUMNS.items() for column in cols
		}
		self.assertEqual(before_second_run, after_second_run)

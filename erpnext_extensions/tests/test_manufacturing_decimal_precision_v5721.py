# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Unit + schema tests for manufacturing DECIMAL(30,9) (v5.7.21)."""

from __future__ import annotations

import unittest
from decimal import Decimal
from unittest.mock import patch

import frappe

from erpnext_extensions import manufacturing_decimal_precision_v5721 as v5721
from erpnext_extensions.approved_decimal_precision import read_column_schema
from erpnext_extensions.patches.post_model_sync.expand_manufacturing_amount_precision_v5721 import (
	execute as expand_execute,
)
from erpnext_extensions.patches.pre_model_sync.set_manufacturing_amount_decimal_metadata_v5721 import (
	execute as metadata_execute,
)

NINE_DECIMAL = Decimal("123456789.123456789")
LARGE_IRR = Decimal("1500000000000")


def _col(table: str, column: str):
	info = read_column_schema(table, column)
	if not info:
		return None, None, None
	return info.get("NUMERIC_PRECISION"), info.get("NUMERIC_SCALE"), info.get("COLUMN_TYPE")


class TestManufacturingDecimalPrecisionV5721(unittest.TestCase):
	def test_registry_covers_bom_wo_jc_costs(self):
		self.assertIn("total_cost", v5721.MANUFACTURING_MONETARY_FIELDS_BY_DOCTYPE["BOM"])
		self.assertIn("planned_operating_cost", v5721.MANUFACTURING_MONETARY_FIELDS_BY_DOCTYPE["Work Order"])
		self.assertIn("hour_rate", v5721.MANUFACTURING_MONETARY_FIELDS_BY_DOCTYPE["Job Card"])
		self.assertGreaterEqual(len(v5721.manufacturing_field_targets()), 40)

	def test_qty_fields_excluded(self):
		self.assertIn("quantity", v5721.EXCLUDED_RATE_PERCENT_QTY_FIELDS_BY_DOCTYPE["BOM"])
		self.assertIn("required_qty", v5721.EXCLUDED_RATE_PERCENT_QTY_FIELDS_BY_DOCTYPE["Work Order Item"])
		self.assertIn("for_quantity", v5721.EXCLUDED_RATE_PERCENT_QTY_FIELDS_BY_DOCTYPE["Job Card"])

	def test_decide_widens_21_2_hour_rate(self):
		action = v5721.decide_manufacturing_decimal_action(
			{"DATA_TYPE": "decimal", "NUMERIC_PRECISION": 21, "NUMERIC_SCALE": 2}
		)
		self.assertEqual(action, v5721.ALTER_TO_DECIMAL_30_9)

	def test_patch_schema_idempotent_and_completeness(self):
		metadata_execute()
		expand_execute()
		status = v5721.get_manufacturing_precision_status()
		self.assertEqual(status["incorrect"], 0)
		for target in v5721.manufacturing_field_targets():
			if not frappe.db.exists("DocType", target.doctype):
				continue
			info = read_column_schema(target.table, target.fieldname)
			if not info:
				continue
			self.assertEqual(
				_col(target.table, target.fieldname)[2],
				"decimal(30,9)",
				f"{target.table}.{target.fieldname}",
			)
		second = v5721.repair_manufacturing_decimal_schema(run_completeness_guard=True)
		self.assertEqual(second["repaired"], [])
		v5721.assert_manufacturing_decimal_schema()
		v5721.assert_manufacturing_field_classification_completeness()

	def test_updatedb_does_not_narrow_representative_fields(self):
		metadata_execute()
		expand_execute()
		for doctype in ("BOM", "BOM Item", "Work Order", "Work Order Item", "Job Card", "BOM Operation"):
			if frappe.db.exists("DocType", doctype):
				frappe.clear_cache(doctype=doctype)
				frappe.db.updatedb(doctype)
		for table, field in (
			("tabBOM", "total_cost"),
			("tabBOM Item", "amount"),
			("tabWork Order", "planned_operating_cost"),
			("tabWork Order Item", "rate"),
			("tabJob Card", "hour_rate"),
			("tabBOM Operation", "hour_rate"),
		):
			self.assertEqual(_col(table, field), (30, 9, "decimal(30,9)"), f"{table}.{field}")

	def test_nine_decimal_and_large_irr_round_trip(self):
		metadata_execute()
		expand_execute()
		# Direct SQL round-trip on BOM.total_cost / Work Order.planned_operating_cost
		# without inventing full manufacturing masters.
		name = frappe.db.get_value("BOM", {"docstatus": ["<", 2]}, "name")
		if not name:
			self.skipTest("No BOM available")
		before = frappe.db.get_value("BOM", name, "total_cost")
		frappe.db.sql(
			"UPDATE `tabBOM` SET total_cost=%s WHERE name=%s",
			(str(NINE_DECIMAL), name),
		)
		frappe.db.commit()
		raw = frappe.db.sql("SELECT CAST(total_cost AS CHAR) FROM `tabBOM` WHERE name=%s", name)[0][0]
		self.assertEqual(Decimal(str(raw)), NINE_DECIMAL)
		frappe.db.sql("UPDATE `tabBOM` SET total_cost=%s WHERE name=%s", (str(LARGE_IRR), name))
		frappe.db.commit()
		raw2 = frappe.db.sql("SELECT CAST(total_cost AS CHAR) FROM `tabBOM` WHERE name=%s", name)[0][0]
		self.assertEqual(Decimal(str(raw2)), LARGE_IRR)
		# restore
		frappe.db.sql("UPDATE `tabBOM` SET total_cost=%s WHERE name=%s", (before, name))
		frappe.db.commit()

	def test_work_order_and_job_card_costs(self):
		metadata_execute()
		expand_execute()
		wo = frappe.db.get_value("Work Order", {}, "name")
		if wo:
			before = frappe.db.get_value("Work Order", wo, "planned_operating_cost")
			frappe.db.sql(
				"UPDATE `tabWork Order` SET planned_operating_cost=%s WHERE name=%s",
				(str(NINE_DECIMAL), wo),
			)
			frappe.db.commit()
			raw = frappe.db.sql(
				"SELECT CAST(planned_operating_cost AS CHAR) FROM `tabWork Order` WHERE name=%s", wo
			)[0][0]
			self.assertEqual(Decimal(str(raw)), NINE_DECIMAL)
			frappe.db.sql(
				"UPDATE `tabWork Order` SET planned_operating_cost=%s WHERE name=%s",
				(before, wo),
			)
			frappe.db.commit()
		jc = frappe.db.get_value("Job Card", {}, "name")
		if jc:
			before = frappe.db.get_value("Job Card", jc, "hour_rate")
			frappe.db.sql(
				"UPDATE `tabJob Card` SET hour_rate=%s WHERE name=%s",
				(str(NINE_DECIMAL), jc),
			)
			frappe.db.commit()
			raw = frappe.db.sql(
				"SELECT CAST(hour_rate AS CHAR) FROM `tabJob Card` WHERE name=%s", jc
			)[0][0]
			self.assertEqual(Decimal(str(raw)), NINE_DECIMAL)
			frappe.db.sql("UPDATE `tabJob Card` SET hour_rate=%s WHERE name=%s", (before, jc))
			frappe.db.commit()

	@patch.object(v5721, "apply_decimal_schema_targets")
	@patch.object(v5721, "verify_and_set_metadata")
	@patch.object(v5721, "assert_manufacturing_decimal_schema")
	@patch.object(v5721, "get_manufacturing_precision_status")
	def test_repair_raises_on_errors(self, mock_status, mock_assert, mock_meta, mock_schema):
		mock_status.return_value = {"incorrect": 1, "correct": 0, "missing": 0, "total_registered": 1}
		mock_meta.return_value = []
		mock_schema.return_value = [
			{"table": "tabBOM", "field": "total_cost", "status": "error", "doctype": "BOM"}
		]
		with self.assertRaises(RuntimeError):
			v5721.repair_manufacturing_decimal_schema()

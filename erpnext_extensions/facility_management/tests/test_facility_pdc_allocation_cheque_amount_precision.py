# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Regression: Facility PDC Allocation.cheque_amount DECIMAL(30,9) overflow fix."""

from __future__ import annotations

import unittest
from decimal import Decimal

import frappe
from frappe.utils import cint

from erpnext_extensions.facility_management.facility_monetary import parse_facility_amount
from erpnext_extensions.facility_management.facility_pdc_allocation import (
	facility_contract_total,
	sum_allocated_cheque_amounts,
)
from erpnext_extensions.facility_management.facility_precision import (
	CRITICAL_FACILITY_PDC_ALLOCATION_FIELDS,
	TARGET_PRECISION,
	TARGET_SCALE,
	assert_facility_pdc_allocation_cheque_amount_schema,
	read_facility_column_schema,
	repair_facility_decimal_schema,
)
from erpnext_extensions.patches.post_model_sync.expand_facility_pdc_allocation_cheque_amount_precision import (
	execute as expand_execute,
)
from erpnext_extensions.patches.pre_model_sync.set_facility_pdc_allocation_cheque_amount_decimal_metadata import (
	execute as metadata_execute,
)

PROD_AMOUNT = Decimal("1500000000000")
FACILITY_NAME = "FAC-2026-07063"
PDC_NAME = "PDC-2026-00359"


def _col(table: str, column: str):
	info = read_facility_column_schema(table, column)
	if not info:
		return None, None, None
	return info.get("NUMERIC_PRECISION"), info.get("NUMERIC_SCALE"), info.get("COLUMN_TYPE")


class TestFacilityPdcAllocationChequeAmountPrecision(unittest.TestCase):
	def test_json_metadata_length_is_30(self):
		meta = frappe.get_meta("Facility PDC Allocation", cached=False)
		df = meta.get_field("cheque_amount")
		self.assertIsNotNone(df)
		self.assertEqual(df.fieldtype, "Currency")
		# DocType JSON length and/or Property Setter must resolve to 30.
		self.assertGreaterEqual(cint(df.length) or 0, TARGET_PRECISION)

	def test_patch_expands_schema_idempotently(self):
		metadata_execute()
		expand_execute()
		self.assertEqual(
			_col("tabFacility PDC Allocation", "cheque_amount"),
			(30, 9, "decimal(30,9)"),
		)
		assert_facility_pdc_allocation_cheque_amount_schema()
		second = repair_facility_decimal_schema()
		self.assertNotIn("tabFacility PDC Allocation.cheque_amount", second["repaired"])
		self.assertEqual(
			_col("tabFacility PDC Allocation", "cheque_amount"),
			(30, 9, "decimal(30,9)"),
		)

	def test_updatedb_does_not_narrow(self):
		metadata_execute()
		expand_execute()
		frappe.clear_cache(doctype="Facility PDC Allocation")
		frappe.db.updatedb("Facility PDC Allocation")
		self.assertEqual(
			_col("tabFacility PDC Allocation", "cheque_amount"),
			(30, 9, "decimal(30,9)"),
		)

	def test_large_irr_amount_persists_exact(self):
		metadata_execute()
		expand_execute()
		if not frappe.db.exists("Facility", FACILITY_NAME):
			self.skipTest(f"Missing Facility {FACILITY_NAME}")
		if not frappe.db.exists("Post Dated Cheque", PDC_NAME):
			self.skipTest(f"Missing PDC {PDC_NAME}")

		pdc_amount = parse_facility_amount(
			frappe.db.get_value("Post Dated Cheque", PDC_NAME, "cheque_amount")
		)
		self.assertEqual(pdc_amount, PROD_AMOUNT)

		fac = frappe.get_doc("Facility", FACILITY_NAME)
		# Remove any prior test allocation of this PDC without deleting other history.
		fac.set(
			"debt_purchase_pdcs",
			[r for r in (fac.debt_purchase_pdcs or []) if r.post_dated_cheque != PDC_NAME],
		)
		fac.append("debt_purchase_pdcs", {"post_dated_cheque": PDC_NAME})
		fac.flags.ignore_permissions = True
		fac.save(ignore_permissions=True)
		frappe.db.commit()

		reloaded = frappe.get_doc("Facility", FACILITY_NAME)
		matches = [r for r in reloaded.debt_purchase_pdcs if r.post_dated_cheque == PDC_NAME]
		self.assertEqual(len(matches), 1, "exactly one allocation row for the PDC")
		self.assertEqual(parse_facility_amount(matches[0].cheque_amount), PROD_AMOUNT)

		raw = frappe.db.sql(
			"""
			SELECT CAST(cheque_amount AS CHAR)
			FROM `tabFacility PDC Allocation`
			WHERE parent=%s AND post_dated_cheque=%s
			""",
			(FACILITY_NAME, PDC_NAME),
		)[0][0]
		self.assertEqual(Decimal(str(raw)), PROD_AMOUNT)

		# Business rule: allocated total equals principal + profit for this Facility.
		self.assertEqual(sum_allocated_cheque_amounts(reloaded), facility_contract_total(reloaded))
		self.assertEqual(facility_contract_total(reloaded), PROD_AMOUNT)

	def test_critical_field_allowlisted(self):
		self.assertEqual(CRITICAL_FACILITY_PDC_ALLOCATION_FIELDS, ("cheque_amount",))
		from erpnext_extensions.facility_management.facility_precision import (
			TABLES_AND_COLUMNS,
		)

		self.assertIn("cheque_amount", TABLES_AND_COLUMNS["tabFacility PDC Allocation"])
		self.assertEqual(TARGET_PRECISION, 30)
		self.assertEqual(TARGET_SCALE, 9)

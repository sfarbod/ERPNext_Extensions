# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Post-model-sync: expand Facility PDC Allocation.cheque_amount to DECIMAL(30,9).

Fixes MySQLdb.DataError 1264 when allocating PDCs with large IRR amounts
(e.g. FAC-2026-07063 / PDC-2026-00359 → 1_500_000_000_000).
"""

from __future__ import annotations

import frappe

from erpnext_extensions.facility_management.facility_precision import (
	CRITICAL_FACILITY_PDC_ALLOCATION_FIELDS,
	alter_facility_decimal_column,
	assert_facility_pdc_allocation_cheque_amount_schema,
	ensure_facility_length_property_setter,
)


def execute() -> None:
	logger = frappe.logger(
		"erpnext_extensions.expand_facility_pdc_allocation_cheque_amount_precision"
	)
	logger.info("Starting expand_facility_pdc_allocation_cheque_amount_precision")
	if not frappe.db.exists("DocType", "Facility PDC Allocation"):
		logger.warning("Skipping missing DocType Facility PDC Allocation")
		return

	for fieldname in CRITICAL_FACILITY_PDC_ALLOCATION_FIELDS:
		ensure_facility_length_property_setter("Facility PDC Allocation", fieldname, logger)
		action = alter_facility_decimal_column("tabFacility PDC Allocation", fieldname, logger)
		logger.info("cheque_amount action=%s", action)

	assert_facility_pdc_allocation_cheque_amount_schema()
	logger.info("Completed expand_facility_pdc_allocation_cheque_amount_precision")

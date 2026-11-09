# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Pre-model-sync: length=30 for Facility PDC Allocation.cheque_amount."""

from __future__ import annotations

import frappe

from erpnext_extensions.facility_management.facility_precision import (
	CRITICAL_FACILITY_PDC_ALLOCATION_FIELDS,
	ensure_facility_length_property_setter,
)


def execute() -> None:
	logger = frappe.logger(
		"erpnext_extensions.set_facility_pdc_allocation_cheque_amount_decimal_metadata"
	)
	logger.info("Starting set_facility_pdc_allocation_cheque_amount_decimal_metadata")
	if not frappe.db.exists("DocType", "Facility PDC Allocation"):
		logger.warning("Skipping missing DocType Facility PDC Allocation")
		return
	for fieldname in CRITICAL_FACILITY_PDC_ALLOCATION_FIELDS:
		ensure_facility_length_property_setter("Facility PDC Allocation", fieldname, logger)
	frappe.clear_cache(doctype="Facility PDC Allocation")
	logger.info("Completed set_facility_pdc_allocation_cheque_amount_decimal_metadata")

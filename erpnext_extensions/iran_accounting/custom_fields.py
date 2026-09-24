# Copyright (c) 2026, ERPNext Extensions contributors
"""Minimal Manufacture costing-contract snapshot fields (v5.3.3)."""

from __future__ import annotations

from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

MODULE = "erpnext_extensions"

FACTOR_FIELD = {
	"fieldname": "custom_output_equivalent_factor",
	"label": "Output Equivalent Factor",
	"fieldtype": "Float",
	"precision": "9",
	"non_negative": 1,
	"description": (
		"Economic equivalent units per 1 stock UOM when physical UOM conversion "
		"cannot establish Stage Output equivalence. Must be greater than zero. "
		"Do not use Cost Allocation % for this."
	),
	"module": MODULE,
}


def get_custom_fields() -> dict:
	return {
		"Stock Entry": [
			{
				"fieldname": "custom_manufacturing_costing_contract_version",
				"label": "Manufacturing Costing Contract Version",
				"fieldtype": "Data",
				"read_only": 1,
				"no_copy": 1,
				"insert_after": "purpose",
				"description": "Frozen Iran Manufacture costing contract (e.g. 5.3.3).",
				"module": MODULE,
			},
		],
		"Stock Entry Detail": [
			{
				"fieldname": "custom_output_class",
				"label": "Output Class",
				"fieldtype": "Data",
				"read_only": 1,
				"no_copy": 1,
				"insert_after": "secondary_item_type",
				"module": MODULE,
			},
			{
				**FACTOR_FIELD,
				"insert_after": "custom_output_class",
			},
			{
				"fieldname": "custom_physical_conversion",
				"label": "Physical Conversion",
				"fieldtype": "Float",
				"precision": "9",
				"read_only": 1,
				"no_copy": 1,
				"insert_after": "custom_output_equivalent_factor",
				"module": MODULE,
			},
			{
				"fieldname": "custom_equivalent_qty",
				"label": "Equivalent Qty",
				"fieldtype": "Float",
				"precision": "9",
				"read_only": 1,
				"no_copy": 1,
				"insert_after": "custom_physical_conversion",
				"module": MODULE,
			},
			{
				"fieldname": "custom_common_uom",
				"label": "Common Equivalent UOM",
				"fieldtype": "Data",
				"read_only": 1,
				"no_copy": 1,
				"insert_after": "custom_equivalent_qty",
				"module": MODULE,
			},
			{
				"fieldname": "custom_parent_co_product",
				"label": "Parent Co-Product",
				"fieldtype": "Link",
				"options": "Item",
				"read_only": 1,
				"no_copy": 1,
				"insert_after": "custom_common_uom",
				"module": MODULE,
			},
		],
		"Job Card Secondary Item": [
			{
				**FACTOR_FIELD,
				"insert_after": "stock_uom",
			},
		],
		"BOM Secondary Item": [
			{
				**FACTOR_FIELD,
				"insert_after": "cost_allocation_per",
			},
		],
	}


def ensure_custom_fields() -> None:
	create_custom_fields(get_custom_fields(), update=True)


def after_migrate() -> None:
	ensure_custom_fields()

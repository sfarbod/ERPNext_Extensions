# Copyright (c) 2026, ERPNext Extensions contributors
"""Custom fields + property setters for MCA provenance and allow_on_submit."""

from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.custom.doctype.property_setter.property_setter import make_property_setter

from erpnext_extensions.manufacturing_cost_adjustment.constants import (
	CF_COMPANY_ENABLE,
	CF_MCA,
	CF_MCA_COST_ROW,
	MODULE,
)


def get_custom_fields() -> dict:
	return {
		"Company": [
			{
				"fieldname": CF_COMPANY_ENABLE,
				"label": "Enable Manufacturing Cost Adjustment",
				"fieldtype": "Check",
				"default": "1",
				"insert_after": "stock_adjustment_account",
				"description": "Allow post-submission Manufacturing Cost Adjustment for Manufacture Stock Entries.",
				"module": MODULE,
			},
		],
		"Landed Cost Taxes and Charges": [
			{
				"fieldname": CF_MCA,
				"label": "Manufacturing Cost Adjustment",
				"fieldtype": "Link",
				"options": "Manufacturing Cost Adjustment",
				"read_only": 1,
				"no_copy": 1,
				"insert_after": "project",
				"description": "Empty for original pre-submit costs; set for MCA-originated rows.",
				"module": MODULE,
			},
			{
				"fieldname": CF_MCA_COST_ROW,
				"label": "MCA Cost Row",
				"fieldtype": "Data",
				"read_only": 1,
				"no_copy": 1,
				"insert_after": CF_MCA,
				"module": MODULE,
			},
		],
	}


def ensure_custom_fields() -> None:
	create_custom_fields(get_custom_fields(), update=True)


def ensure_allow_on_submit() -> None:
	"""Allow appending additional_costs rows after SE submit (server-guarded)."""
	make_property_setter(
		"Stock Entry",
		"additional_costs",
		"allow_on_submit",
		1,
		"Check",
		for_doctype=False,
		validate_fields_for_doctype=False,
	)
	# Child fields needed for new rows
	for fieldname in (
		"expense_account",
		"account_currency",
		"exchange_rate",
		"amount",
		"base_amount",
		"description",
		"has_operating_cost",
		"has_corrective_cost",
		"cost_center",
		"project",
		CF_MCA,
		CF_MCA_COST_ROW,
	):
		make_property_setter(
			"Landed Cost Taxes and Charges",
			fieldname,
			"allow_on_submit",
			1,
			"Check",
			for_doctype=False,
			validate_fields_for_doctype=False,
		)


def after_migrate() -> None:
	ensure_custom_fields()
	ensure_allow_on_submit()

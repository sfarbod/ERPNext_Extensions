# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Batch/Product distribution of Control Credit pools (persisted product_rows)."""

from __future__ import annotations

import frappe
from frappe import _

from erpnext_extensions.iran_accounting.fg_cost_allocation.control_credit.reporting import (
	fetch_control_credit_batch_product_details,
)


def execute(filters=None):
	filters = frappe._dict(filters or {})
	columns = get_columns()
	data = fetch_control_credit_batch_product_details(filters)
	return columns, data


def get_columns():
	return [
		{"label": _("Company"), "fieldname": "company", "fieldtype": "Link", "options": "Company", "width": 130},
		{"label": _("Persian Period"), "fieldname": "persian_period", "fieldtype": "Data", "width": 100},
		{"label": _("Control Type"), "fieldname": "control_type", "fieldtype": "Data", "width": 90},
		{
			"label": _("Control Account"),
			"fieldname": "control_account",
			"fieldtype": "Link",
			"options": "Account",
			"width": 200,
		},
		{
			"label": _("Source Credit Pool"),
			"fieldname": "credit_allocation",
			"fieldtype": "Link",
			"options": "MFG Cost Control Credit Allocation",
			"width": 170,
		},
		{"label": _("Production Batch"), "fieldname": "batch_no", "fieldtype": "Data", "width": 140},
		{
			"label": _("Batch Allocation"),
			"fieldname": "batch_allocation",
			"fieldtype": "Link",
			"options": "MFG Cost Batch Allocation",
			"width": 150,
		},
		{"label": _("Product"), "fieldname": "item_code", "fieldtype": "Link", "options": "Item", "width": 120},
		{
			"label": _("Product Allocation"),
			"fieldname": "product_allocation",
			"fieldtype": "Link",
			"options": "MFG Cost Product Allocation",
			"width": 150,
		},
		{"label": _("Allocation Basis"), "fieldname": "allocation_basis", "fieldtype": "Data", "width": 220},
		{
			"label": _("Applied Batch Share"),
			"fieldname": "applied_batch_share",
			"fieldtype": "Float",
			"width": 120,
			"precision": 9,
		},
		{
			"label": _("Product Share"),
			"fieldname": "product_share",
			"fieldtype": "Float",
			"width": 110,
			"precision": 9,
		},
		{
			"label": _("Allocation Ratio"),
			"fieldname": "allocation_ratio",
			"fieldtype": "Float",
			"width": 110,
			"precision": 9,
		},
		{
			"label": _("Allocated Amount"),
			"fieldname": "allocated_amount",
			"fieldtype": "Currency",
			"options": "currency",
			"width": 130,
		},
		{"label": _("Source Document Type"), "fieldname": "source_document_type", "fieldtype": "Data", "width": 120},
		{"label": _("Source Document"), "fieldname": "source_document", "fieldtype": "Data", "width": 150},
		{"label": _("Quantity"), "fieldname": "quantity", "fieldtype": "Float", "width": 90, "precision": 3},
		{"label": _("Currency"), "fieldname": "currency", "fieldtype": "Link", "options": "Currency", "width": 80},
	]

# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Monthly Labor/Overhead Control Credit pools from persisted allocation documents."""

from __future__ import annotations

import frappe
from frappe import _

from erpnext_extensions.iran_accounting.fg_cost_allocation.control_credit.reporting import (
	fetch_control_credit_pools,
)


def execute(filters=None):
	filters = frappe._dict(filters or {})
	columns = get_columns()
	data = fetch_control_credit_pools(filters)
	return columns, data


def get_columns():
	return [
		{"label": _("Company"), "fieldname": "company", "fieldtype": "Link", "options": "Company", "width": 140},
		{"label": _("Persian Period"), "fieldname": "persian_period", "fieldtype": "Data", "width": 100},
		{"label": _("Control Type"), "fieldname": "control_type", "fieldtype": "Data", "width": 100},
		{
			"label": _("Control Account"),
			"fieldname": "control_account",
			"fieldtype": "Link",
			"options": "Account",
			"width": 220,
		},
		{
			"label": _("Monthly Credit Amount (Allocation Base)"),
			"fieldname": "monthly_credit_amount",
			"fieldtype": "Currency",
			"options": "currency",
			"width": 180,
		},
		{
			"label": _("Previously Allocated (PA-linked LCV)"),
			"fieldname": "previously_allocated",
			"fieldtype": "Currency",
			"options": "currency",
			"width": 180,
		},
		{
			"label": _("Eligible Credit (Informational)"),
			"fieldname": "eligible_credit",
			"fieldtype": "Currency",
			"options": "currency",
			"width": 160,
		},
		{
			"label": _("Allocated Amount"),
			"fieldname": "allocated_amount",
			"fieldtype": "Currency",
			"options": "currency",
			"width": 130,
		},
		{
			"label": _("Remaining Amount"),
			"fieldname": "remaining_amount",
			"fieldtype": "Currency",
			"options": "currency",
			"width": 130,
		},
		{"label": _("Pool Status"), "fieldname": "pool_status", "fieldtype": "Data", "width": 130},
		{"label": _("Document Status"), "fieldname": "document_status", "fieldtype": "Data", "width": 110},
		{"label": _("Source Count"), "fieldname": "source_count", "fieldtype": "Int", "width": 100},
		{
			"label": _("Allocation Reference"),
			"fieldname": "credit_allocation",
			"fieldtype": "Link",
			"options": "MFG Cost Control Credit Allocation",
			"width": 180,
		},
		{"label": _("Currency"), "fieldname": "currency", "fieldtype": "Link", "options": "Currency", "width": 80},
	]

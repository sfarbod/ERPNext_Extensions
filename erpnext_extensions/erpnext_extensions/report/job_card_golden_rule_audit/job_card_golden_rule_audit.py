# Copyright (c) 2026, ERPNext Extensions contributors
"""Script Report: Job Card Golden Rule Audit (v5.5.10).

READ-ONLY. Date basis = Job Card.posting_date.
Grain = Job Card × Component Item (batch ignored).
"""

from __future__ import annotations

import frappe
from frappe import _

from erpnext_extensions.iran_accounting.job_card_stock_rebuild.golden_rule_audit import (
	DATE_BASIS,
	run_golden_rule_audit,
)


def execute(filters=None):
	filters = frappe._dict(filters or {})
	result = run_golden_rule_audit(filters)
	columns = get_columns()
	data = result.get("rows") or []
	c = result.get("counts") or {}
	message = _(
		"Date basis: {0}. Grain: Job Card × Item (batch ignored). "
		"Total Job Cards: {1}. Balanced: {2}. Review: {3}. "
		"Item rows: {4} (balanced {5} / review {6})."
	).format(
		DATE_BASIS,
		c.get("total_job_cards"),
		c.get("balanced_job_cards"),
		c.get("review_job_cards"),
		c.get("total_item_rows"),
		c.get("balanced_item_rows"),
		c.get("review_item_rows"),
	)
	return columns, data, message


def get_columns():
	return [
		{
			"label": _("Job Card"),
			"fieldname": "job_card",
			"fieldtype": "Link",
			"options": "Job Card",
			"width": 140,
		},
		{
			"label": _("Work Order"),
			"fieldname": "work_order",
			"fieldtype": "Link",
			"options": "Work Order",
			"width": 130,
		},
		{
			"label": _("Item"),
			"fieldname": "component_item",
			"fieldtype": "Link",
			"options": "Item",
			"width": 120,
		},
		{
			"label": _("Item Name"),
			"fieldname": "item_name",
			"fieldtype": "Data",
			"width": 160,
		},
		{"label": _("Issued"), "fieldname": "issued_qty", "fieldtype": "Float", "width": 90},
		{"label": _("Returned"), "fieldname": "returned_qty", "fieldtype": "Float", "width": 90},
		{
			"label": _("MFG Consume"),
			"fieldname": "manufacture_consumed_qty",
			"fieldtype": "Float",
			"width": 110,
		},
		{
			"label": _("Material Issue"),
			"fieldname": "other_proven_outflow",
			"fieldtype": "Float",
			"width": 110,
		},
		{"label": _("Scrap"), "fieldname": "component_scrap_qty", "fieldtype": "Float", "width": 80},
		{
			"label": _("Remaining"),
			"fieldname": "remaining_wip",
			"fieldtype": "Float",
			"width": 100,
		},
		{
			"label": _("Status"),
			"fieldname": "golden_status",
			"fieldtype": "Data",
			"width": 100,
		},
		{
			"label": _("Reason"),
			"fieldname": "reason",
			"fieldtype": "Data",
			"width": 160,
		},
		{
			"label": _("JC Summary"),
			"fieldname": "job_card_summary",
			"fieldtype": "Data",
			"width": 100,
		},
		{
			"label": _("MFG Count"),
			"fieldname": "manufacture_count",
			"fieldtype": "Int",
			"width": 80,
		},
		{
			"label": _("MI Review"),
			"fieldname": "material_issue_review",
			"fieldtype": "Data",
			"width": 140,
		},
		{
			"label": _("Suggested"),
			"fieldname": "suggested_disposition",
			"fieldtype": "Data",
			"width": 130,
		},
		{
			"label": _("Posting Date"),
			"fieldname": "posting_date",
			"fieldtype": "Date",
			"width": 100,
		},
		{
			"label": _("Open Rebuild"),
			"fieldname": "open_rebuild",
			"fieldtype": "Data",
			"width": 110,
		},
	]

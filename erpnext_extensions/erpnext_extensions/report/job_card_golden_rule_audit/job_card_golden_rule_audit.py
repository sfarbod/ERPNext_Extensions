# Copyright (c) 2026, ERPNext Extensions contributors
"""Script Report: Job Card Golden Rule Audit (v5.5.0).

READ-ONLY. Date basis = Job Card.posting_date.
Grain = Job Card × Component Item × Batch.
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
	# Attach summary message for UI (does not mutate DB)
	c = result.get("counts") or {}
	message = _(
		"Date basis: {0}. Scanned JC: {1}. Balanced JC: {2}. Review JC: {3}. "
		"Golden Fail: {4}. Multi MFG: {5}. Merge Review: {6}. MI Review: {7}. "
		"Blocked: {8}. Exception rows: {9}."
	).format(
		DATE_BASIS,
		c.get("total_job_cards"),
		c.get("balanced_job_cards"),
		c.get("review_job_cards"),
		c.get("golden_rule_failures"),
		c.get("multiple_manufacture_job_cards"),
		c.get("merge_review_job_cards"),
		c.get("material_issue_review_job_cards"),
		c.get("blocked_job_cards"),
		c.get("exception_rows"),
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
			"label": _("Job Card Status"),
			"fieldname": "job_card_status",
			"fieldtype": "Data",
			"width": 110,
		},
		{
			"label": _("Work Order"),
			"fieldname": "work_order",
			"fieldtype": "Link",
			"options": "Work Order",
			"width": 140,
		},
		{
			"label": _("Production Item"),
			"fieldname": "production_item",
			"fieldtype": "Link",
			"options": "Item",
			"width": 120,
		},
		{
			"label": _("Component Item"),
			"fieldname": "component_item",
			"fieldtype": "Link",
			"options": "Item",
			"width": 120,
		},
		{"label": _("Batch"), "fieldname": "batch_no", "fieldtype": "Data", "width": 160},
		{"label": _("Issued Qty"), "fieldname": "issued_qty", "fieldtype": "Float", "width": 90},
		{"label": _("Returned Qty"), "fieldname": "returned_qty", "fieldtype": "Float", "width": 90},
		{
			"label": _("Manufacture Consumed Qty"),
			"fieldname": "manufacture_consumed_qty",
			"fieldtype": "Float",
			"width": 120,
		},
		{
			"label": _("Component Scrap Qty"),
			"fieldname": "component_scrap_qty",
			"fieldtype": "Float",
			"width": 110,
		},
		{
			"label": _("Other Proven Outflow"),
			"fieldname": "other_proven_outflow",
			"fieldtype": "Float",
			"width": 120,
		},
		{
			"label": _("Remaining WIP"),
			"fieldname": "remaining_wip",
			"fieldtype": "Float",
			"width": 100,
		},
		{
			"label": _("Golden Difference"),
			"fieldname": "golden_difference",
			"fieldtype": "Float",
			"width": 110,
		},
		{
			"label": _("Golden Status"),
			"fieldname": "golden_status",
			"fieldtype": "Data",
			"width": 150,
		},
		{
			"label": _("Manufacture Count"),
			"fieldname": "manufacture_count",
			"fieldtype": "Int",
			"width": 110,
		},
		{
			"label": _("Material Issue Count"),
			"fieldname": "material_issue_count",
			"fieldtype": "Int",
			"width": 120,
		},
		{
			"label": _("Merge Review"),
			"fieldname": "merge_review",
			"fieldtype": "Data",
			"width": 90,
		},
		{
			"label": _("Material Issue Review"),
			"fieldname": "material_issue_review",
			"fieldtype": "Data",
			"width": 140,
		},
		{
			"label": _("Suggested Disposition"),
			"fieldname": "suggested_disposition",
			"fieldtype": "Data",
			"width": 130,
		},
		{
			"label": _("Confidence"),
			"fieldname": "confidence",
			"fieldtype": "Data",
			"width": 90,
		},
		{
			"label": _("Repair Status"),
			"fieldname": "repair_status",
			"fieldtype": "Data",
			"width": 110,
		},
		{
			"label": _("Posting Date"),
			"fieldname": "posting_date",
			"fieldtype": "Date",
			"width": 100,
		},
		{
			"label": _("Job Card Summary"),
			"fieldname": "job_card_summary",
			"fieldtype": "Data",
			"width": 140,
		},
		{
			"label": _("Open Rebuild"),
			"fieldname": "open_rebuild",
			"fieldtype": "Data",
			"width": 110,
		},
	]

# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

from __future__ import annotations

import json

import frappe
from frappe.utils import cint


def report_one(name: str) -> dict:
	from erpnext_extensions.iran_accounting.fg_cost_allocation.contract import (
		FGCostAllocationNotConsumable,
		assert_consumable,
	)

	doc = frappe.get_doc("Monthly FG Cost Calculation", name)
	rev = cint(doc.revision)
	aq = frappe.db.sql(
		"""
		SELECT COALESCE(SUM(actual_qty),0), COALESCE(SUM(normalized_qty),0)
		FROM `tabMFG Cost Product Allocation`
		WHERE monthly_calculation=%s AND revision=%s
		""",
		(name, rev),
	)[0]
	configured = frappe.db.count(
		"FG Item Primary UOM",
		{"parent": frappe.db.get_value("Finished Goods Cost Allocation Settings", {"company": doc.company}, "name")},
	)
	try:
		assert_consumable(name)
		consumable, blocker = True, None
	except FGCostAllocationNotConsumable as ex:
		consumable, blocker = False, str(ex)

	# blockers from exceptions_json
	exc = {}
	try:
		exc = json.loads(doc.exceptions_json or "{}")
	except Exception:
		exc = {"raw": doc.exceptions_json}

	return {
		"name": doc.name,
		"persian_period": doc.persian_period or f"{cint(doc.persian_year):04d}-{cint(doc.persian_month):02d}",
		"status": doc.status,
		"revision": rev,
		"is_consistent": cint(doc.is_consistent),
		"configured_item_primary_uoms": configured,
		"eligible_source_row_count": cint(doc.eligible_source_row_count),
		"batch_count": cint(doc.batch_count),
		"product_count": cint(doc.product_count),
		"actual_qty_sum": float(aq[0] or 0),
		"normalized_qty_sum": float(aq[1] or 0),
		"missing_uom_count": cint(doc.missing_uom_count),
		"uom_exception_rows": frappe.db.count(
			"MFG Cost Product Allocation",
			{"monthly_calculation": name, "revision": rev, "missing_uom_factor": 1},
		),
		"pool_count": cint(doc.pool_count),
		"gl_entry_count": cint(doc.gl_entry_count),
		"total_identified_expenses": float(doc.total_identified_expenses or 0),
		"total_allocated_expenses": float(doc.total_allocated_expenses or 0),
		"batch_expense_count": frappe.db.count(
			"MFG Cost Batch Expense Allocation", {"monthly_calculation": name, "revision": rev}
		),
		"product_expense_active_rev": frappe.db.count(
			"MFG Cost Product Expense Allocation", {"monthly_calculation": name, "revision": rev}
		),
		"ads_count": frappe.db.count(
			"MFG Cost Account Dimension Summary", {"monthly_calculation": name, "revision": rev}
		),
		"share_variance": float(doc.share_variance or 0),
		"over_allocated": cint(doc.over_allocated),
		"shares_are_provisional": cint(getattr(doc, "shares_are_provisional", 0)),
		"consumable": consumable,
		"consumable_blocker": blocker,
		"blocking_exceptions_sample": (exc.get("blocking") or [])[:10],
	}


def report_all_fy1405() -> dict:
	rows = []
	for cal in frappe.get_all(
		"Monthly FG Cost Calculation",
		filters={"persian_year": 1405},
		pluck="name",
		order_by="persian_month",
	):
		rows.append(report_one(cal))
	return {"months": rows, "product_expense_total": frappe.db.count("MFG Cost Product Expense Allocation")}


def regen_month6() -> dict:
	from erpnext_extensions.iran_accounting.fg_cost_allocation.engine import run_calculation

	name = frappe.db.get_value(
		"Monthly FG Cost Calculation", {"persian_year": 1405, "persian_month": 6}, "name"
	)
	if not name:
		return {"error": "month 6 missing"}
	# bump wait for large month
	frappe.db.sql("SET SESSION wait_timeout=28800")
	frappe.db.sql("SET SESSION net_read_timeout=600")
	frappe.db.sql("SET SESSION net_write_timeout=600")
	msg = run_calculation(name, retain_overrides=False, action="Calculate")
	return {"engine": msg, "report": report_one(name)}

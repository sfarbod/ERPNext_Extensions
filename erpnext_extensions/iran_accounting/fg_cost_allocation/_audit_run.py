# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Audit helpers for Finished Goods Cost Allocation (dev only)."""

from __future__ import annotations

import json
from collections import defaultdict
from decimal import Decimal

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.fg_cost_allocation.engine import run_calculation
from erpnext_extensions.iran_accounting.fg_cost_allocation.expense_rules import is_admin_staff_cost_center
from erpnext_extensions.iran_accounting.fg_cost_allocation.period import resolve_persian_month
from erpnext_extensions.iran_accounting.fg_cost_allocation.production_extract import fetch_eligible_fg_rows

COMPANY = "اسپاد فارمد دارو"


def recalc_fy1405():
	names = frappe.get_all(
		"Monthly FG Cost Calculation",
		filters={"persian_year": 1405},
		pluck="name",
		order_by="persian_month",
	)
	return [run_calculation(n, action="Calculate") for n in names]


def collect_audit():
	settings_name = frappe.db.get_value(
		"Finished Goods Cost Allocation Settings", {"company": COMPANY}, "name"
	)
	settings = frappe.get_doc("Finished Goods Cost Allocation Settings", settings_name)
	uom_rows = frappe.get_all(
		"FG Item Primary UOM",
		filters={"parent": settings.name},
		fields=[
			"item",
			"item_name",
			"stock_uom",
			"primary_uom",
			"conversion_factor",
			"conversion_status",
		],
		order_by="item",
	)
	status_counts: dict[str, int] = defaultdict(int)
	for r in uom_rows:
		status_counts[r.conversion_status or ""] += 1

	accounts_6 = frappe.db.sql(
		"""
		SELECT name, account_number, account_name
		FROM `tabAccount`
		WHERE company=%s AND is_group=0 AND account_number LIKE '6%%'
		ORDER BY account_number
		""",
		COMPANY,
		as_dict=True,
	)

	rules = frappe.get_all(
		"Expense Account Dimension Rule",
		filters={"parent": settings.name},
		fields=["name", "account", "cost_center", "department", "enabled"],
	)

	months = frappe.get_all(
		"Monthly FG Cost Calculation",
		filters={"persian_year": 1405, "company": COMPANY},
		fields=[
			"name",
			"persian_month",
			"persian_month_name",
			"start_date",
			"end_date",
			"revision",
			"status",
			"is_consistent",
			"shares_are_provisional",
			"total_identified_expenses",
			"total_allocated_expenses",
			"unallocated_amount",
			"excluded_gl_amount",
			"total_monthly_normalized_qty",
			"gl_entry_count",
			"batch_count",
			"product_count",
			"pool_count",
			"rule_count",
			"exception_count",
			"share_variance",
			"eligible_source_row_count",
			"reconciliation_summary",
		],
		order_by="persian_month",
	)

	prod_recon = []
	uom_exceptions = []
	for m in months:
		period = resolve_persian_month(COMPANY, 1405, int(m.persian_month))
		rows = fetch_eligible_fg_rows(COMPANY, period)
		actual_sum = sum(Decimal(str(flt(r.qty))) for r in rows)
		batch_actual = frappe.db.sql(
			"""
			SELECT COALESCE(SUM(actual_qty),0)
			FROM `tabMFG Cost Batch Allocation`
			WHERE monthly_calculation=%s AND revision=%s
			""",
			(m.name, m.revision),
		)[0][0]
		pool_net = frappe.db.sql(
			"""
			SELECT COALESCE(SUM(monthly_net_expense),0)
			FROM `tabMFG Cost Expense Pool`
			WHERE monthly_calculation=%s AND revision=%s
			""",
			(m.name, m.revision),
		)[0][0]
		batch_alloc = frappe.db.sql(
			"""
			SELECT COALESCE(SUM(total_allocated_expense),0)
			FROM `tabMFG Cost Batch Allocation`
			WHERE monthly_calculation=%s AND revision=%s
			""",
			(m.name, m.revision),
		)[0][0]
		product_alloc = frappe.db.sql(
			"""
			SELECT COALESCE(SUM(product_allocated_expense),0)
			FROM `tabMFG Cost Product Allocation`
			WHERE monthly_calculation=%s AND revision=%s
			""",
			(m.name, m.revision),
		)[0][0]
		recon = json.loads(m.reconciliation_summary or "{}")
		prod_recon.append(
			{
				"month": m.persian_month,
				"name": m.name,
				"source_rows": len(rows),
				"source_actual_qty": float(actual_sum),
				"persisted_batch_actual_qty": float(batch_actual or 0),
				"eligible_source_row_count": m.eligible_source_row_count,
				"normalized_qty": m.total_monthly_normalized_qty,
				"exception_count": m.exception_count,
				"pool_net": float(pool_net or 0),
				"header_identified": float(m.total_identified_expenses or 0),
				"batch_allocated": float(batch_alloc or 0),
				"product_allocated": float(product_alloc or 0),
				"recon_ok": recon.get("ok"),
				"uom_complete": recon.get("uom_complete"),
			}
		)
		miss = frappe.get_all(
			"MFG Cost Product Allocation",
			filters={
				"monthly_calculation": m.name,
				"revision": m.revision,
				"missing_uom_factor": 1,
			},
			fields=[
				"item_code",
				"item_name",
				"uom",
				"normalization_uom",
				"normalization_factor",
				"actual_qty",
				"normalized_qty",
				"batch_no",
				"work_order",
				"stock_entry",
			],
		)
		for x in miss:
			uom_exceptions.append(
				{
					"persian_month": m.persian_month,
					"monthly": m.name,
					**x,
					"exception_reason": "Missing/invalid Item UOM conversion to primary UOM",
				}
			)

	ccs = frappe.get_all(
		"Cost Center",
		filters={"company": COMPANY, "is_group": 0},
		fields=["name"],
	)
	excluded_ccs = [c.name for c in ccs if is_admin_staff_cost_center(c.name)]

	module = frappe.db.get_value("Module Def", {"module_name": "Costing"}, ["name", "app_name"], as_dict=True)
	doctypes = frappe.get_all(
		"DocType",
		filters={"module": "Costing"},
		pluck="name",
		order_by="name",
	)

	# three removed seed rules — from revision history / audit note
	removed_seed_rules = [
		{
			"note": "Removed blank Cost Center seed rules (confirmed prior to generation)",
			"source": "Settings expense_rules before Generate Department Rules",
		}
	]
	# Try to recover exact combos from revision log / error log if available
	seed_log = frappe.db.sql(
		"""
		SELECT creation, error FROM `tabError Log`
		WHERE error LIKE %s
		ORDER BY creation DESC LIMIT 5
		""",
		("%blank%seed%rule%",),
		as_dict=True,
	)

	out = {
		"company": COMPANY,
		"settings": settings.name,
		"module": module,
		"costing_doctypes": doctypes,
		"uom_status_counts": dict(status_counts),
		"uom_valid_items": [
			{
				"item_code": r.item,
				"item_name": r.item_name,
				"stock_uom": r.stock_uom,
				"primary_uom": r.primary_uom,
				"factor": r.conversion_factor,
			}
			for r in uom_rows
			if r.conversion_status == "Valid"
		],
		"uom_missing_items_count": status_counts.get("Missing", 0),
		"accounts_starting_6_count": len(accounts_6),
		"accounts_starting_6": accounts_6,
		"rule_count": len(rules),
		"enabled_rule_count": sum(1 for r in rules if r.enabled),
		"excluded_admin_staff_cost_centers": excluded_ccs,
		"removed_seed_rules": removed_seed_rules,
		"seed_error_logs": seed_log,
		"months": [
			{k: m.get(k) for k in m.keys() if k != "reconciliation_summary"}
			for m in months
		],
		"production_reconciliation": prod_recon,
		"uom_exception_count": len(uom_exceptions),
		"uom_exception_items": sorted({x["item_code"] for x in uom_exceptions}),
		"uom_exceptions": uom_exceptions,
		"version": frappe.get_attr("erpnext_extensions.__version__")
		if frappe.get_module("erpnext_extensions")
		else None,
	}
	try:
		import erpnext_extensions

		out["version"] = getattr(erpnext_extensions, "__version__", None)
	except Exception:
		pass

	return out

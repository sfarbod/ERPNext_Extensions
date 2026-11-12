"""Final Farvardin/Ordibehesht recalc + UOM/consumable/export evidence after 20100213 fix."""

from __future__ import annotations

import json
from pathlib import Path

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.fg_cost_allocation.contract import is_consumable
from erpnext_extensions.iran_accounting.fg_cost_allocation.engine import calculate_monthly
from erpnext_extensions.iran_accounting.fg_cost_allocation.export_costing import (
	_csv_bytes,
	_filename,
	_matrix,
	get_detail_counts,
)


COMPANY = "اسپاد فارمد دارو"
OUT = Path("/tmp/costing_qa_v5727/final_recalc.json")


def _uom_evidence():
	row = frappe.db.get_value(
		"FG Item Primary UOM",
		{"item": "20100213"},
		[
			"name",
			"parent",
			"item",
			"stock_uom",
			"primary_uom",
			"source_conversion_factor",
			"manual_override_factor",
			"conversion_factor",
			"conversion_status",
			"conversion_source",
			"override_reason",
		],
		as_dict=True,
	)
	item_convs = frappe.get_all(
		"UOM Conversion Detail",
		filters={"parent": "20100213"},
		fields=["uom", "conversion_factor"],
	)
	return {
		"correction_location": "Finished Goods Cost Allocation Settings → FG Item Primary UOM (Costing-specific)",
		"not_item_master": True,
		"item_uom_conversion_detail": item_convs,
		"fg_row": row,
		"packaging_note": "Manual Override factor 2.0: 1 BOX(2Vial) → 2 BOX(1pfs) normalized units",
	}


def _snap(name):
	doc = frappe.get_doc("Monthly FG Cost Calculation", name)
	prod_20100213 = frappe.get_all(
		"MFG Cost Product Allocation",
		filters={"monthly_calculation": name, "revision": doc.revision, "item_code": "20100213"},
		fields=[
			"batch_no",
			"actual_qty",
			"normalized_qty",
			"normalization_factor",
			"missing_uom_factor",
			"product_share",
			"material_consumption_value",
			"product_allocated_expense",
			"total_product_cost",
			"unit_cost_on_actual",
			"unit_cost_on_normalized",
			"selling_price",
			"price_status",
		],
	)
	return {
		"name": name,
		"persian_period": doc.persian_period,
		"status": doc.status,
		"revision": doc.revision,
		"is_consistent": doc.is_consistent,
		"shares_are_provisional": doc.shares_are_provisional,
		"missing_uom_count": doc.missing_uom_count,
		"exception_count": doc.exception_count,
		"identified": flt(doc.total_identified_expenses),
		"allocated": flt(doc.total_allocated_expenses),
		"unallocated": flt(doc.unallocated_amount),
		"identified_eq_allocated": abs(flt(doc.total_identified_expenses) - flt(doc.total_allocated_expenses))
		< 0.05,
		"pools": doc.pool_count,
		"batches": doc.batch_count,
		"products": doc.product_count,
		"gl": doc.gl_entry_count,
		"consumable": is_consumable(name),
		"detail_counts": get_detail_counts(name, doc.revision),
		"product_20100213": prod_20100213,
		"selling_price_list": frappe.db.get_value(
			"Finished Goods Cost Allocation Settings", {"company": COMPANY}, "selling_price_list"
		),
	}


def _export_smoke(name, period):
	rev = frappe.db.get_value("Monthly FG Cost Calculation", name, "revision")
	rows = frappe.get_all(
		"MFG Cost Reconciliation Check",
		filters={"monthly_calculation": name, "revision": rev},
		fields=["name", "check_name", "status", "expected_value", "actual_value", "difference"],
		limit_page_length=5000,
	)
	matrix = _matrix("MFG Cost Reconciliation Check", rows)
	raw = _csv_bytes(matrix)
	fname = _filename("MFG Cost Reconciliation Check", name, "csv", period=period)
	exc_n = frappe.db.count("MFG Cost Exception", {"monthly_calculation": name, "revision": rev})
	bx_n = frappe.db.count(
		"MFG Cost Batch Expense Allocation", {"monthly_calculation": name, "revision": rev}
	)
	return {
		"recon_export_filename": fname,
		"recon_export_rows": len(rows),
		"recon_csv_bytes": len(raw),
		"exceptions": exc_n,
		"batch_expense_rows": bx_n,
		"row_count_matches_db": len(rows)
		== frappe.db.count("MFG Cost Reconciliation Check", {"monthly_calculation": name, "revision": rev}),
	}


def _run_month(period_month: int):
	name = frappe.db.get_value(
		"Monthly FG Cost Calculation",
		{"company": COMPANY, "persian_year": 1405, "persian_month": period_month},
		"name",
	)
	src_before = {
		"GL Entry": frappe.db.count("GL Entry"),
		"rules": frappe.db.count("Expense Account Dimension Rule"),
	}
	first = calculate_monthly(name, retain_overrides=0)
	snap1 = _snap(name)
	second = calculate_monthly(name, retain_overrides=1)
	snap2 = _snap(name)
	src_after = {
		"GL Entry": frappe.db.count("GL Entry"),
		"rules": frappe.db.count("Expense Account Dimension Rule"),
	}
	idempotent = (
		snap1["identified"] == snap2["identified"]
		and snap1["allocated"] == snap2["allocated"]
		and snap1["pools"] == snap2["pools"]
		and snap1["gl"] == snap2["gl"]
		and snap1["products"] == snap2["products"]
	)
	return {
		"name": name,
		"first_calc": first,
		"first": snap1,
		"second": snap2,
		"idempotent": idempotent,
		"source_unchanged": src_before == src_after,
		"export": _export_smoke(name, snap2["persian_period"]),
	}


def main():
	report = {
		"version": __import__("erpnext_extensions").__version__,
		"uom_20100213": _uom_evidence(),
		"farvardin": _run_month(1),
		"ordibehesht": _run_month(2),
		"rules": frappe.db.count("Expense Account Dimension Rule"),
	}
	OUT.parent.mkdir(parents=True, exist_ok=True)
	OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
	print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
	return report

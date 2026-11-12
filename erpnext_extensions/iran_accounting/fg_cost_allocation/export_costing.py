# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Server-side CSV/XLSX export for Costing detail DocTypes and Settings child tables.

Exports all matching rows (not just the visible page), respects filters, and
uses meaningful Costing_* filenames with Persian period when available.
"""

from __future__ import annotations

import csv
import io
import json
import re
from typing import Any

import frappe
from frappe import _
from frappe.utils import cint, flt
from frappe.utils.xlsxutils import make_xlsx

COSTING_EXPORT_DOCTYPES = {
	"MFG Cost Expense Pool": "Expense_Pools",
	"MFG Cost GL Line": "GL_Lines",
	"MFG Cost Batch Allocation": "Batches",
	"MFG Cost Batch Expense Allocation": "Batch_Expense",
	"MFG Cost Product Allocation": "Product_Allocation",
	"MFG Cost Product Expense Allocation": "Product_Expense",
	"MFG Cost Account Dimension Summary": "Account_Dimension",
	"MFG Cost Reconciliation Check": "Reconciliation",
	"MFG Cost Exception": "Exceptions",
	"MFG Cost Calculation Revision": "Revisions",
	"Expense Account Dimension Rule": "Expense_Rules",
	"FG Item Primary UOM": "Item_Primary_UOMs",
	"FG Cost Rule Generation Summary": "Rule_Generation_Summary",
	"FG Cost UOM Validation Summary": "UOM_Validation_Summary",
	"MFG Cost Rule Generation Line": "Rule_Generation_Lines",
}


def _safe_filename_part(value: str | None) -> str:
	text = (value or "").strip() or "NA"
	text = re.sub(r"[^\w.\-]+", "_", text, flags=re.UNICODE)
	return text[:80] or "NA"


def _period_for_monthly(monthly_name: str | None) -> str:
	if not monthly_name:
		return ""
	return frappe.db.get_value("Monthly FG Cost Calculation", monthly_name, "persian_period") or ""


def _value_fields(doctype: str) -> list[dict]:
	meta = frappe.get_meta(doctype)
	skip = {
		"naming_series",
		"amended_from",
		"_user_tags",
		"_comments",
		"_assign",
		"_liked_by",
	}
	out = []
	# Always lead with name for standalone docs
	if not meta.istable:
		out.append({"fieldname": "name", "label": "ID", "fieldtype": "Data"})
	for df in meta.fields:
		if df.fieldtype in frappe.model.no_value_fields:
			continue
		if df.fieldname in skip:
			continue
		out.append({"fieldname": df.fieldname, "label": df.label or df.fieldname, "fieldtype": df.fieldtype})
	return out


def _parse_filters(filters: Any) -> dict | list:
	if not filters:
		return {}
	if isinstance(filters, str):
		filters = json.loads(filters)
	return filters


def _filename(doctype: str, monthly_name: str | None, file_format: str, period: str | None = None) -> str:
	slug = COSTING_EXPORT_DOCTYPES.get(doctype, _safe_filename_part(doctype))
	period = period or _period_for_monthly(monthly_name)
	parts = ["Costing", slug]
	if period:
		parts.append(_safe_filename_part(period))
	elif monthly_name:
		parts.append(_safe_filename_part(monthly_name))
	return "_".join(parts) + f".{file_format}"


def _fetch_rows(doctype: str, filters: dict | list, fields: list[str]) -> list[dict]:
	frappe.has_permission(doctype, "read", throw=True)
	# Chunk to avoid truncating large exports
	rows: list[dict] = []
	page_length = 2000
	start = 0
	while True:
		chunk = frappe.get_all(
			doctype,
			filters=filters,
			fields=fields,
			limit_start=start,
			limit_page_length=page_length,
			order_by="modified desc",
		)
		if not chunk:
			break
		rows.extend(chunk)
		if len(chunk) < page_length:
			break
		start += page_length
	return rows


def _matrix(doctype: str, rows: list[dict]) -> list[list]:
	cols = _value_fields(doctype)
	headers = [c["label"] for c in cols]
	fieldnames = [c["fieldname"] for c in cols]
	matrix = [headers]
	for row in rows:
		matrix.append([row.get(fn) if row.get(fn) is not None else "" for fn in fieldnames])
	return matrix


def _csv_bytes(matrix: list[list]) -> bytes:
	buf = io.StringIO()
	writer = csv.writer(buf)
	for row in matrix:
		writer.writerow(row)
	# UTF-8 BOM for Excel Persian compatibility
	return ("\ufeff" + buf.getvalue()).encode("utf-8")


def _xlsx_bytes(matrix: list[list], sheet_title: str) -> bytes:
	xlsx = make_xlsx(matrix, sheet_title[:31] or "Costing")
	return xlsx.getvalue()


@frappe.whitelist()
def export_costing_doctype(
	doctype: str,
	file_format: str = "csv",
	monthly_calculation: str | None = None,
	revision: int | str | None = None,
	filters: str | dict | list | None = None,
	search: str | None = None,
):
	"""Export all matching Costing rows as CSV or XLSX download."""
	doctype = (doctype or "").strip()
	if doctype not in COSTING_EXPORT_DOCTYPES:
		frappe.throw(_("DocType {0} is not a supported Costing export.").format(doctype))

	file_format = (file_format or "csv").lower()
	if file_format not in ("csv", "xlsx"):
		frappe.throw(_("file_format must be csv or xlsx"))

	parsed = _parse_filters(filters)
	# List View sends filters as [[doctype, field, op, value], ...]
	if isinstance(parsed, list):
		flt_map: dict | list = parsed
		# Also inject monthly/revision when provided as dedicated args
		extra = []
		if monthly_calculation:
			extra.append([doctype, "monthly_calculation", "=", monthly_calculation])
		if revision not in (None, "", "null") and doctype != "MFG Cost Calculation Revision":
			extra.append([doctype, "revision", "=", cint(revision)])
		flt_map = list(parsed) + extra
	else:
		flt_map = dict(parsed or {})
		if monthly_calculation and "monthly_calculation" not in flt_map:
			flt_map["monthly_calculation"] = monthly_calculation
		if revision not in (None, "", "null") and "revision" not in flt_map:
			if doctype != "MFG Cost Calculation Revision":
				flt_map["revision"] = cint(revision)

	# Optional simple name/or search for list views
	or_filters = None
	if search:
		meta = frappe.get_meta(doctype)
		candidates = [df.fieldname for df in meta.fields if df.fieldtype in ("Data", "Link", "Small Text", "Text") ][:5]
		if "name" not in candidates:
			candidates.insert(0, "name")
		or_filters = [[doctype, f, "like", f"%{search}%"] for f in candidates]

	cols = _value_fields(doctype)
	fields = [c["fieldname"] for c in cols]
	# get_all with or_filters
	frappe.has_permission(doctype, "read", throw=True)
	rows: list[dict] = []
	page_length = 2000
	start = 0
	while True:
		chunk = frappe.get_all(
			doctype,
			filters=flt_map,
			or_filters=or_filters,
			fields=fields,
			limit_start=start,
			limit_page_length=page_length,
			order_by="modified desc",
		)
		if not chunk:
			break
		rows.extend(chunk)
		if len(chunk) < page_length:
			break
		start += page_length

	if not rows:
		frappe.msgprint(_("No records to export for the current filters."), indicator="orange")
		return {"row_count": 0, "filename": None}

	matrix = _matrix(doctype, rows)
	period = _period_for_monthly(monthly_calculation if isinstance(flt_map, dict) else None)
	if isinstance(flt_map, dict) and flt_map.get("monthly_calculation"):
		period = period or _period_for_monthly(flt_map.get("monthly_calculation"))
	fname = _filename(doctype, monthly_calculation, file_format, period=period)
	content = _csv_bytes(matrix) if file_format == "csv" else _xlsx_bytes(matrix, COSTING_EXPORT_DOCTYPES[doctype])

	frappe.local.response.filename = fname
	frappe.local.response.filecontent = content
	frappe.local.response.type = "download"
	return {"row_count": len(rows), "filename": fname}


@frappe.whitelist()
def export_settings_child_table(
	settings_name: str,
	child_field: str,
	file_format: str = "csv",
):
	"""Export a Settings child table (all rows) as CSV/XLSX."""
	settings_name = settings_name or frappe.db.get_value(
		"Finished Goods Cost Allocation Settings", {}, "name"
	)
	if not settings_name:
		frappe.throw(_("Finished Goods Cost Allocation Settings not found."))
	doc = frappe.get_doc("Finished Goods Cost Allocation Settings", settings_name)
	doc.check_permission("read")
	child_field = (child_field or "").strip()
	rows = doc.get(child_field) or []
	if not rows:
		frappe.msgprint(_("No records to export."), indicator="orange")
		return {"row_count": 0}

	df = doc.meta.get_field(child_field)
	if not df or df.fieldtype != "Table":
		frappe.throw(_("Field {0} is not a child table.").format(child_field))
	child_dt = df.options
	matrix = _matrix(child_dt, [r.as_dict() for r in rows])
	slug_map = {
		"item_primary_uoms": "Item_Primary_UOMs",
		"expense_account_dimension_rules": "Expense_Rules",
		"rule_generation_summary": "Rule_Generation_Summary",
		"uom_validation_summary": "UOM_Validation_Summary",
	}
	slug = slug_map.get(child_field, _safe_filename_part(child_field))
	file_format = (file_format or "csv").lower()
	fname = f"Costing_{slug}.{file_format}"
	content = _csv_bytes(matrix) if file_format == "csv" else _xlsx_bytes(matrix, slug[:31])
	frappe.local.response.filename = fname
	frappe.local.response.filecontent = content
	frappe.local.response.type = "download"
	return {"row_count": len(rows), "filename": fname}


@frappe.whitelist()
def get_detail_counts(monthly_name: str, revision: int | str | None = None) -> dict:
	"""Accurate status counts for Monthly form summary panels (not limited to 30)."""
	if not monthly_name:
		frappe.throw(_("monthly_name is required"))
	frappe.has_permission("Monthly FG Cost Calculation", "read", throw=True)
	rev = cint(revision) if revision not in (None, "") else cint(
		frappe.db.get_value("Monthly FG Cost Calculation", monthly_name, "revision")
	)
	base = {"monthly_calculation": monthly_name, "revision": rev}

	def _count(dt, extra=None):
		f = dict(base)
		if extra:
			f.update(extra)
		return frappe.db.count(dt, f)

	recon = frappe.db.sql(
		"""
		SELECT status, COUNT(*) AS c
		FROM `tabMFG Cost Reconciliation Check`
		WHERE monthly_calculation=%s AND revision=%s
		GROUP BY status
		""",
		(monthly_name, rev),
		as_dict=True,
	)
	exc = frappe.db.sql(
		"""
		SELECT severity, COUNT(*) AS c
		FROM `tabMFG Cost Exception`
		WHERE monthly_calculation=%s AND revision=%s
		GROUP BY severity
		""",
		(monthly_name, rev),
		as_dict=True,
	)
	return {
		"revision": rev,
		"expense_pool": _count("MFG Cost Expense Pool"),
		"gl_line": _count("MFG Cost GL Line"),
		"batch": _count("MFG Cost Batch Allocation"),
		"batch_expense": _count("MFG Cost Batch Expense Allocation"),
		"product": _count("MFG Cost Product Allocation"),
		"account_dimension": _count("MFG Cost Account Dimension Summary"),
		"reconciliation": {r.status: r.c for r in recon},
		"reconciliation_total": sum(r.c for r in recon),
		"exceptions": {r.severity: r.c for r in exc},
		"exceptions_total": sum(r.c for r in exc),
		"revisions": frappe.db.count("MFG Cost Calculation Revision", {"monthly_calculation": monthly_name}),
	}

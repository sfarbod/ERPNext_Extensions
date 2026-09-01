# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Selling documents DECIMAL(30,9) allowlist + migrate-safe helpers (v5.0.7).

Authoritative monetary storage hardening for Sales Order, Delivery Note, and
Sales Invoice parent/child DocTypes. Supersedes partial approved_decimal_precision
selling coverage and the v5.0.5 single-field Sales Order commission hotfix.

Does not change Frappe global Currency/Float mapping or ERPNext business logic.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any

import frappe
from frappe.utils import cint, cstr

from erpnext_extensions.approved_decimal_precision import (
	ALTER_TO_DECIMAL_30_9,
	DECIMAL_COMPATIBLE_FIELDTYPES,
	SKIP_ALREADY_CORRECT,
	SKIP_ALREADY_WIDER,
	SKIP_INCOMPATIBLE_FIELDTYPE,
	SKIP_MISSING_COLUMN,
	SKIP_MISSING_DOCTYPE,
	SKIP_MISSING_FIELD,
	SKIP_MISSING_TABLE,
	TARGET_LENGTH,
	TARGET_PRECISION,
	TARGET_SCALE,
	alter_decimal_column,
	decide_decimal_action,
	desired_metadata_length,
	ensure_length_property_setter,
	read_column_schema,
	table_exists,
)

SELLING_ROOT_DOCTYPES: tuple[str, ...] = ("Sales Order", "Delivery Note", "Sales Invoice")

# Static allowlist from metadata audit — every DB-backed monetary amount in scope.
SELLING_AMOUNT_FIELDS_BY_DOCTYPE: dict[str, tuple[str, ...]] = {
	"Delivery Note": (
		"amount_eligible_for_commission",
		"base_discount_amount",
		"base_grand_total",
		"base_net_total",
		"base_rounded_total",
		"base_rounding_adjustment",
		"base_total",
		"base_total_taxes_and_charges",
		"discount_amount",
		"grand_total",
		"net_total",
		"rounded_total",
		"rounding_adjustment",
		"total",
		"total_commission",
		"total_taxes_and_charges",
	),
	"Delivery Note Item": (
		"amount",
		"base_amount",
		"base_net_amount",
		"base_rate_with_margin",
		"billed_amt",
		"company_total_stock",
		"discount_amount",
		"distributed_discount_amount",
		"net_amount",
		"rate_with_margin",
	),
	"Item Wise Tax Detail": ("amount", "taxable_amount"),
	"PDC Invoice Application": ("amount", "amount_in_pdc_currency", "open_amount"),
	"Payment Schedule": (
		"base_outstanding",
		"base_paid_amount",
		"base_payment_amount",
		"discounted_amount",
		"outstanding",
		"paid_amount",
		"payment_amount",
	),
	"Sales Invoice": (
		"amount_eligible_for_commission",
		"base_change_amount",
		"base_discount_amount",
		"base_grand_total",
		"base_net_total",
		"base_paid_amount",
		"base_rounded_total",
		"base_rounding_adjustment",
		"base_total",
		"base_total_taxes_and_charges",
		"base_write_off_amount",
		"change_amount",
		"custom_sales_growth_discount",
		"custom_total_foc_amount",
		"custom_total_raw_discount_amount",
		"discount_amount",
		"grand_total",
		"loyalty_amount",
		"net_total",
		"outstanding_amount",
		"paid_amount",
		"rounded_total",
		"rounding_adjustment",
		"total",
		"total_advance",
		"total_billing_amount",
		"total_commission",
		"total_taxes_and_charges",
		"write_off_amount",
	),
	"Sales Invoice Advance": ("advance_amount", "allocated_amount", "exchange_gain_loss"),
	"Sales Invoice Item": (
		"amount",
		"base_amount",
		"base_net_amount",
		"base_rate_with_margin",
		"company_total_stock",
		"custom_additional_discount_share",
		"custom_base_amount_for_payment",
		"custom_net_amount_for_payment",
		"custom_raw_discount_amount",
		"discount_amount",
		"distributed_discount_amount",
		"net_amount",
		"rate_with_margin",
	),
	"Sales Invoice Payment": ("amount", "base_amount"),
	"Sales Invoice Timesheet": ("billing_amount",),
	"Sales Order": (
		"advance_paid",
		"amount_eligible_for_commission",
		"base_discount_amount",
		"base_grand_total",
		"base_net_total",
		"base_rounded_total",
		"base_rounding_adjustment",
		"base_total",
		"base_total_taxes_and_charges",
		"discount_amount",
		"grand_total",
		"loyalty_amount",
		"net_total",
		"rounded_total",
		"rounding_adjustment",
		"total",
		"total_commission",
		"total_taxes_and_charges",
	),
	"Sales Order Item": (
		"amount",
		"base_amount",
		"base_net_amount",
		"base_rate_with_margin",
		"billed_amt",
		"company_total_stock",
		"discount_amount",
		"distributed_discount_amount",
		"gross_profit",
		"net_amount",
		"rate_with_margin",
	),
	"Sales Taxes and Charges": (
		"base_net_amount",
		"base_tax_amount",
		"base_tax_amount_after_discount_amount",
		"base_total",
		"net_amount",
		"tax_amount",
		"tax_amount_after_discount_amount",
		"total",
	),
	"Sales Team": ("allocated_amount", "incentives"),
	"Tax Withholding Entry": ("taxable_amount", "withholding_amount"),
}

EXCLUDED_RATE_PERCENT_FIELDS_BY_DOCTYPE: dict[str, tuple[str, ...]] = {
	"Delivery Note": (
		"additional_discount_percentage",
		"commission_rate",
		"conversion_rate",
		"per_billed",
		"per_installed",
		"per_returned",
		"plc_conversion_rate",
		"total_net_weight",
		"total_qty",
	),
	"Delivery Note Item": (
		"actual_batch_qty",
		"actual_qty",
		"base_net_rate",
		"base_price_list_rate",
		"base_rate",
		"conversion_factor",
		"discount_percentage",
		"incoming_rate",
		"installed_qty",
		"margin_rate_or_amount",
		"net_rate",
		"packed_qty",
		"price_list_rate",
		"qty",
		"rate",
		"received_qty",
		"returned_qty",
		"stock_qty",
		"stock_uom_rate",
		"total_weight",
		"weight_per_unit",
	),
	"Item Wise Tax Detail": ("rate",),
	"PDC Invoice Application": ("fx_rate",),
	"Packed Item": (
		"actual_batch_qty",
		"actual_qty",
		"conversion_factor",
		"incoming_rate",
		"ordered_qty",
		"packed_qty",
		"picked_qty",
		"projected_qty",
		"qty",
		"rate",
		"requested_qty",
	),
	"Payment Schedule": ("discount", "invoice_portion"),
	"Pricing Rule Detail": ("rate_or_discount",),
	"Sales Invoice": (
		"additional_discount_percentage",
		"commission_rate",
		"conversion_rate",
		"plc_conversion_rate",
		"total_billing_hours",
		"total_net_weight",
		"total_qty",
	),
	"Sales Invoice Advance": ("ref_exchange_rate",),
	"Sales Invoice Item": (
		"actual_batch_qty",
		"actual_qty",
		"base_net_rate",
		"base_price_list_rate",
		"base_rate",
		"conversion_factor",
		"delivered_qty",
		"discount_percentage",
		"incoming_rate",
		"margin_rate_or_amount",
		"net_rate",
		"price_list_rate",
		"qty",
		"rate",
		"stock_qty",
		"stock_uom_rate",
		"total_weight",
		"weight_per_unit",
	),
	"Sales Invoice Timesheet": ("billing_hours",),
	"Sales Order": (
		"additional_discount_percentage",
		"commission_rate",
		"conversion_rate",
		"per_billed",
		"per_delivered",
		"per_picked",
		"plc_conversion_rate",
		"total_net_weight",
		"total_qty",
	),
	"Sales Order Item": (
		"actual_qty",
		"base_net_rate",
		"base_price_list_rate",
		"base_rate",
		"blanket_order_rate",
		"conversion_factor",
		"delivered_qty",
		"discount_percentage",
		"fg_item_qty",
		"margin_rate_or_amount",
		"net_rate",
		"ordered_qty",
		"picked_qty",
		"planned_qty",
		"price_list_rate",
		"produced_qty",
		"production_plan_qty",
		"projected_qty",
		"qty",
		"rate",
		"requested_qty",
		"returned_qty",
		"stock_qty",
		"stock_reserved_qty",
		"stock_uom_rate",
		"subcontracted_qty",
		"total_weight",
		"valuation_rate",
		"weight_per_unit",
		"work_order_qty",
	),
	"Sales Taxes and Charges": ("rate",),
	"Sales Team": ("allocated_percentage",),
	"Tax Withholding Entry": ("conversion_rate", "tax_rate"),
}

EXCLUDED_VIRTUAL_FIELDS_BY_DOCTYPE: dict[str, tuple[str, ...]] = {
	"Delivery Note": ("last_scanned_warehouse",),
	"Sales Invoice": ("last_scanned_warehouse",),
	"Sales Order": ("last_scanned_warehouse",),
}

RATE_EXACT = frozenset(
	{
		"rate",
		"price_list_rate",
		"base_price_list_rate",
		"stock_uom_rate",
		"net_rate",
		"base_net_rate",
		"incoming_rate",
		"valuation_rate",
		"blanket_order_rate",
		"ref_exchange_rate",
		"fx_rate",
	}
)
RATE_SUBSTR = ("conversion_rate", "plc_conversion_rate", "commission_rate", "tax_rate")
NON_AMOUNT_FLOAT = frozenset({"total_billing_hours", "billing_hours", "discount"})


@dataclass(frozen=True)
class SellingFieldTarget:
	doctype: str
	fieldname: str

	@property
	def table(self) -> str:
		return f"tab{self.doctype}"


def selling_related_doctypes() -> tuple[str, ...]:
	related: set[str] = set(SELLING_ROOT_DOCTYPES)
	changed = True
	while changed:
		changed = False
		for dt in list(related):
			if not frappe.db.exists("DocType", dt):
				continue
			meta = frappe.get_meta(dt, cached=False)
			for df in meta.fields:
				if df.fieldtype == "Table" and df.options and df.options not in related:
					related.add(df.options)
					changed = True
	return tuple(sorted(related))


def selling_field_targets() -> tuple[SellingFieldTarget, ...]:
	return tuple(
		SellingFieldTarget(doctype=doctype, fieldname=fieldname)
		for doctype, fieldnames in SELLING_AMOUNT_FIELDS_BY_DOCTYPE.items()
		for fieldname in fieldnames
	)


def classify_selling_field(df) -> str | None:
	"""Classify Currency/Float/Percent fields for completeness guard."""
	if cint(getattr(df, "is_virtual", 0)):
		return "virtual"
	ft = df.fieldtype
	if ft == "Percent":
		return "rate_pct"
	if ft not in ("Currency", "Float"):
		return None
	fn = (df.fieldname or "").lower()
	if fn in RATE_EXACT:
		return "rate_pct"
	for sub in RATE_SUBSTR:
		if sub in fn:
			return "rate_pct"
	if fn.endswith("_rate") and "amount" not in fn:
		return "rate_pct"
	if fn.endswith("_percentage") or fn in {"discount_percentage", "margin_rate_or_amount", "allocated_percentage"}:
		return "rate_pct"
	if fn in {"invoice_portion", "rate_or_discount"}:
		return "rate_pct"
	if fn.endswith("_qty") or fn in {"qty", "conversion_factor"} or "weight" in fn:
		return "rate_pct"
	if ft == "Float" and fn in NON_AMOUNT_FLOAT:
		return "rate_pct"
	return "amount"


def summarize_results(results: list[dict[str, Any]], *, action_key: str = "action") -> dict[str, Any]:
	changed: list[str] = []
	skipped: list[str] = []
	errors: list[str] = []
	action_counts: Counter[str] = Counter()
	for row in results:
		label = f"{row.get('doctype') or row.get('table')}.{row.get('field')}"
		action = cstr(row.get(action_key) or row.get("metadata_action") or "")
		action_counts[action or "UNKNOWN"] += 1
		if row.get("status") == "error":
			errors.append(label)
		elif action in {ALTER_TO_DECIMAL_30_9, "CREATE_METADATA_LENGTH", "UPDATE_METADATA_LENGTH"}:
			changed.append(label)
		else:
			skipped.append(label)
	return {
		"changed": changed,
		"skipped": skipped,
		"errors": errors,
		"action_counts": dict(action_counts),
		"total": len(results),
	}


def verify_and_set_metadata(logger) -> list[dict[str, Any]]:
	results: list[dict[str, Any]] = []
	for target in selling_field_targets():
		row = {
			"doctype": target.doctype,
			"table": target.table,
			"field": target.fieldname,
			"fieldtype": None,
			"metadata_length": None,
			"metadata_action": None,
			"status": "ok",
		}
		try:
			if not frappe.db.exists("DocType", target.doctype):
				row["metadata_action"] = SKIP_MISSING_DOCTYPE
				results.append(row)
				logger.warning("Skipping missing DocType %s", target.doctype)
				continue
			meta = frappe.get_meta(target.doctype, cached=False)
			df = meta.get_field(target.fieldname)
			if not df:
				row["metadata_action"] = SKIP_MISSING_FIELD
				results.append(row)
				logger.warning("Skipping missing field %s on %s", target.fieldname, target.doctype)
				continue
			row["fieldtype"] = df.fieldtype
			if df.fieldtype not in DECIMAL_COMPATIBLE_FIELDTYPES:
				row["metadata_action"] = SKIP_INCOMPATIBLE_FIELDTYPE
				results.append(row)
				continue
			column_info = read_column_schema(target.table, target.fieldname)
			target_length = desired_metadata_length(column_info)
			action, final_length = ensure_length_property_setter(
				target.doctype,
				target.fieldname,
				target_length,
				logger,
				validate_fields_for_doctype=False,
			)
			row["metadata_length"] = final_length
			row["metadata_action"] = action
			results.append(row)
		except Exception:
			row["status"] = "error"
			row["metadata_action"] = "METADATA_EXCEPTION"
			row["traceback"] = frappe.get_traceback()
			results.append(row)
			logger.error("Metadata failed for %s.%s\n%s", target.doctype, target.fieldname, row["traceback"])

	summary = summarize_results(results, action_key="metadata_action")
	logger.info(
		"Selling documents metadata summary: total=%s changed=%s skipped=%s errors=%s actions=%s",
		summary["total"],
		len(summary["changed"]),
		len(summary["skipped"]),
		len(summary["errors"]),
		summary["action_counts"],
	)
	return results


def apply_decimal_schema_targets(logger) -> list[dict[str, Any]]:
	results: list[dict[str, Any]] = []
	for target in selling_field_targets():
		row = {
			"doctype": target.doctype,
			"table": target.table,
			"field": target.fieldname,
			"fieldtype": None,
			"before_db_type": None,
			"action": None,
			"after_db_type": None,
			"status": "ok",
		}
		try:
			if not frappe.db.exists("DocType", target.doctype):
				row["action"] = SKIP_MISSING_DOCTYPE
				results.append(row)
				continue
			meta = frappe.get_meta(target.doctype, cached=False)
			df = meta.get_field(target.fieldname)
			if not df:
				row["action"] = SKIP_MISSING_FIELD
				results.append(row)
				continue
			row["fieldtype"] = df.fieldtype
			if df.fieldtype not in DECIMAL_COMPATIBLE_FIELDTYPES:
				row["action"] = SKIP_INCOMPATIBLE_FIELDTYPE
				results.append(row)
				continue
			if not table_exists(target.table):
				row["action"] = SKIP_MISSING_TABLE
				results.append(row)
				continue
			column_info = read_column_schema(target.table, target.fieldname)
			if not column_info:
				row["action"] = SKIP_MISSING_COLUMN
				results.append(row)
				continue
			row["before_db_type"] = column_info.get("COLUMN_TYPE")
			action = decide_decimal_action(column_info)
			row["action"] = action
			if action == ALTER_TO_DECIMAL_30_9:
				alter_decimal_column(target.table, target.fieldname, column_info, logger)
			after_info = read_column_schema(target.table, target.fieldname)
			row["after_db_type"] = after_info.get("COLUMN_TYPE") if after_info else None
			results.append(row)
		except Exception:
			row["status"] = "error"
			row["action"] = "SQL_EXCEPTION"
			row["traceback"] = frappe.get_traceback()
			results.append(row)
			logger.error("Schema update failed for %s.%s\n%s", target.table, target.fieldname, row["traceback"])

	summary = summarize_results(results, action_key="action")
	logger.info(
		"Selling documents schema summary: total=%s changed=%s skipped=%s errors=%s actions=%s",
		summary["total"],
		len(summary["changed"]),
		len(summary["skipped"]),
		len(summary["errors"]),
		summary["action_counts"],
	)
	return results


def assert_schema_targets(logger=None) -> list[dict[str, Any]]:
	logger = logger or frappe.logger("erpnext_extensions.selling_documents_decimal_precision_v507")
	failures: list[dict[str, Any]] = []
	for target in selling_field_targets():
		if not frappe.db.exists("DocType", target.doctype):
			continue
		meta = frappe.get_meta(target.doctype, cached=False)
		if not meta.get_field(target.fieldname):
			continue
		info = read_column_schema(target.table, target.fieldname)
		if not info:
			continue
		action = decide_decimal_action(info)
		if action in {SKIP_ALREADY_CORRECT, SKIP_ALREADY_WIDER}:
			continue
		failures.append(
			{
				"doctype": target.doctype,
				"table": target.table,
				"field": target.fieldname,
				"column_type": info.get("COLUMN_TYPE"),
				"numeric_precision": info.get("NUMERIC_PRECISION"),
				"numeric_scale": info.get("NUMERIC_SCALE"),
				"expected": f"DECIMAL({TARGET_PRECISION},{TARGET_SCALE})",
				"action": action,
			}
		)
	if failures:
		detail = "; ".join(
			f"{r['doctype']}.{r['field']} ({r['table']}.{r['field']}): "
			f"actual={r['column_type']} expected={r['expected']}"
			for r in failures
		)
		raise RuntimeError(f"Selling documents DECIMAL(30,9) schema drift detected: {detail}")
	logger.info("Selling documents schema guard OK for %s fields", len(selling_field_targets()))
	return failures


def assert_field_classification_completeness() -> None:
	"""Fail if any Currency/Float/Percent on the selling graph is unclassified."""
	missing_amount: list[str] = []
	missing_rate: list[str] = []
	missing_virtual: list[str] = []
	unknown: list[str] = []

	for dt in selling_related_doctypes():
		if not frappe.db.exists("DocType", dt):
			continue
		allowlisted = set(SELLING_AMOUNT_FIELDS_BY_DOCTYPE.get(dt, ()))
		excluded_rate = set(EXCLUDED_RATE_PERCENT_FIELDS_BY_DOCTYPE.get(dt, ()))
		excluded_virtual = set(EXCLUDED_VIRTUAL_FIELDS_BY_DOCTYPE.get(dt, ()))
		meta = frappe.get_meta(dt, cached=False)
		for df in meta.fields:
			if df.fieldtype not in ("Currency", "Float", "Percent"):
				continue
			label = f"{dt}.{df.fieldname}"
			cls = classify_selling_field(df)
			if cls == "amount":
				if df.fieldname not in allowlisted:
					missing_amount.append(label)
			elif cls == "rate_pct":
				if df.fieldname not in excluded_rate:
					missing_rate.append(label)
			elif cls == "virtual":
				if df.fieldname not in excluded_virtual:
					missing_virtual.append(label)
			elif cls is None:
				unknown.append(label)

	if missing_amount or missing_rate or missing_virtual or unknown:
		lines = []
		if missing_amount:
			lines.append("Unallowlisted monetary amounts: " + ", ".join(sorted(missing_amount)))
		if missing_rate:
			lines.append("Unclassified rate/percentage fields: " + ", ".join(sorted(missing_rate)))
		if missing_virtual:
			lines.append("Unclassified virtual fields: " + ", ".join(sorted(missing_virtual)))
		if unknown:
			lines.append("Unknown monetary-like fields: " + ", ".join(sorted(unknown)))
		raise AssertionError("Selling documents field classification incomplete:\n" + "\n".join(lines))


def audit_report_rows() -> list[dict[str, Any]]:
	"""Build audit table rows for documentation / tests."""
	rows: list[dict[str, Any]] = []
	for dt in selling_related_doctypes():
		if not frappe.db.exists("DocType", dt):
			continue
		meta = frappe.get_meta(dt, cached=False)
		table = f"tab{dt}"
		for df in meta.fields:
			if df.fieldtype not in ("Currency", "Float", "Percent"):
				continue
			cls = classify_selling_field(df)
			if cls == "amount":
				classification = "Monetary Amount"
			elif cls == "rate_pct":
				classification = "Rate / Percentage — Excluded"
			elif cls == "virtual":
				classification = "Virtual / Non-DB — Excluded"
			else:
				classification = "Unknown"
			info = read_column_schema(table, df.fieldname) if cls != "virtual" else None
			old_type = info.get("COLUMN_TYPE") if info else None
			new_type = old_type
			if classification == "Monetary Amount" and info:
				action = decide_decimal_action(info)
				new_type = f"decimal({TARGET_PRECISION},{TARGET_SCALE})" if action != SKIP_ALREADY_WIDER else old_type
			rows.append(
				{
					"doctype": dt,
					"table": table,
					"field": df.fieldname,
					"fieldtype": df.fieldtype,
					"classification": classification,
					"old_sql": old_type,
					"new_sql": new_type,
				}
			)
	return rows

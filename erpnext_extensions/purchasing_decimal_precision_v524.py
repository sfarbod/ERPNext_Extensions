# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Purchasing + Asset Acquisition DECIMAL(30,9) allowlist + migrate-safe helpers (v5.2.4).

Authoritative monetary storage hardening for the ERPNext purchasing lifecycle:

  Material Request / Supplier Quotation
    → Purchase Order → Purchase Receipt → Purchase Invoice
    → Landed Cost / Subcontracting
    → Asset creation / capitalization / value adjustment / repair

Does not change Frappe global Currency/Float mapping or ERPNext business logic.

Fields already covered by stock_repost_decimal_precision_v518 are listed in
ALREADY_HARDENED_BY_STOCK_REPOST for completeness classification and are
re-asserted by the stock after_migrate healer — not owned here.
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

PURCHASING_ROOT_DOCTYPES: tuple[str, ...] = (
	"Purchase Order",
	"Purchase Receipt",
	"Purchase Invoice",
	"Supplier Quotation",
	"Material Request",
	"Landed Cost Voucher",
	"Subcontracting Order",
	"Subcontracting Receipt",
	"Asset",
	"Asset Capitalization",
	"Asset Value Adjustment",
	"Asset Repair",
)

# New / not-yet-owned monetary amount + rate fields for purchasing & assets.
PURCHASING_MONETARY_FIELDS_BY_DOCTYPE: dict[str, tuple[str, ...]] = {
	"Asset": (
		"net_purchase_amount",
		"purchase_amount",
		"gross_purchase_amount",
		"total_asset_cost",
		"additional_asset_cost",
		"opening_accumulated_depreciation",
		"value_after_depreciation",
	),
	"Asset Finance Book": (
		"expected_value_after_useful_life",
		"value_after_depreciation",
	),
	"Asset Value Adjustment": (
		"current_asset_value",
		"new_asset_value",
		"difference_amount",
	),
	"Asset Capitalization": (
		"stock_items_total",
		"asset_items_total",
		"service_items_total",
		"total_value",
		"target_incoming_rate",
	),
	"Asset Capitalization Asset Item": (
		"asset_value",
		"current_asset_value",
	),
	"Asset Capitalization Stock Item": (
		"amount",
		"valuation_rate",
	),
	"Asset Capitalization Service Item": (
		"amount",
		"rate",
	),
	"Asset Repair": (
		"repair_cost",
		"total_repair_cost",
		"consumed_items_cost",
	),
	"Asset Repair Consumed Item": (
		"total_value",
		"valuation_rate",
	),
	"Asset Repair Purchase Invoice": ("repair_cost",),
	"Asset Depreciation Schedule": (
		"expected_value_after_useful_life",
		"net_purchase_amount",
		"opening_accumulated_depreciation",
		"value_after_depreciation",
	),
	"Depreciation Schedule": (
		"depreciation_amount",
		"accumulated_depreciation_amount",
	),
	"Purchase Order": (
		"base_total",
		"base_net_total",
		"total",
		"net_total",
		"base_taxes_and_charges_added",
		"base_taxes_and_charges_deducted",
		"base_total_taxes_and_charges",
		"taxes_and_charges_added",
		"taxes_and_charges_deducted",
		"total_taxes_and_charges",
		"base_discount_amount",
		"discount_amount",
		"base_grand_total",
		"base_rounding_adjustment",
		"base_rounded_total",
		"grand_total",
		"rounding_adjustment",
		"rounded_total",
		"advance_paid",
	),
	"Purchase Order Item": (
		"amount",
		"base_amount",
		"net_amount",
		"base_net_amount",
		"discount_amount",
		"distributed_discount_amount",
		"billed_amt",
		"rate",
		"base_rate",
		"net_rate",
		"base_net_rate",
		"price_list_rate",
		"base_price_list_rate",
		"stock_uom_rate",
		"last_purchase_rate",
		"blanket_order_rate",
		"rate_with_margin",
		"base_rate_with_margin",
	),
	"Purchase Receipt": (
		"base_total",
		"base_net_total",
		"total",
		"net_total",
		"base_taxes_and_charges_added",
		"base_taxes_and_charges_deducted",
		"base_total_taxes_and_charges",
		"taxes_and_charges_added",
		"taxes_and_charges_deducted",
		"total_taxes_and_charges",
		"base_discount_amount",
		"discount_amount",
		"base_grand_total",
		"base_rounding_adjustment",
		"base_rounded_total",
		"grand_total",
		"rounding_adjustment",
		"rounded_total",
	),
	"Purchase Receipt Item": (
		# Item amounts/valuation already in stock_repost — rates still needed here.
		"rate",
		"base_rate",
		"net_rate",
		"base_net_rate",
		"price_list_rate",
		"base_price_list_rate",
		"stock_uom_rate",
	),
	"Purchase Invoice": (
		"base_total",
		"base_net_total",
		"total",
		"net_total",
		"base_taxes_and_charges_added",
		"base_taxes_and_charges_deducted",
		"base_total_taxes_and_charges",
		"taxes_and_charges_added",
		"taxes_and_charges_deducted",
		"total_taxes_and_charges",
		"base_discount_amount",
		"discount_amount",
		"base_grand_total",
		"base_rounding_adjustment",
		"base_rounded_total",
		"grand_total",
		"rounding_adjustment",
		"rounded_total",
		"total_advance",
		"outstanding_amount",
		"paid_amount",
		"base_paid_amount",
		"write_off_amount",
		"base_write_off_amount",
		"claimed_landed_cost_amount",
		# Site custom PI monetary fields
		"guarantee_deposit",
		"insurance_deposit",
		"retention_money",
		"total_deductions",
		"withholding_tax",
	),
	"Purchase Invoice Item": (
		"rate",
		"base_rate",
		"net_rate",
		"base_net_rate",
		"price_list_rate",
		"base_price_list_rate",
		"stock_uom_rate",
	),
	"Purchase Invoice Advance": (
		"advance_amount",
		"allocated_amount",
		"exchange_gain_loss",
	),
	"Purchase Taxes and Charges": (
		"tax_amount",
		"base_tax_amount",
		"total",
		"base_total",
		"tax_amount_after_discount_amount",
		"base_tax_amount_after_discount_amount",
		"net_amount",
		"base_net_amount",
	),
	"Supplier Quotation": (
		"base_total",
		"base_net_total",
		"total",
		"net_total",
		"base_taxes_and_charges_added",
		"base_taxes_and_charges_deducted",
		"base_total_taxes_and_charges",
		"taxes_and_charges_added",
		"taxes_and_charges_deducted",
		"total_taxes_and_charges",
		"base_discount_amount",
		"discount_amount",
		"base_grand_total",
		"base_rounding_adjustment",
		"base_rounded_total",
		"grand_total",
		"rounding_adjustment",
		"rounded_total",
	),
	"Supplier Quotation Item": (
		"amount",
		"base_amount",
		"net_amount",
		"base_net_amount",
		"discount_amount",
		"distributed_discount_amount",
		"rate",
		"base_rate",
		"net_rate",
		"base_net_rate",
		"price_list_rate",
		"base_price_list_rate",
		"rate_with_margin",
	),
	"Material Request Item": (
		"amount",
		"rate",
		"price_list_rate",
	),
	"Purchase Order Item Supplied": (
		"amount",
		"rate",
	),
	"Subcontracting Order": (
		"total",
		"total_additional_costs",
	),
	"Subcontracting Order Item": (
		"amount",
		"rate",
		"service_cost_per_qty",
		"additional_cost_per_qty",
		"rm_cost_per_qty",
	),
	"Subcontracting Order Supplied Item": (
		"amount",
		"rate",
	),
	"Subcontracting Receipt": (
		"total",
		"total_additional_costs",
	),
	"Advance Payment Ledger Entry": (
		"amount",
		"base_amount",
	),
}

# Monetary unit prices/rates intentionally hardened for large IRR.
PURCHASING_MONETARY_RATE_FIELDS: frozenset[tuple[str, str]] = frozenset(
	{
		("Purchase Order Item", "rate"),
		("Purchase Order Item", "base_rate"),
		("Purchase Order Item", "net_rate"),
		("Purchase Order Item", "base_net_rate"),
		("Purchase Order Item", "price_list_rate"),
		("Purchase Order Item", "base_price_list_rate"),
		("Purchase Order Item", "stock_uom_rate"),
		("Purchase Order Item", "last_purchase_rate"),
		("Purchase Order Item", "blanket_order_rate"),
		("Purchase Order Item", "rate_with_margin"),
		("Purchase Order Item", "base_rate_with_margin"),
		("Purchase Receipt Item", "rate"),
		("Purchase Receipt Item", "base_rate"),
		("Purchase Receipt Item", "net_rate"),
		("Purchase Receipt Item", "base_net_rate"),
		("Purchase Receipt Item", "price_list_rate"),
		("Purchase Receipt Item", "base_price_list_rate"),
		("Purchase Receipt Item", "stock_uom_rate"),
		("Purchase Invoice Item", "rate"),
		("Purchase Invoice Item", "base_rate"),
		("Purchase Invoice Item", "net_rate"),
		("Purchase Invoice Item", "base_net_rate"),
		("Purchase Invoice Item", "price_list_rate"),
		("Purchase Invoice Item", "base_price_list_rate"),
		("Purchase Invoice Item", "stock_uom_rate"),
		("Supplier Quotation Item", "rate"),
		("Supplier Quotation Item", "base_rate"),
		("Supplier Quotation Item", "net_rate"),
		("Supplier Quotation Item", "base_net_rate"),
		("Supplier Quotation Item", "price_list_rate"),
		("Supplier Quotation Item", "base_price_list_rate"),
		("Supplier Quotation Item", "rate_with_margin"),
		("Material Request Item", "rate"),
		("Material Request Item", "price_list_rate"),
		("Purchase Order Item Supplied", "rate"),
		("Subcontracting Order Item", "rate"),
		("Subcontracting Order Item", "service_cost_per_qty"),
		("Subcontracting Order Item", "additional_cost_per_qty"),
		("Subcontracting Order Item", "rm_cost_per_qty"),
		("Subcontracting Order Supplied Item", "rate"),
		("Asset Capitalization", "target_incoming_rate"),
		("Asset Capitalization Stock Item", "valuation_rate"),
		("Asset Capitalization Service Item", "rate"),
		("Asset Repair Consumed Item", "valuation_rate"),
	}
)

# Owned by stock_repost_decimal_precision_v518 — completeness only.
ALREADY_HARDENED_BY_STOCK_REPOST: dict[str, tuple[str, ...]] = {
	"Purchase Receipt Item": (
		"amount",
		"base_amount",
		"net_amount",
		"base_net_amount",
		"discount_amount",
		"distributed_discount_amount",
		"billed_amt",
		"item_tax_amount",
		"landed_cost_voucher_amount",
		"rm_supp_cost",
		"amount_difference_with_purchase_invoice",
		"rate_with_margin",
		"base_rate_with_margin",
		"valuation_rate",
		"sales_incoming_rate",
	),
	"Purchase Invoice Item": (
		"amount",
		"base_amount",
		"net_amount",
		"base_net_amount",
		"discount_amount",
		"distributed_discount_amount",
		"item_tax_amount",
		"landed_cost_voucher_amount",
		"rm_supp_cost",
		"rate_with_margin",
		"base_rate_with_margin",
		"valuation_rate",
		"sales_incoming_rate",
	),
	"Purchase Receipt Item Supplied": ("rate", "amount"),
	"Landed Cost Voucher": ("total_taxes_and_charges", "total_vendor_invoices_cost"),
	"Landed Cost Item": ("amount", "applicable_charges", "rate"),
	"Landed Cost Purchase Receipt": ("grand_total",),
	"Landed Cost Taxes and Charges": ("amount", "base_amount"),
	"Payment Schedule": (
		"base_outstanding",
		"base_paid_amount",
		"base_payment_amount",
		"discounted_amount",
		"outstanding",
		"paid_amount",
		"payment_amount",
	),
	"Subcontracting Receipt Item": (
		"rate",
		"amount",
		"rm_cost_per_qty",
		"service_cost_per_qty",
		"additional_cost_per_qty",
		"rm_supp_cost",
		"landed_cost_voucher_amount",
		"secondary_items_cost_per_qty",
	),
	"Subcontracting Receipt Supplied Item": ("rate", "amount"),
}

EXCLUDED_RATE_PERCENT_QTY_FIELDS_BY_DOCTYPE: dict[str, tuple[str, ...]] = {
	"Asset": ("custom_approximate_weight",),
	"Asset Finance Book": ("rate_of_depreciation", "salvage_value_percentage"),
	"Asset Depreciation Schedule": ("rate_of_depreciation",),
	"Asset Capitalization Stock Item": ("stock_qty", "actual_qty"),
	"Asset Capitalization Service Item": ("qty",),
	"Asset Repair Consumed Item": ("current_available_qty",),
	"Asset Shift Factor": ("shift_factor",),
	"Request for Quotation Item": ("qty", "conversion_factor", "stock_qty"),
	"Purchase Order": (
		"conversion_rate",
		"plc_conversion_rate",
		"total_qty",
		"total_net_weight",
		"additional_discount_percentage",
		"per_received",
		"per_billed",
	),
	"Purchase Order Item": (
		"qty",
		"conversion_factor",
		"stock_qty",
		"received_qty",
		"returned_qty",
		"actual_qty",
		"company_total_stock",
		"fg_item_qty",
		"subcontracted_qty",
		"weight_per_unit",
		"total_weight",
		"discount_percentage",
		"margin_rate_or_amount",
	),
	"Purchase Receipt": (
		"conversion_rate",
		"plc_conversion_rate",
		"total_qty",
		"total_net_weight",
		"additional_discount_percentage",
		"per_billed",
		"per_returned",
	),
	"Purchase Receipt Item": (
		"qty",
		"received_qty",
		"rejected_qty",
		"returned_qty",
		"stock_qty",
		"received_stock_qty",
		"conversion_factor",
		"discount_percentage",
		"margin_rate_or_amount",
		"weight_per_unit",
		"total_weight",
	),
	"Purchase Invoice": (
		"conversion_rate",
		"plc_conversion_rate",
		"total_qty",
		"total_net_weight",
		"additional_discount_percentage",
		"per_received",
	),
	"Purchase Invoice Item": (
		"qty",
		"received_qty",
		"rejected_qty",
		"stock_qty",
		"conversion_factor",
		"discount_percentage",
		"margin_rate_or_amount",
		"weight_per_unit",
		"total_weight",
	),
	"Purchase Invoice Advance": ("ref_exchange_rate",),
	"Purchase Taxes and Charges": ("rate",),
	"Payment Schedule": ("discount", "invoice_portion"),
	"Supplier Quotation": (
		"conversion_rate",
		"plc_conversion_rate",
		"total_qty",
		"total_net_weight",
		"additional_discount_percentage",
	),
	"Supplier Quotation Item": (
		"qty",
		"conversion_factor",
		"stock_qty",
		"discount_percentage",
		"margin_rate_or_amount",
		"weight_per_unit",
		"total_weight",
	),
	"Material Request": ("per_ordered", "per_received"),
	"Material Request Item": (
		"qty",
		"stock_qty",
		"conversion_factor",
		"ordered_qty",
		"received_qty",
		"actual_qty",
		"projected_qty",
		"min_order_qty",
		"reorder_level",
		"reorder_qty",
		"projected_on_hand",
		"picked_qty",
		"custom_base_qty",
	),
	"Landed Cost Item": ("qty",),
	"Landed Cost Taxes and Charges": ("exchange_rate", "qty"),
	"Subcontracting Order": ("total_qty", "per_received"),
	"Subcontracting Order Item": (
		"qty",
		"received_qty",
		"returned_qty",
		"conversion_factor",
		"fg_item_qty",
		"subcontracting_conversion_factor",
	),
	"Subcontracting Order Supplied Item": (
		"required_qty",
		"supplied_qty",
		"consumed_qty",
		"returned_qty",
		"total_supplied_qty",
		"conversion_factor",
		"stock_reserved_qty",
		"available_qty_for_consumption",
	),
	"Subcontracting Receipt": ("total_qty", "per_returned"),
	"Subcontracting Receipt Item": (
		"received_qty",
		"qty",
		"rejected_qty",
		"conversion_factor",
		"returned_qty",
		"process_loss_qty",
	),
	"Subcontracting Receipt Supplied Item": (
		"required_qty",
		"consumed_qty",
		"conversion_factor",
		"current_stock",
		"available_qty_for_consumption",
	),
	"Purchase Receipt Item Supplied": (
		"required_qty",
		"consumed_qty",
		"conversion_factor",
		"current_stock",
	),
	"Purchase Order Item Supplied": (
		"required_qty",
		"supplied_qty",
		"consumed_qty",
		"returned_qty",
		"total_supplied_qty",
		"conversion_factor",
	),
	"Advance Payment Ledger Entry": ("exchange_rate",),
}

EXCLUDED_VIRTUAL_FIELDS_BY_DOCTYPE: dict[str, tuple[str, ...]] = {}

CRITICAL_ASSET_FIELDS: tuple[str, ...] = (
	"net_purchase_amount",
	"purchase_amount",
	"gross_purchase_amount",
	"total_asset_cost",
)


@dataclass(frozen=True)
class PurchasingFieldTarget:
	doctype: str
	fieldname: str

	@property
	def table(self) -> str:
		return f"tab{self.doctype}"


def purchasing_field_targets() -> tuple[PurchasingFieldTarget, ...]:
	return tuple(
		PurchasingFieldTarget(doctype=doctype, fieldname=fieldname)
		for doctype, fieldnames in PURCHASING_MONETARY_FIELDS_BY_DOCTYPE.items()
		for fieldname in fieldnames
	)


def purchasing_graph_doctypes() -> tuple[str, ...]:
	docs = set(PURCHASING_MONETARY_FIELDS_BY_DOCTYPE)
	docs |= set(ALREADY_HARDENED_BY_STOCK_REPOST)
	docs |= set(EXCLUDED_RATE_PERCENT_QTY_FIELDS_BY_DOCTYPE)
	return tuple(sorted(docs))


def classify_purchasing_field(doctype: str, df) -> str | None:
	if cint(getattr(df, "is_virtual", 0)):
		return "virtual"
	ft = df.fieldtype
	if ft not in ("Currency", "Float", "Percent"):
		return None
	fn = df.fieldname
	if fn in PURCHASING_MONETARY_FIELDS_BY_DOCTYPE.get(doctype, ()):
		if (doctype, fn) in PURCHASING_MONETARY_RATE_FIELDS:
			return "monetary_rate"
		return "amount"
	if fn in ALREADY_HARDENED_BY_STOCK_REPOST.get(doctype, ()):
		return "already"
	if fn in EXCLUDED_RATE_PERCENT_QTY_FIELDS_BY_DOCTYPE.get(doctype, ()):
		return "rate_pct_qty"
	if ft == "Percent":
		return "rate_pct_qty"
	return None


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
	for target in purchasing_field_targets():
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
				continue
			meta = frappe.get_meta(target.doctype, cached=False)
			df = meta.get_field(target.fieldname)
			if not df:
				# gross_purchase_amount may exist only as leftover SQL column
				info = read_column_schema(target.table, target.fieldname)
				if info:
					row["metadata_action"] = "SKIP_NO_DOCFIELD_SQL_ONLY"
					results.append(row)
					continue
				row["metadata_action"] = SKIP_MISSING_FIELD
				results.append(row)
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
		"Purchasing metadata summary: total=%s changed=%s skipped=%s errors=%s",
		summary["total"],
		len(summary["changed"]),
		len(summary["skipped"]),
		len(summary["errors"]),
	)
	return results


def apply_decimal_schema_targets(logger) -> list[dict[str, Any]]:
	results: list[dict[str, Any]] = []
	for target in purchasing_field_targets():
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
			column_info = read_column_schema(target.table, target.fieldname)
			if not column_info:
				row["action"] = SKIP_MISSING_COLUMN
				results.append(row)
				continue
			if df:
				row["fieldtype"] = df.fieldtype
				if df.fieldtype not in DECIMAL_COMPATIBLE_FIELDTYPES:
					row["action"] = SKIP_INCOMPATIBLE_FIELDTYPE
					results.append(row)
					continue
			elif not column_info:
				row["action"] = SKIP_MISSING_FIELD
				results.append(row)
				continue
			if not table_exists(target.table):
				row["action"] = SKIP_MISSING_TABLE
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
		"Purchasing schema summary: total=%s changed=%s skipped=%s errors=%s actions=%s",
		summary["total"],
		len(summary["changed"]),
		len(summary["skipped"]),
		len(summary["errors"]),
		summary["action_counts"],
	)
	return results


def assert_purchasing_decimal_schema(logger=None) -> None:
	logger = logger or frappe.logger("erpnext_extensions.purchasing_decimal_precision_v524")
	failures: list[dict[str, Any]] = []
	for target in purchasing_field_targets():
		if not frappe.db.exists("DocType", target.doctype):
			continue
		info = read_column_schema(target.table, target.fieldname)
		if not info:
			# Skip SQL-only optional leftovers that don't exist
			meta = frappe.get_meta(target.doctype, cached=False)
			if not meta.get_field(target.fieldname):
				continue
			failures.append(
				{
					"doctype": target.doctype,
					"table": target.table,
					"field": target.fieldname,
					"column_type": None,
					"expected": f"DECIMAL({TARGET_PRECISION},{TARGET_SCALE})",
				}
			)
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
				"expected": f"DECIMAL({TARGET_PRECISION},{TARGET_SCALE})",
			}
		)
	if failures:
		detail = "; ".join(
			f"{r['doctype']}.{r['field']} ({r['table']}.{r['field']}): "
			f"actual={r['column_type']} expected={r['expected']}"
			for r in failures
		)
		raise RuntimeError(f"Purchasing DECIMAL(30,9) schema drift detected: {detail}")

	# Critical Asset proof fields
	for fieldname in CRITICAL_ASSET_FIELDS:
		info = read_column_schema("tabAsset", fieldname)
		if not info:
			# gross_purchase_amount may be absent on some sites
			if fieldname == "gross_purchase_amount":
				continue
			raise RuntimeError(f"Critical Asset column missing: tabAsset.{fieldname}")
		action = decide_decimal_action(info)
		if action not in {SKIP_ALREADY_CORRECT, SKIP_ALREADY_WIDER}:
			raise RuntimeError(
				f"Critical Asset field tabAsset.{fieldname} is {info.get('COLUMN_TYPE')}, "
				f"expected decimal({TARGET_PRECISION},{TARGET_SCALE})"
			)
	logger.info("Purchasing schema guard OK for %s fields", len(purchasing_field_targets()))


def assert_purchasing_field_classification_completeness() -> None:
	missing: list[str] = []
	for dt in purchasing_graph_doctypes():
		if not frappe.db.exists("DocType", dt):
			continue
		meta = frappe.get_meta(dt, cached=False)
		for df in meta.fields:
			if df.fieldtype not in ("Currency", "Float", "Percent"):
				continue
			cls = classify_purchasing_field(dt, df)
			if cls is None:
				missing.append(f"{dt}.{df.fieldname} ({df.fieldtype})")
	if missing:
		raise AssertionError(
			"Purchasing field classification incomplete:\n" + "\n".join(sorted(missing))
		)


def get_purchasing_precision_status() -> dict[str, Any]:
	correct: list[dict[str, Any]] = []
	incorrect: list[dict[str, Any]] = []
	missing: list[dict[str, Any]] = []
	for target in purchasing_field_targets():
		row = {
			"doctype": target.doctype,
			"table": target.table,
			"field": target.fieldname,
			"expected": f"decimal({TARGET_PRECISION},{TARGET_SCALE})",
		}
		if not frappe.db.exists("DocType", target.doctype):
			row["status"] = "missing_doctype"
			missing.append(row)
			continue
		info = read_column_schema(target.table, target.fieldname)
		meta = frappe.get_meta(target.doctype, cached=False)
		if not info:
			if not meta.get_field(target.fieldname):
				row["status"] = "optional_absent"
				missing.append(row)
				continue
			row["status"] = "missing_column"
			missing.append(row)
			continue
		row["actual"] = info.get("COLUMN_TYPE")
		action = decide_decimal_action(info)
		if action in {SKIP_ALREADY_CORRECT, SKIP_ALREADY_WIDER}:
			row["status"] = "ok"
			correct.append(row)
		else:
			row["status"] = "mismatch"
			row["action"] = action
			incorrect.append(row)
	return {
		"version_layer": "5.2.4",
		"registry": "purchasing_decimal_precision_v524.PURCHASING_MONETARY_FIELDS_BY_DOCTYPE",
		"total_registered": len(list(purchasing_field_targets())),
		"correct": len(correct),
		"incorrect": len(incorrect),
		"missing": len(missing),
		"incorrect_targets": incorrect,
		"missing_targets": missing,
		"critical_asset": {
			field: (read_column_schema("tabAsset", field) or {}).get("COLUMN_TYPE")
			for field in CRITICAL_ASSET_FIELDS
		},
	}


def repair_purchasing_decimal_schema(*, run_completeness_guard: bool = False) -> dict[str, Any]:
	logger = frappe.logger("erpnext_extensions.purchasing_decimal_precision_v524")
	logger.info("Starting purchasing DECIMAL(30,9) repair (v5.2.4)")
	before = get_purchasing_precision_status()
	metadata_results = verify_and_set_metadata(logger)
	schema_results = apply_decimal_schema_targets(logger)
	repaired = [
		f"{row['table']}.{row['field']}"
		for row in schema_results
		if row.get("action") == ALTER_TO_DECIMAL_30_9 and row.get("status") == "ok"
	]
	errors = [row for row in metadata_results + schema_results if row.get("status") == "error"]
	if errors:
		raise RuntimeError(
			"Purchasing DECIMAL(30,9) repair encountered errors:\n"
			+ "\n".join(f"{row.get('doctype') or row.get('table')}.{row.get('field')}" for row in errors)
		)
	assert_purchasing_decimal_schema(logger)
	if run_completeness_guard:
		assert_purchasing_field_classification_completeness()
	after = get_purchasing_precision_status()
	logger.info(
		"Completed purchasing repair: repaired=%s incorrect_before=%s incorrect_after=%s",
		len(repaired),
		before["incorrect"],
		after["incorrect"],
	)
	return {"repaired": repaired, "before": before, "after": after}


def after_migrate() -> None:
	repair_purchasing_decimal_schema(run_completeness_guard=False)


def audit_report_rows() -> list[dict[str, Any]]:
	"""Full classification audit for RELEASE / CLI reporting."""
	rows: list[dict[str, Any]] = []
	label_map = {
		"amount": "Monetary Amount",
		"monetary_rate": "Monetary Rate",
		"already": "Already hardened elsewhere",
		"rate_pct_qty": "Rate / Qty / Percentage — Excluded",
		"virtual": "Virtual / Non-DB — Excluded",
	}
	for dt in purchasing_graph_doctypes():
		if not frappe.db.exists("DocType", dt):
			continue
		meta = frappe.get_meta(dt, cached=False)
		table = f"tab{dt}"
		for df in meta.fields:
			if df.fieldtype not in ("Currency", "Float", "Percent"):
				continue
			cls = classify_purchasing_field(dt, df) or "UNCLASSIFIED"
			info = read_column_schema(table, df.fieldname) if not cint(getattr(df, "is_virtual", 0)) else None
			protected = bool(
				frappe.db.exists(
					"Property Setter",
					{
						"doc_type": dt,
						"field_name": df.fieldname,
						"property": "length",
						"doctype_or_field": "DocField",
					},
				)
			)
			rows.append(
				{
					"doctype": dt,
					"table": table,
					"field": df.fieldname,
					"fieldtype": df.fieldtype,
					"purpose": (df.label or df.fieldname),
					"classification": label_map.get(cls, cls),
					"schema": (info or {}).get("COLUMN_TYPE"),
					"metadata_protected": protected
					if cls in {"amount", "monetary_rate"}
					else (protected if cls == "already" else False),
				}
			)
	return rows


def print_audit_summary() -> None:
	rows = audit_report_rows()
	by_cls: Counter[str] = Counter(r["classification"] for r in rows)
	print(f"doctypes_audited={len(purchasing_graph_doctypes())}")
	print(f"fields_inspected={len(rows)}")
	for k, v in sorted(by_cls.items()):
		print(f"  {k}: {v}")
	status = get_purchasing_precision_status()
	print(f"registered_targets={status['total_registered']}")
	print(f"correct={status['correct']} incorrect={status['incorrect']} missing={status['missing']}")
	print("critical_asset", status["critical_asset"])

# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Stock repost / valuation DECIMAL(30,9) allowlist + migrate-safe helpers (v5.1.8).

Authoritative monetary storage hardening for the ERPNext Repost Item Valuation graph:

  Repost Item Valuation
    → repost_future_sle / update_entries_after
    → Stock Ledger Entry recalculation
    → Stock Entry Detail.recalculate_amounts_in_stock_entry (basic_amount, …)
    → Delivery/Sales/Purchase/Subcontracting rate rewrites
    → Serial and Batch Bundle / Bin / Landed Cost propagation
    → GL Entry accounting ledger repost

Does not change Frappe global Currency/Float mapping or ERPNext business logic.
Does not suppress legitimate negative-stock validation.

Supersedes partial Stock Reconciliation-only coverage for fields already in that allowlist
by re-asserting DECIMAL(30,9) idempotently (no conflict).
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

# DocTypes discovered by tracing repost_item_valuation → stock_ledger.update_entries_after
# and related voucher recalculation / GL repost write paths.
REPOST_ROOT_DOCTYPES: tuple[str, ...] = (
	"Repost Item Valuation",
	"Stock Entry",
	"Stock Ledger Entry",
	"Stock Reconciliation",
	"Landed Cost Voucher",
	"Purchase Receipt",
	"Purchase Invoice",
	"Delivery Note",
	"Sales Invoice",
	"Subcontracting Receipt",
	"Serial and Batch Bundle",
	"Bin",
	"GL Entry",
)

# Monetary amounts + valuation rates written/recalculated during repost.
# Static allowlist — never scan DocType fields at migrate time for discovery.
REPOST_MONETARY_FIELDS_BY_DOCTYPE: dict[str, tuple[str, ...]] = {
	"Stock Entry": (
		"total_incoming_value",
		"total_outgoing_value",
		"value_difference",
		"total_additional_costs",
		"total_amount",
	),
	"Stock Entry Detail": (
		"basic_amount",
		"amount",
		"additional_cost",
		"landed_cost_voucher_amount",
		"customer_provided_item_cost",
		"basic_rate",
		"valuation_rate",
		# Site custom monetary fields that participate in consignment / material-loan settlement.
		"custom_consignment_settlement_amount",
		"custom_material_loan_issue_value",
		"custom_material_loan_return_value",
		"custom_material_loan_settlement_amount",
		"custom_material_loan_issue_rate",
		"custom_original_receipt_rate",
	),
	"Stock Ledger Entry": (
		"stock_value",
		"stock_value_difference",
		"incoming_rate",
		"outgoing_rate",
		"valuation_rate",
	),
	"Stock Reconciliation": ("difference_amount",),
	"Stock Reconciliation Item": (
		"amount",
		"current_amount",
		"amount_difference",
		"valuation_rate",
		"current_valuation_rate",
	),
	"Landed Cost Voucher": (
		"total_taxes_and_charges",
		"total_vendor_invoices_cost",
	),
	"Landed Cost Item": (
		"amount",
		"applicable_charges",
		"rate",
	),
	"Landed Cost Purchase Receipt": ("grand_total",),
	"Landed Cost Taxes and Charges": (
		"amount",
		"base_amount",
	),
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
	"Purchase Receipt Item Supplied": (
		"rate",
		"amount",
	),
	"Delivery Note Item": (
		# Repost writes incoming_rate; amount fields already hardened in v5.0.7 (re-asserted).
		"incoming_rate",
		"amount",
		"base_amount",
		"net_amount",
		"base_net_amount",
		"discount_amount",
		"distributed_discount_amount",
		"billed_amt",
		"rate_with_margin",
		"base_rate_with_margin",
		"company_total_stock",
	),
	"Sales Invoice Item": (
		"incoming_rate",
		"amount",
		"base_amount",
		"net_amount",
		"base_net_amount",
		"discount_amount",
		"distributed_discount_amount",
		"rate_with_margin",
		"base_rate_with_margin",
		"company_total_stock",
		"custom_additional_discount_share",
		"custom_base_amount_for_payment",
		"custom_net_amount_for_payment",
		"custom_raw_discount_amount",
	),
	"Packed Item": (
		"incoming_rate",
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
	"Subcontracting Receipt Supplied Item": (
		"rate",
		"amount",
	),
	"Serial and Batch Bundle": (
		"avg_rate",
		"total_amount",
	),
	"Serial and Batch Entry": (
		"incoming_rate",
		"outgoing_rate",
		"stock_value_difference",
	),
	"Bin": (
		"stock_value",
		"valuation_rate",
	),
	"GL Entry": (
		"debit",
		"credit",
		"debit_in_account_currency",
		"credit_in_account_currency",
		"debit_in_transaction_currency",
		"credit_in_transaction_currency",
		"debit_in_reporting_currency",
		"credit_in_reporting_currency",
	),
}

# Monetary-per-unit rates intentionally hardened for IRR capacity (written during repost).
REPOST_MONETARY_RATE_FIELDS: frozenset[tuple[str, str]] = frozenset(
	{
		("Stock Entry Detail", "basic_rate"),
		("Stock Entry Detail", "valuation_rate"),
		("Stock Entry Detail", "custom_material_loan_issue_rate"),
		("Stock Entry Detail", "custom_original_receipt_rate"),
		("Stock Ledger Entry", "incoming_rate"),
		("Stock Ledger Entry", "outgoing_rate"),
		("Stock Ledger Entry", "valuation_rate"),
		("Stock Reconciliation Item", "valuation_rate"),
		("Stock Reconciliation Item", "current_valuation_rate"),
		("Landed Cost Item", "rate"),
		("Purchase Receipt Item", "valuation_rate"),
		("Purchase Receipt Item", "sales_incoming_rate"),
		("Purchase Invoice Item", "valuation_rate"),
		("Purchase Invoice Item", "sales_incoming_rate"),
		("Purchase Receipt Item Supplied", "rate"),
		("Delivery Note Item", "incoming_rate"),
		("Sales Invoice Item", "incoming_rate"),
		("Packed Item", "incoming_rate"),
		("Subcontracting Receipt Item", "rate"),
		("Subcontracting Receipt Item", "rm_cost_per_qty"),
		("Subcontracting Receipt Item", "service_cost_per_qty"),
		("Subcontracting Receipt Item", "additional_cost_per_qty"),
		("Subcontracting Receipt Item", "secondary_items_cost_per_qty"),
		("Subcontracting Receipt Supplied Item", "rate"),
		("Serial and Batch Bundle", "avg_rate"),
		("Serial and Batch Entry", "incoming_rate"),
		("Serial and Batch Entry", "outgoing_rate"),
		("Bin", "valuation_rate"),
	}
)

EXCLUDED_RATE_PERCENT_QTY_FIELDS_BY_DOCTYPE: dict[str, tuple[str, ...]] = {
	"Stock Entry": (
		"fg_completed_qty",
		"per_transferred",
		"process_loss_percentage",
		"process_loss_qty",
	),
	"Stock Entry Detail": (
		"qty",
		"conversion_factor",
		"transfer_qty",
		"actual_qty",
		"transferred_qty",
		"custom_material_loan_issue_qty",
		"custom_material_loan_previously_returned_qty",
		"custom_material_loan_remaining_returnable_qty",
		"custom_original_receipt_qty",
		"custom_previously_returned_qty",
		"custom_remaining_returnable_qty",
	),
	"Stock Ledger Entry": (
		"actual_qty",
		"qty_after_transaction",
	),
	"Stock Reconciliation Item": (
		"qty",
		"current_qty",
	),
	"Landed Cost Item": ("qty",),
	"Landed Cost Taxes and Charges": (
		"exchange_rate",
		"qty",
	),
	"Purchase Receipt Item": (
		"qty",
		"received_qty",
		"rejected_qty",
		"returned_qty",
		"stock_qty",
		"received_stock_qty",
		"conversion_factor",
		"rate",
		"base_rate",
		"net_rate",
		"base_net_rate",
		"price_list_rate",
		"base_price_list_rate",
		"stock_uom_rate",
		"discount_percentage",
		"margin_rate_or_amount",
		"weight_per_unit",
		"total_weight",
	),
	"Purchase Invoice Item": (
		"qty",
		"received_qty",
		"rejected_qty",
		"stock_qty",
		"conversion_factor",
		"rate",
		"base_rate",
		"net_rate",
		"base_net_rate",
		"price_list_rate",
		"base_price_list_rate",
		"stock_uom_rate",
		"discount_percentage",
		"margin_rate_or_amount",
		"weight_per_unit",
		"total_weight",
	),
	"Purchase Receipt Item Supplied": (
		"required_qty",
		"consumed_qty",
		"conversion_factor",
		"current_stock",
	),
	"Delivery Note Item": (
		"qty",
		"stock_qty",
		"conversion_factor",
		"rate",
		"base_rate",
		"net_rate",
		"base_net_rate",
		"price_list_rate",
		"base_price_list_rate",
		"stock_uom_rate",
		"discount_percentage",
		"margin_rate_or_amount",
		"weight_per_unit",
		"total_weight",
		"actual_qty",
		"actual_batch_qty",
		"installed_qty",
		"returned_qty",
		"received_qty",
		"packed_qty",
	),
	"Sales Invoice Item": (
		"qty",
		"stock_qty",
		"conversion_factor",
		"rate",
		"base_rate",
		"net_rate",
		"base_net_rate",
		"price_list_rate",
		"base_price_list_rate",
		"stock_uom_rate",
		"discount_percentage",
		"margin_rate_or_amount",
		"weight_per_unit",
		"total_weight",
		"actual_qty",
		"actual_batch_qty",
		"delivered_qty",
	),
	"Packed Item": (
		"qty",
		"actual_qty",
		"projected_qty",
		"actual_batch_qty",
		"conversion_factor",
		"rate",
		"ordered_qty",
		"picked_qty",
		"packed_qty",
		"requested_qty",
	),
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
	"Serial and Batch Bundle": ("total_qty",),
	"Serial and Batch Entry": (
		"qty",
		"delivered_qty",
	),
	"Bin": (
		"reserved_qty",
		"actual_qty",
		"ordered_qty",
		"indented_qty",
		"planned_qty",
		"projected_qty",
		"reserved_qty_for_production",
		"reserved_qty_for_sub_contract",
		"reserved_qty_for_production_plan",
		"reserved_stock",
	),
	"GL Entry": (
		"transaction_exchange_rate",
		"reporting_currency_exchange_rate",
	),
}

EXCLUDED_VIRTUAL_FIELDS_BY_DOCTYPE: dict[str, tuple[str, ...]] = {}

# Selling amounts already hardened in v5.0.7 — still present on DN/SI Item but not
# rewritten by stock-ledger rate updates except incoming_rate (allowlisted above).
# Completeness guard for DN/SI Item only requires Currency/Float classification for
# fields that belong to the repost write graph OR are co-located on those tables.
# We classify all Currency/Float/Percent on allowlisted DocTypes.

RATE_EXACT = frozenset(
	{
		"rate",
		"base_rate",
		"net_rate",
		"base_net_rate",
		"price_list_rate",
		"base_price_list_rate",
		"stock_uom_rate",
		"exchange_rate",
	}
)


@dataclass(frozen=True)
class RepostFieldTarget:
	doctype: str
	fieldname: str

	@property
	def table(self) -> str:
		return f"tab{self.doctype}"


def repost_field_targets() -> tuple[RepostFieldTarget, ...]:
	return tuple(
		RepostFieldTarget(doctype=doctype, fieldname=fieldname)
		for doctype, fieldnames in REPOST_MONETARY_FIELDS_BY_DOCTYPE.items()
		for fieldname in fieldnames
	)


def repost_graph_doctypes() -> tuple[str, ...]:
	return tuple(sorted(set(REPOST_MONETARY_FIELDS_BY_DOCTYPE) | set(EXCLUDED_RATE_PERCENT_QTY_FIELDS_BY_DOCTYPE)))


def classify_repost_field(doctype: str, df) -> str | None:
	"""Return amount | monetary_rate | rate_pct_qty | virtual | None."""
	if cint(getattr(df, "is_virtual", 0)):
		return "virtual"
	ft = df.fieldtype
	if ft not in ("Currency", "Float", "Percent"):
		return None
	fn = df.fieldname
	if (doctype, fn) in REPOST_MONETARY_RATE_FIELDS:
		return "monetary_rate"
	if fn in REPOST_MONETARY_FIELDS_BY_DOCTYPE.get(doctype, ()):
		return "amount"
	if fn in EXCLUDED_RATE_PERCENT_QTY_FIELDS_BY_DOCTYPE.get(doctype, ()):
		return "rate_pct_qty"
	if ft == "Percent":
		return "rate_pct_qty"
	# Fallback heuristics for newly discovered fields (completeness will fail until allowlisted).
	fl = (fn or "").lower()
	if fl.endswith("_qty") or fl in {"qty", "conversion_factor"} or "weight" in fl:
		return "rate_pct_qty"
	if fl.endswith("_percentage") or fl == "discount_percentage" or fl == "margin_rate_or_amount":
		return "rate_pct_qty"
	if fl in RATE_EXACT or fl.endswith("_rate"):
		# Unclassified rate — treat as needing explicit decision.
		return None
	if ft == "Currency" or (ft == "Float" and any(h in fl for h in ("amount", "value", "cost", "debit", "credit"))):
		return None
	return "rate_pct_qty"


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
	for target in repost_field_targets():
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
		"Stock repost metadata summary: total=%s changed=%s skipped=%s errors=%s actions=%s",
		summary["total"],
		len(summary["changed"]),
		len(summary["skipped"]),
		len(summary["errors"]),
		summary["action_counts"],
	)
	return results


def apply_decimal_schema_targets(logger) -> list[dict[str, Any]]:
	results: list[dict[str, Any]] = []
	for target in repost_field_targets():
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
		"Stock repost schema summary: total=%s changed=%s skipped=%s errors=%s actions=%s",
		summary["total"],
		len(summary["changed"]),
		len(summary["skipped"]),
		len(summary["errors"]),
		summary["action_counts"],
	)
	return results


def assert_repost_monetary_schema_targets(logger=None) -> list[dict[str, Any]]:
	logger = logger or frappe.logger("erpnext_extensions.stock_repost_decimal_precision_v518")
	failures: list[dict[str, Any]] = []
	for target in repost_field_targets():
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
		raise RuntimeError(f"Stock repost DECIMAL(30,9) schema drift detected: {detail}")
	logger.info("Stock repost schema guard OK for %s fields", len(repost_field_targets()))
	return failures


def assert_repost_field_classification_completeness() -> None:
	"""Fail if any Currency/Float/Percent on the audited repost DocTypes is unclassified."""
	missing_amount: list[str] = []
	missing_rate: list[str] = []
	unknown: list[str] = []

	for dt in repost_graph_doctypes():
		if not frappe.db.exists("DocType", dt):
			continue
		allowlisted = set(REPOST_MONETARY_FIELDS_BY_DOCTYPE.get(dt, ()))
		excluded = set(EXCLUDED_RATE_PERCENT_QTY_FIELDS_BY_DOCTYPE.get(dt, ()))
		excluded_virtual = set(EXCLUDED_VIRTUAL_FIELDS_BY_DOCTYPE.get(dt, ()))
		meta = frappe.get_meta(dt, cached=False)
		for df in meta.fields:
			if df.fieldtype not in ("Currency", "Float", "Percent"):
				continue
			label = f"{dt}.{df.fieldname}"
			cls = classify_repost_field(dt, df)
			if cls in {"amount", "monetary_rate"}:
				if df.fieldname not in allowlisted:
					missing_amount.append(label)
			elif cls == "rate_pct_qty":
				if df.fieldname not in excluded:
					missing_rate.append(label)
			elif cls == "virtual":
				if df.fieldname not in excluded_virtual:
					unknown.append(f"{label} (virtual)")
			elif cls is None:
				unknown.append(label)

	if missing_amount or missing_rate or unknown:
		lines = []
		if missing_amount:
			lines.append("Unallowlisted monetary fields: " + ", ".join(sorted(missing_amount)))
		if missing_rate:
			lines.append("Unclassified rate/qty/percentage fields: " + ", ".join(sorted(missing_rate)))
		if unknown:
			lines.append("Unclassified fields needing explicit decision: " + ", ".join(sorted(unknown)))
		raise AssertionError("Stock repost field classification incomplete:\n" + "\n".join(lines))


def audit_report_rows() -> list[dict[str, Any]]:
	rows: list[dict[str, Any]] = []
	for dt in repost_graph_doctypes():
		if not frappe.db.exists("DocType", dt):
			continue
		meta = frappe.get_meta(dt, cached=False)
		table = f"tab{dt}"
		for df in meta.fields:
			if df.fieldtype not in ("Currency", "Float", "Percent"):
				continue
			cls = classify_repost_field(dt, df)
			if cls == "amount":
				classification = "Monetary Amount"
				repost_role = "recalculated / written"
			elif cls == "monetary_rate":
				classification = "Monetary Rate"
				repost_role = "valuation rate rewrite"
			elif cls == "rate_pct_qty":
				classification = "Rate / Percentage / Quantity — Excluded"
				repost_role = "not amount storage"
			elif cls == "virtual":
				classification = "Virtual / Non-DB — Excluded"
				repost_role = "non-db"
			else:
				classification = "Unknown"
				repost_role = "needs decision"
			info = read_column_schema(table, df.fieldname) if cls != "virtual" else None
			old_type = info.get("COLUMN_TYPE") if info else None
			new_type = old_type
			if classification in {"Monetary Amount", "Monetary Rate"} and info:
				action = decide_decimal_action(info)
				new_type = (
					f"decimal({TARGET_PRECISION},{TARGET_SCALE})"
					if action != SKIP_ALREADY_WIDER
					else old_type
				)
			rows.append(
				{
					"doctype": dt,
					"table": table,
					"field": df.fieldname,
					"fieldtype": df.fieldtype,
					"repost_role": repost_role,
					"classification": classification,
					"old_sql": old_type,
					"new_sql": new_type,
				}
			)
	return rows

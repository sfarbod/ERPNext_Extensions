# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Manufacturing DECIMAL(30,9) allowlist + migrate-safe helpers (v5.7.21).

Authoritative monetary storage hardening for BOM, Work Order, and Job Card
parent/child DocTypes used in ERPNext 16 manufacturing.

Does not change Frappe global Currency/Float mapping or ERPNext business logic.
Does not widen quantities, conversion factors, percentages, times, or potencies.
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
	SKIP_UNEXPECTED_TYPE,
	TARGET_LENGTH,
	TARGET_PRECISION,
	TARGET_SCALE,
	alter_decimal_column,
	desired_metadata_length,
	ensure_length_property_setter,
	read_column_schema,
	table_exists,
)

MANUFACTURING_ROOT_DOCTYPES: tuple[str, ...] = ("BOM", "Work Order", "Job Card")

# Explicit monetary amount / monetary rate allowlist (DB-backed).
MANUFACTURING_MONETARY_FIELDS_BY_DOCTYPE: dict[str, tuple[str, ...]] = {
	"BOM": (
		"operating_cost",
		"operating_cost_per_bom_quantity",
		"raw_material_cost",
		"scrap_material_cost",
		"secondary_items_cost",
		"total_cost",
		"base_operating_cost",
		"base_raw_material_cost",
		"base_scrap_material_cost",
		"base_secondary_items_cost",
		"base_total_cost",
		"cost_allocation",
	),
	"BOM Item": (
		"rate",
		"base_rate",
		"amount",
		"base_amount",
	),
	"BOM Explosion Item": (
		"rate",
		"amount",
	),
	"BOM Operation": (
		"hour_rate",
		"base_hour_rate",
		"operating_cost",
		"base_operating_cost",
		"cost_per_unit",
		"base_cost_per_unit",
	),
	"BOM Scrap Item": (
		"rate",
		"base_rate",
		"amount",
		"base_amount",
	),
	"BOM Batch Items": (
		"rate",
		"base_rate",
		"amount",
		"base_amount",
	),
	"BOM Secondary Item": (
		"cost",
		"base_cost",
		"rate",
	),
	"BOM Creator": ("raw_material_cost",),
	"BOM Creator Item": (
		"rate",
		"base_rate",
		"amount",
		"base_amount",
	),
	"Work Order": (
		"planned_operating_cost",
		"actual_operating_cost",
		"additional_operating_cost",
		"total_operating_cost",
		"corrective_operation_cost",
	),
	"Work Order Item": (
		"rate",
		"amount",
	),
	"Work Order Operation": (
		"hour_rate",
		"planned_operating_cost",
		"actual_operating_cost",
	),
	"Work Order Additional Item": (
		"rate",
		"amount",
	),
	"Job Card": ("hour_rate",),
}

# Intentionally excluded qty / % / fx / time / potency / volume fields.
EXCLUDED_RATE_PERCENT_QTY_FIELDS_BY_DOCTYPE: dict[str, tuple[str, ...]] = {
	"BOM": (
		"quantity",
		"conversion_rate",
		"plc_conversion_rate",
		"process_loss_percentage",
		"process_loss_qty",
		"cost_allocation_per",
		"custom_actual_batch_size_quantity",
		"custom_api_volume",
		"custom_calculation_batch_size",
		"custom_ha_potency",
		"custom_reference_potency",
		"custom_target_filling_volume_ml",
	),
	"BOM Item": (
		"qty",
		"stock_qty",
		"conversion_factor",
		"qty_consumed_per_unit",
		"custom_actual_qty",
		"custom_base_qty",
		"custom_item_over",
	),
	"BOM Explosion Item": ("stock_qty", "qty_consumed_per_unit"),
	"BOM Operation": ("time_in_mins", "batch_size", "finished_good_qty"),
	"BOM Scrap Item": ("stock_qty",),
	"BOM Batch Items": (
		"qty",
		"stock_qty",
		"conversion_factor",
		"qty_consumed_per_unit",
		"custom_actual_qty",
	),
	"BOM Secondary Item": (
		"qty",
		"stock_qty",
		"conversion_factor",
		"cost_allocation_per",
		"process_loss_per",
		"process_loss_qty",
		"custom_output_equivalent_factor",
	),
	"BOM Creator": ("qty", "conversion_rate", "plc_conversion_rate"),
	"BOM Creator Item": ("qty", "design_quantity", "stock_qty", "conversion_factor"),
	"BOM Creator Material": ("qty",),
	"BOM Creator Operation": ("time_in_mins", "output_quantity"),
	"Work Order": (
		"qty",
		"material_transferred_for_manufacturing",
		"produced_qty",
		"lead_time",
		"batch_size",
		"process_loss_qty",
		"disassembled_qty",
		"additional_transferred_qty",
		"max_producible_qty",
	),
	"Work Order Item": (
		"required_qty",
		"transferred_qty",
		"consumed_qty",
		"available_qty_at_source_warehouse",
		"available_qty_at_wip_warehouse",
		"returned_qty",
		"subcontracted_qty",
		"stock_reserved_qty",
	),
	"Work Order Operation": (
		"completed_qty",
		"time_in_mins",
		"actual_operation_time",
		"batch_size",
		"process_loss_qty",
		"output_qty",
		"pending_qty",
	),
	"Work Order Additional Item": ("qty",),
	"Job Card": (
		"for_quantity",
		"total_completed_qty",
		"total_time_in_mins",
		"transferred_qty",
		"requested_qty",
		"process_loss_qty",
		"time_required",
		"manufactured_qty",
		"pending_qty",
		"custom_real_qty_to_manufacture",
	),
	"Job Card Item": (
		"required_qty",
		"transferred_qty",
		"consumed_qty",
		"custom_custom_returnableqty",
		"custom_issued_qty",
		"custom_returnable_qty",
		"custom_returned_qty",
		"custom_still_in_wip",
	),
	"Job Card Operation": ("completed_qty",),
	"Job Card Time Log": ("time_in_mins", "completed_qty"),
	"Job Card Scrap Item": ("stock_qty",),
	"Job Card Secondary Item": ("stock_qty", "custom_output_equivalent_factor"),
	"Job Card Scheduled Time": ("time_in_mins",),
}


@dataclass(frozen=True)
class ManufacturingFieldTarget:
	doctype: str
	fieldname: str

	@property
	def table(self) -> str:
		return f"tab{self.doctype}"


def manufacturing_field_targets() -> tuple[ManufacturingFieldTarget, ...]:
	return tuple(
		ManufacturingFieldTarget(doctype=doctype, fieldname=fieldname)
		for doctype, fieldnames in MANUFACTURING_MONETARY_FIELDS_BY_DOCTYPE.items()
		for fieldname in fieldnames
	)


def manufacturing_graph_doctypes() -> tuple[str, ...]:
	docs = set(MANUFACTURING_MONETARY_FIELDS_BY_DOCTYPE) | set(EXCLUDED_RATE_PERCENT_QTY_FIELDS_BY_DOCTYPE)
	return tuple(sorted(docs))


def decide_manufacturing_decimal_action(column_info: dict[str, Any] | None) -> str:
	"""Widen precision and/or scale to DECIMAL(30,9) for monetary columns.

	Unlike the generic helper (which skips unexpected scale), manufacturing must
	also lift ``DECIMAL(21,2)`` hour rates and similar monetary columns to scale 9.
	"""
	if not column_info:
		return SKIP_MISSING_COLUMN
	data_type = cstr(column_info.get("DATA_TYPE")).lower()
	if data_type != "decimal":
		return SKIP_UNEXPECTED_TYPE
	precision = cint(column_info.get("NUMERIC_PRECISION"))
	scale = cint(column_info.get("NUMERIC_SCALE"))
	if precision > TARGET_PRECISION and scale >= TARGET_SCALE:
		return SKIP_ALREADY_WIDER
	if precision == TARGET_PRECISION and scale == TARGET_SCALE:
		return SKIP_ALREADY_CORRECT
	if precision >= TARGET_PRECISION and scale > TARGET_SCALE:
		return SKIP_ALREADY_WIDER
	# Narrower precision and/or scale → widen (safe; does not truncate existing values).
	return ALTER_TO_DECIMAL_30_9


def classify_manufacturing_field(doctype: str, df) -> str | None:
	if cint(getattr(df, "is_virtual", 0)):
		return "virtual"
	if df.fieldtype not in ("Currency", "Float", "Percent"):
		return None
	fn = df.fieldname
	if fn in MANUFACTURING_MONETARY_FIELDS_BY_DOCTYPE.get(doctype, ()):
		return "amount"
	if fn in EXCLUDED_RATE_PERCENT_QTY_FIELDS_BY_DOCTYPE.get(doctype, ()):
		return "rate_pct_qty"
	if df.fieldtype == "Percent":
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


def ensure_scale_property_setter(doctype: str, fieldname: str, logger) -> str:
	"""Ensure DocField property ``precision`` (MariaDB scale) is 9.

	Frappe maps Currency/Float ``precision`` → DECIMAL scale. Some manufacturing
	fields (e.g. BOM Operation.hour_rate) ship with precision=2, which would
	cause ``updatedb()`` to emit DECIMAL(30,2) even after length=30.
	"""
	from frappe.custom.doctype.property_setter.property_setter import make_property_setter

	filters = {
		"doc_type": doctype,
		"field_name": fieldname,
		"property": "precision",
		"doctype_or_field": "DocField",
	}
	existing_name = frappe.db.get_value("Property Setter", filters, "name")
	current_value = frappe.db.get_value("Property Setter", filters, "value") if existing_name else None
	if current_value is not None and cint(current_value) >= TARGET_SCALE:
		return "SKIP_SCALE_ALREADY_SET"
	if existing_name:
		frappe.db.set_value(
			"Property Setter", existing_name, "value", str(TARGET_SCALE), update_modified=False
		)
		frappe.clear_cache(doctype=doctype)
		logger.info("Updated Property Setter %s.%s precision(scale) -> %s", doctype, fieldname, TARGET_SCALE)
		return "UPDATE_METADATA_SCALE"
	make_property_setter(
		doctype,
		fieldname,
		"precision",
		str(TARGET_SCALE),
		"Select",
		is_system_generated=True,
		validate_fields_for_doctype=False,
	)
	frappe.clear_cache(doctype=doctype)
	logger.info("Created Property Setter %s.%s precision(scale) -> %s", doctype, fieldname, TARGET_SCALE)
	return "CREATE_METADATA_SCALE"


def verify_and_set_metadata(logger) -> list[dict[str, Any]]:
	results: list[dict[str, Any]] = []
	for target in manufacturing_field_targets():
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
				# Optional leftover SQL-only columns (e.g. rate on Secondary Item).
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
			# Always pin scale=9 so updatedb cannot emit DECIMAL(30,2) from DocField precision.
			scale_action = ensure_scale_property_setter(target.doctype, target.fieldname, logger)
			row["metadata_length"] = final_length
			row["metadata_action"] = action
			row["metadata_scale_action"] = scale_action
			results.append(row)
		except Exception:
			row["status"] = "error"
			row["metadata_action"] = "METADATA_EXCEPTION"
			row["traceback"] = frappe.get_traceback()
			results.append(row)
			logger.error("Metadata failed for %s.%s\n%s", target.doctype, target.fieldname, row["traceback"])
	summary = summarize_results(results, action_key="metadata_action")
	logger.info(
		"Manufacturing metadata summary: total=%s changed=%s skipped=%s errors=%s",
		summary["total"],
		len(summary["changed"]),
		len(summary["skipped"]),
		len(summary["errors"]),
	)
	return results


def apply_decimal_schema_targets(logger) -> list[dict[str, Any]]:
	results: list[dict[str, Any]] = []
	for target in manufacturing_field_targets():
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
			if not table_exists(target.table):
				row["action"] = SKIP_MISSING_TABLE
				results.append(row)
				continue
			column_info = read_column_schema(target.table, target.fieldname)
			if not column_info:
				row["action"] = SKIP_MISSING_COLUMN
				results.append(row)
				continue
			# Prefer DocField type checks when the DocType still exists; still ALTER
			# orphan leftover tables (e.g. tabBOM Scrap Item without DocType).
			df = None
			if frappe.db.exists("DocType", target.doctype):
				meta = frappe.get_meta(target.doctype, cached=False)
				df = meta.get_field(target.fieldname)
				if df:
					row["fieldtype"] = df.fieldtype
					if df.fieldtype not in DECIMAL_COMPATIBLE_FIELDTYPES:
						row["action"] = SKIP_INCOMPATIBLE_FIELDTYPE
						results.append(row)
						continue
			row["before_db_type"] = column_info.get("COLUMN_TYPE")
			action = decide_manufacturing_decimal_action(column_info)
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
		"Manufacturing schema summary: total=%s changed=%s skipped=%s errors=%s actions=%s",
		summary["total"],
		len(summary["changed"]),
		len(summary["skipped"]),
		len(summary["errors"]),
		summary["action_counts"],
	)
	return results


def assert_manufacturing_decimal_schema(logger=None) -> None:
	logger = logger or frappe.logger("erpnext_extensions.manufacturing_decimal_precision_v5721")
	failures: list[dict[str, Any]] = []
	for target in manufacturing_field_targets():
		if not table_exists(target.table):
			# Optional child DocType not materialized on this site.
			continue
		info = read_column_schema(target.table, target.fieldname)
		if not info:
			# Orphan/optional column not present.
			continue
		action = decide_manufacturing_decimal_action(info)
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
		raise RuntimeError(f"Manufacturing DECIMAL(30,9) schema drift detected: {detail}")
	logger.info("Manufacturing schema guard OK for %s fields", len(manufacturing_field_targets()))


def assert_manufacturing_field_classification_completeness() -> None:
	missing: list[str] = []
	for dt in manufacturing_graph_doctypes():
		if not frappe.db.exists("DocType", dt):
			continue
		meta = frappe.get_meta(dt, cached=False)
		for df in meta.fields:
			if df.fieldtype not in ("Currency", "Float", "Percent"):
				continue
			cls = classify_manufacturing_field(dt, df)
			if cls is None:
				missing.append(f"{dt}.{df.fieldname} ({df.fieldtype})")
	if missing:
		raise AssertionError(
			"Manufacturing field classification incomplete:\n" + "\n".join(sorted(missing))
		)


def get_manufacturing_precision_status() -> dict[str, Any]:
	correct: list[dict[str, Any]] = []
	incorrect: list[dict[str, Any]] = []
	missing: list[dict[str, Any]] = []
	for target in manufacturing_field_targets():
		row = {
			"doctype": target.doctype,
			"table": target.table,
			"field": target.fieldname,
			"expected": f"decimal({TARGET_PRECISION},{TARGET_SCALE})",
		}
		if not table_exists(target.table):
			row["status"] = "missing_table"
			missing.append(row)
			continue
		info = read_column_schema(target.table, target.fieldname)
		if not info:
			row["status"] = "missing_column"
			missing.append(row)
			continue
		row["actual"] = info.get("COLUMN_TYPE")
		action = decide_manufacturing_decimal_action(info)
		if action in {SKIP_ALREADY_CORRECT, SKIP_ALREADY_WIDER}:
			row["status"] = "ok"
			correct.append(row)
		else:
			row["status"] = "mismatch"
			row["action"] = action
			incorrect.append(row)
	return {
		"version_layer": "5.7.21",
		"registry": "manufacturing_decimal_precision_v5721.MANUFACTURING_MONETARY_FIELDS_BY_DOCTYPE",
		"total_registered": len(list(manufacturing_field_targets())),
		"correct": len(correct),
		"incorrect": len(incorrect),
		"missing": len(missing),
		"incorrect_targets": incorrect,
		"missing_targets": missing,
	}


def repair_manufacturing_decimal_schema(*, run_completeness_guard: bool = False) -> dict[str, Any]:
	logger = frappe.logger("erpnext_extensions.manufacturing_decimal_precision_v5721")
	logger.info("Starting manufacturing DECIMAL(30,9) repair (v5.7.21)")
	before = get_manufacturing_precision_status()
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
			"Manufacturing DECIMAL(30,9) repair encountered errors:\n"
			+ "\n".join(f"{row.get('doctype') or row.get('table')}.{row.get('field')}" for row in errors)
		)
	assert_manufacturing_decimal_schema(logger)
	if run_completeness_guard:
		assert_manufacturing_field_classification_completeness()
	after = get_manufacturing_precision_status()
	logger.info(
		"Completed manufacturing repair: repaired=%s incorrect_before=%s incorrect_after=%s",
		len(repaired),
		before["incorrect"],
		after["incorrect"],
	)
	return {"repaired": repaired, "before": before, "after": after}

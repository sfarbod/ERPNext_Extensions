# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Sales Order amount_eligible_for_commission DECIMAL(30,9) hotfix helpers.

Targeted storage-capacity hardening for one failing Sales Order column only.
Does not change Frappe global Currency/Float mapping or other Sales Order amounts.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any

import frappe
from frappe.custom.doctype.property_setter.property_setter import make_property_setter
from frappe.utils import cint, cstr

TARGET_DOCTYPE = "Sales Order"
TARGET_FIELD = "amount_eligible_for_commission"
TARGET_PRECISION = 30
TARGET_SCALE = 9
TARGET_LENGTH = 30

DECIMAL_COMPATIBLE_FIELDTYPES = frozenset({"Currency", "Float", "Percent"})

SKIP_ALREADY_CORRECT = "SKIP_ALREADY_CORRECT"
SKIP_ALREADY_WIDER = "SKIP_ALREADY_WIDER"
ALTER_TO_DECIMAL_30_9 = "ALTER_TO_DECIMAL_30_9"
SKIP_UNEXPECTED_SCALE = "SKIP_UNEXPECTED_SCALE"
SKIP_UNEXPECTED_TYPE = "SKIP_UNEXPECTED_TYPE"
SKIP_MISSING_TABLE = "SKIP_MISSING_TABLE"
SKIP_MISSING_COLUMN = "SKIP_MISSING_COLUMN"
SKIP_MISSING_DOCTYPE = "SKIP_MISSING_DOCTYPE"
SKIP_MISSING_FIELD = "SKIP_MISSING_FIELD"
SKIP_INCOMPATIBLE_FIELDTYPE = "SKIP_INCOMPATIBLE_FIELDTYPE"


@dataclass(frozen=True)
class SalesOrderCommissionFieldTarget:
	doctype: str = TARGET_DOCTYPE
	fieldname: str = TARGET_FIELD

	@property
	def table(self) -> str:
		return f"tab{self.doctype}"


def sales_order_commission_field_target() -> SalesOrderCommissionFieldTarget:
	return SalesOrderCommissionFieldTarget()


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


def read_column_schema(table: str, column: str) -> dict[str, Any] | None:
	db_name = frappe.db.sql("SELECT DATABASE()")[0][0]
	row = frappe.db.sql(
		"""
		SELECT
			TABLE_NAME,
			COLUMN_NAME,
			DATA_TYPE,
			COLUMN_TYPE,
			NUMERIC_PRECISION,
			NUMERIC_SCALE,
			IS_NULLABLE,
			COLUMN_DEFAULT
		FROM INFORMATION_SCHEMA.COLUMNS
		WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND COLUMN_NAME=%s
		""",
		(db_name, table, column),
		as_dict=True,
	)
	return row[0] if row else None


def table_exists(table: str) -> bool:
	db_name = frappe.db.sql("SELECT DATABASE()")[0][0]
	return bool(
		frappe.db.sql(
			"""
			SELECT 1
			FROM INFORMATION_SCHEMA.COLUMNS
			WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s
			LIMIT 1
			""",
			(db_name, table),
		)
	)


def decide_decimal_action(column_info: dict[str, Any] | None) -> str:
	if not column_info:
		return SKIP_MISSING_COLUMN

	data_type = cstr(column_info.get("DATA_TYPE")).lower()
	precision = column_info.get("NUMERIC_PRECISION")
	scale = column_info.get("NUMERIC_SCALE")

	if data_type != "decimal":
		return SKIP_UNEXPECTED_TYPE
	if precision is None or scale is None:
		return SKIP_UNEXPECTED_TYPE
	precision = int(precision)
	scale = int(scale)

	if scale != TARGET_SCALE:
		return SKIP_UNEXPECTED_SCALE
	if precision == TARGET_PRECISION:
		return SKIP_ALREADY_CORRECT
	if precision > TARGET_PRECISION:
		return SKIP_ALREADY_WIDER
	return ALTER_TO_DECIMAL_30_9


def desired_metadata_length(column_info: dict[str, Any] | None) -> int:
	if not column_info:
		return TARGET_LENGTH
	data_type = cstr(column_info.get("DATA_TYPE")).lower()
	precision = column_info.get("NUMERIC_PRECISION")
	scale = column_info.get("NUMERIC_SCALE")
	if data_type == "decimal" and precision is not None and scale is not None:
		precision = int(precision)
		scale = int(scale)
		if scale == TARGET_SCALE and precision > TARGET_PRECISION:
			return precision
	return TARGET_LENGTH


def ensure_length_property_setter(
	doctype: str,
	fieldname: str,
	target_length: int,
	logger,
	*,
	validate_fields_for_doctype: bool = False,
) -> tuple[str, int]:
	filters = {
		"doc_type": doctype,
		"field_name": fieldname,
		"property": "length",
		"doctype_or_field": "DocField",
	}
	existing_name = frappe.db.get_value("Property Setter", filters, "name")
	current_value = frappe.db.get_value("Property Setter", filters, "value") if existing_name else None

	if current_value is not None and cint(current_value) >= cint(target_length):
		return "SKIP_METADATA_ALREADY_SET", cint(current_value)

	if existing_name:
		frappe.db.set_value("Property Setter", existing_name, "value", str(target_length), update_modified=False)
		frappe.clear_cache(doctype=doctype)
		logger.info(
			"Updated Property Setter %s: %s.%s length -> %s",
			existing_name,
			doctype,
			fieldname,
			target_length,
		)
		return "UPDATE_METADATA_LENGTH", target_length

	make_property_setter(
		doctype,
		fieldname,
		"length",
		str(target_length),
		"Int",
		is_system_generated=True,
		validate_fields_for_doctype=validate_fields_for_doctype,
	)
	frappe.clear_cache(doctype=doctype)
	logger.info("Created Property Setter: %s.%s length -> %s", doctype, fieldname, target_length)
	return "CREATE_METADATA_LENGTH", target_length


def verify_and_set_metadata(logger) -> list[dict[str, Any]]:
	target = sales_order_commission_field_target()
	results: list[dict[str, Any]] = []
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
			return results

		meta = frappe.get_meta(target.doctype, cached=False)
		df = meta.get_field(target.fieldname)
		if not df:
			row["metadata_action"] = SKIP_MISSING_FIELD
			results.append(row)
			logger.warning("Skipping missing field %s on %s", target.fieldname, target.doctype)
			return results

		row["fieldtype"] = df.fieldtype
		if df.fieldtype not in DECIMAL_COMPATIBLE_FIELDTYPES:
			row["metadata_action"] = SKIP_INCOMPATIBLE_FIELDTYPE
			results.append(row)
			logger.warning(
				"Skipping incompatible fieldtype %s on %s.%s",
				df.fieldtype,
				target.doctype,
				target.fieldname,
			)
			return results

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
		"Sales Order commission metadata summary: total=%s changed=%s skipped=%s errors=%s actions=%s",
		summary["total"],
		len(summary["changed"]),
		len(summary["skipped"]),
		len(summary["errors"]),
		summary["action_counts"],
	)
	return results


def _default_sql(column_default: Any) -> str:
	if column_default is None:
		return ""
	return f" DEFAULT {frappe.db.escape(cstr(column_default))}"


def alter_decimal_column(table: str, column: str, column_info: dict[str, Any], logger) -> None:
	null_sql = "NULL" if cstr(column_info.get("IS_NULLABLE")).upper() == "YES" else "NOT NULL"
	default_sql = _default_sql(column_info.get("COLUMN_DEFAULT"))
	frappe.db.sql_ddl(
		f"""
		ALTER TABLE `{table}`
		MODIFY `{column}` DECIMAL({TARGET_PRECISION},{TARGET_SCALE}) {null_sql}{default_sql}
		"""
	)
	logger.info("Updated %s.%s to DECIMAL(%s,%s)", table, column, TARGET_PRECISION, TARGET_SCALE)


def apply_decimal_schema_target(logger) -> list[dict[str, Any]]:
	target = sales_order_commission_field_target()
	results: list[dict[str, Any]] = []
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
			logger.warning("Skipping missing DocType %s", target.doctype)
			return results

		meta = frappe.get_meta(target.doctype, cached=False)
		df = meta.get_field(target.fieldname)
		if not df:
			row["action"] = SKIP_MISSING_FIELD
			results.append(row)
			logger.warning("Skipping missing field %s on %s", target.fieldname, target.doctype)
			return results

		row["fieldtype"] = df.fieldtype
		if df.fieldtype not in DECIMAL_COMPATIBLE_FIELDTYPES:
			row["action"] = SKIP_INCOMPATIBLE_FIELDTYPE
			results.append(row)
			logger.warning(
				"Skipping incompatible fieldtype %s on %s.%s",
				df.fieldtype,
				target.doctype,
				target.fieldname,
			)
			return results

		if not table_exists(target.table):
			row["action"] = SKIP_MISSING_TABLE
			results.append(row)
			logger.warning("Skipping missing table %s", target.table)
			return results

		column_info = read_column_schema(target.table, target.fieldname)
		if not column_info:
			row["action"] = SKIP_MISSING_COLUMN
			results.append(row)
			logger.warning("Skipping missing column %s.%s", target.table, target.fieldname)
			return results

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
		"Sales Order commission schema summary: total=%s changed=%s skipped=%s errors=%s actions=%s",
		summary["total"],
		len(summary["changed"]),
		len(summary["skipped"]),
		len(summary["errors"]),
		summary["action_counts"],
	)
	return results


def assert_schema_target(logger=None) -> list[dict[str, Any]]:
	"""Schema guard: tabSales Order.amount_eligible_for_commission must be DECIMAL(30,9) or wider scale=9."""
	logger = logger or frappe.logger("erpnext_extensions.sales_order_commission_decimal_precision")
	target = sales_order_commission_field_target()
	info = read_column_schema(target.table, target.fieldname)
	action = decide_decimal_action(info)
	if action in {SKIP_ALREADY_CORRECT, SKIP_ALREADY_WIDER}:
		logger.info("Sales Order commission schema guard OK for %s.%s", target.table, target.fieldname)
		return []

	failures = [
		{
			"doctype": target.doctype,
			"table": target.table,
			"field": target.fieldname,
			"column_type": info.get("COLUMN_TYPE") if info else None,
			"numeric_precision": info.get("NUMERIC_PRECISION") if info else None,
			"numeric_scale": info.get("NUMERIC_SCALE") if info else None,
			"action": action,
		}
	]
	detail = (
		f"{failures[0]['table']}.{failures[0]['field']}="
		f"{failures[0]['column_type']}/{failures[0]['action']}"
	)
	raise RuntimeError(f"Sales Order amount_eligible_for_commission DECIMAL(30,9) schema drift detected: {detail}")

# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Shared Facility Management DECIMAL(30,9) field lists.

Keep this as an explicit allowlist. Do not widen future Currency fields implicitly.
"""

from __future__ import annotations

TARGET_PRECISION = 30
TARGET_SCALE = 9
TARGET_LENGTH = 30

FACILITY_AMOUNT_DOCTYPES: tuple[str, ...] = (
	"Facility",
	"Facility Repayment",
	"Facility PDC Allocation",
)

TABLES_AND_COLUMNS: dict[str, list[str]] = {
	"tabFacility": [
		"principal_amount",
		"profit_amount",
		"total_liability_amount",
		"opening_paid_principal_amount",
		"opening_paid_profit_amount",
		"opening_paid_penalty_amount",
		"received_amount",
		"paid_principal_amount",
		"paid_profit_amount",
		"paid_penalty_amount",
		"remaining_principal_amount",
		"remaining_profit_amount",
		"remaining_total_amount",
	],
	"tabFacility Repayment": [
		"principal_amount",
		"profit_amount",
		"penalty_amount",
		"total_payment_amount",
	],
	# Child table added after the original Facility precision pass; PDC.cheque_amount
	# is already DECIMAL(30,9) but the fetched allocation copy was still DECIMAL(21,9).
	"tabFacility PDC Allocation": [
		"cheque_amount",
	],
}

# Production overflow field (FAC-2026-07063 / PDC-2026-00359 → 1_500_000_000_000 IRR).
CRITICAL_FACILITY_PDC_ALLOCATION_FIELDS: tuple[str, ...] = ("cheque_amount",)


def all_facility_currency_fieldnames() -> dict[str, list[str]]:
	return {
		"Facility": list(TABLES_AND_COLUMNS["tabFacility"]),
		"Facility Repayment": list(TABLES_AND_COLUMNS["tabFacility Repayment"]),
		"Facility PDC Allocation": list(TABLES_AND_COLUMNS["tabFacility PDC Allocation"]),
	}


def read_facility_column_schema(table: str, column: str) -> dict | None:
	import frappe

	db_name = frappe.db.sql("SELECT DATABASE()")[0][0]
	rows = frappe.db.sql(
		"""
		SELECT
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
	return rows[0] if rows else None


def ensure_facility_length_property_setter(doctype: str, fieldname: str, logger=None) -> str:
	"""Property Setter length=30 so updatedb() does not narrow DECIMAL(30,9)."""
	import frappe
	from frappe.custom.doctype.property_setter.property_setter import make_property_setter
	from frappe.utils import cint

	logger = logger or frappe.logger("erpnext_extensions.facility_precision")
	filters = {
		"doc_type": doctype,
		"field_name": fieldname,
		"property": "length",
		"doctype_or_field": "DocField",
	}
	existing_name = frappe.db.get_value("Property Setter", filters, "name")
	current_value = frappe.db.get_value("Property Setter", filters, "value") if existing_name else None
	if current_value is not None and cint(current_value) >= TARGET_LENGTH:
		return "SKIP_ALREADY_CORRECT"
	if existing_name:
		frappe.db.set_value(
			"Property Setter", existing_name, "value", str(TARGET_LENGTH), update_modified=False
		)
		logger.info("Updated Property Setter %s.%s length -> %s", doctype, fieldname, TARGET_LENGTH)
		return "UPDATE_METADATA_LENGTH"
	make_property_setter(
		doctype,
		fieldname,
		"length",
		str(TARGET_LENGTH),
		"Int",
		is_system_generated=True,
		validate_fields_for_doctype=False,
	)
	logger.info("Created Property Setter %s.%s length -> %s", doctype, fieldname, TARGET_LENGTH)
	return "CREATE_METADATA_LENGTH"


def alter_facility_decimal_column(table: str, column: str, logger=None) -> str:
	"""Idempotent ALTER to DECIMAL(30,9) preserving nullability/default."""
	import frappe
	from frappe.utils import cint, cstr

	logger = logger or frappe.logger("erpnext_extensions.facility_precision")
	info = read_facility_column_schema(table, column)
	if not info:
		return "SKIP_MISSING_COLUMN"
	precision = cint(info.get("NUMERIC_PRECISION"))
	scale = cint(info.get("NUMERIC_SCALE"))
	if cstr(info.get("DATA_TYPE")).lower() == "decimal" and precision >= TARGET_PRECISION and scale >= TARGET_SCALE:
		return "SKIP_ALREADY_CORRECT"
	null_sql = "NULL" if cstr(info.get("IS_NULLABLE")).upper() == "YES" else "NOT NULL"
	default = info.get("COLUMN_DEFAULT")
	default_sql = f" DEFAULT {frappe.db.escape(str(default))}" if default is not None else ""
	frappe.db.sql_ddl(
		f"ALTER TABLE `{table}` MODIFY `{column}` "
		f"DECIMAL({TARGET_PRECISION},{TARGET_SCALE}) {null_sql}{default_sql}"
	)
	logger.info("Updated %s.%s to DECIMAL(%s,%s)", table, column, TARGET_PRECISION, TARGET_SCALE)
	return "ALTER_TO_DECIMAL_30_9"


def assert_facility_pdc_allocation_cheque_amount_schema() -> None:
	info = read_facility_column_schema("tabFacility PDC Allocation", "cheque_amount")
	if not info:
		raise RuntimeError("Missing column tabFacility PDC Allocation.cheque_amount")
	from frappe.utils import cint, cstr

	if (
		cstr(info.get("DATA_TYPE")).lower() != "decimal"
		or cint(info.get("NUMERIC_PRECISION")) != TARGET_PRECISION
		or cint(info.get("NUMERIC_SCALE")) != TARGET_SCALE
	):
		raise RuntimeError(
			"Facility PDC Allocation.cheque_amount schema drift: "
			f"actual={info.get('COLUMN_TYPE')} expected=decimal({TARGET_PRECISION},{TARGET_SCALE})"
		)


def repair_facility_decimal_schema() -> dict:
	"""Re-assert Property Setters + DECIMAL(30,9) for the Facility allowlist."""
	import frappe

	logger = frappe.logger("erpnext_extensions.facility_precision")
	repaired: list[str] = []
	for doctype in FACILITY_AMOUNT_DOCTYPES:
		if not frappe.db.exists("DocType", doctype):
			continue
		for fieldname in all_facility_currency_fieldnames().get(doctype, []):
			ensure_facility_length_property_setter(doctype, fieldname, logger)
			table = f"tab{doctype}"
			action = alter_facility_decimal_column(table, fieldname, logger)
			if action == "ALTER_TO_DECIMAL_30_9":
				repaired.append(f"{table}.{fieldname}")
	assert_facility_pdc_allocation_cheque_amount_schema()
	return {"repaired": repaired}

# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Stock repost DECIMAL(30,9) runtime reliability layer (v5.1.9).

v5.1.8 introduced the authoritative repost monetary registry and one-shot migrate
patches. Forensic diagnosis showed that Frappe ``updatedb()`` will *narrow*
``DECIMAL(30,9)`` back to ``DECIMAL(21,9)`` whenever the Property Setter
``length=30`` is missing from DocField meta.

Because v5.1.8 patches run only once (tabPatch Log), any later metadata/schema
drift left production sites with ``tabStock Entry Detail.basic_amount`` at
``DECIMAL(21,9)`` while code assumed v5.1.8 had permanently hardened it.

v5.1.9:
- Reuses the v5.1.8 registry (no duplicate allowlist)
- Re-applies Property Setters + idempotent SQL repair every migrate (after_migrate)
- Fails migrate loudly if any registered target is not DECIMAL(30,9)
- Exposes ``get_stock_repost_precision_status()`` for support diagnostics
"""

from __future__ import annotations

from typing import Any

import frappe

from erpnext_extensions.approved_decimal_precision import (
	ALTER_TO_DECIMAL_30_9,
	TARGET_PRECISION,
	TARGET_SCALE,
	decide_decimal_action,
	read_column_schema,
)
from erpnext_extensions.stock_repost_decimal_precision_v518 import (
	REPOST_MONETARY_FIELDS_BY_DOCTYPE,
	apply_decimal_schema_targets,
	assert_repost_field_classification_completeness,
	assert_repost_monetary_schema_targets,
	repost_field_targets,
	verify_and_set_metadata,
)

# Mandatory critical fields that must be DECIMAL(30,9) before any stock repost runs.
CRITICAL_STOCK_ENTRY_DETAIL_FIELDS: tuple[str, ...] = (
	"basic_amount",
	"amount",
	"basic_rate",
	"valuation_rate",
)


def assert_stock_repost_decimal_schema(logger=None) -> None:
	"""Fail loudly if any registered stock/repost target is not DECIMAL(30,9)."""
	logger = logger or frappe.logger("erpnext_extensions.stock_repost_decimal_precision_v519")
	assert_repost_monetary_schema_targets(logger)
	_assert_critical_stock_entry_detail_fields()


def _assert_critical_stock_entry_detail_fields() -> None:
	failures: list[str] = []
	for fieldname in CRITICAL_STOCK_ENTRY_DETAIL_FIELDS:
		info = read_column_schema("tabStock Entry Detail", fieldname)
		action = decide_decimal_action(info)
		if action in {"SKIP_ALREADY_CORRECT", "SKIP_ALREADY_WIDER"}:
			continue
		actual = info.get("COLUMN_TYPE") if info else None
		failures.append(
			f"Stock Entry Detail.{fieldname} (tabStock Entry Detail.{fieldname}): "
			f"actual={actual} expected=decimal({TARGET_PRECISION},{TARGET_SCALE})"
		)
	if failures:
		raise RuntimeError(
			"Critical Stock Entry Detail DECIMAL(30,9) schema guarantee failed:\n"
			+ "\n".join(failures)
		)


def get_stock_repost_precision_status() -> dict[str, Any]:
	"""Support/admin diagnostic — not for per-SLE hot path."""
	correct: list[dict[str, Any]] = []
	incorrect: list[dict[str, Any]] = []
	missing: list[dict[str, Any]] = []

	for target in repost_field_targets():
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
		meta = frappe.get_meta(target.doctype, cached=False)
		if not meta.get_field(target.fieldname):
			row["status"] = "missing_field"
			missing.append(row)
			continue
		info = read_column_schema(target.table, target.fieldname)
		if not info:
			row["status"] = "missing_column"
			missing.append(row)
			continue
		row["actual"] = info.get("COLUMN_TYPE")
		row["numeric_precision"] = info.get("NUMERIC_PRECISION")
		row["numeric_scale"] = info.get("NUMERIC_SCALE")
		action = decide_decimal_action(info)
		if action in {"SKIP_ALREADY_CORRECT", "SKIP_ALREADY_WIDER"}:
			row["status"] = "ok"
			correct.append(row)
		else:
			row["status"] = "mismatch"
			row["action"] = action
			incorrect.append(row)

	return {
		"version_layer": "5.1.9",
		"registry": "stock_repost_decimal_precision_v518.REPOST_MONETARY_FIELDS_BY_DOCTYPE",
		"total_registered": len(list(repost_field_targets())),
		"correct": len(correct),
		"incorrect": len(incorrect),
		"missing": len(missing),
		"incorrect_targets": incorrect,
		"missing_targets": missing,
		"critical_stock_entry_detail": {
			field: (read_column_schema("tabStock Entry Detail", field) or {}).get("COLUMN_TYPE")
			for field in CRITICAL_STOCK_ENTRY_DETAIL_FIELDS
		},
	}


def repair_stock_repost_decimal_schema(*, run_completeness_guard: bool = False) -> dict[str, Any]:
	"""Re-apply Property Setters + idempotent ALTER for every v5.1.8 registry target.

	Safe to call from after_migrate and from the v5.1.9 repair patch.
	"""
	logger = frappe.logger("erpnext_extensions.stock_repost_decimal_precision_v519")
	logger.info("Starting stock repost DECIMAL(30,9) repair (v5.1.9)")

	before_status = get_stock_repost_precision_status()
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
			"Stock repost DECIMAL(30,9) repair encountered errors:\n"
			+ "\n".join(f"{row.get('doctype') or row.get('table')}.{row.get('field')}" for row in errors)
		)

	assert_stock_repost_decimal_schema(logger)
	if run_completeness_guard:
		assert_repost_field_classification_completeness()

	after_status = get_stock_repost_precision_status()
	logger.info(
		"Completed stock repost DECIMAL(30,9) repair: repaired=%s incorrect_before=%s incorrect_after=%s",
		len(repaired),
		before_status["incorrect"],
		after_status["incorrect"],
	)
	return {
		"repaired": repaired,
		"before": before_status,
		"after": after_status,
		"metadata_rows": len(metadata_results),
		"schema_rows": len(schema_results),
	}


def after_migrate() -> None:
	"""Recurring migrate hook — re-heal schema drift that one-shot v5.1.8 patches cannot."""
	repair_stock_repost_decimal_schema(run_completeness_guard=False)


# Re-export registry for callers that should not import v518 directly.
__all__ = [
	"CRITICAL_STOCK_ENTRY_DETAIL_FIELDS",
	"REPOST_MONETARY_FIELDS_BY_DOCTYPE",
	"after_migrate",
	"assert_stock_repost_decimal_schema",
	"get_stock_repost_precision_status",
	"repair_stock_repost_decimal_schema",
]

# Copyright (c) 2026, ERPNext Extensions contributors
"""Whitelisted Desk API for Stock Entry Dimension Repair."""

from __future__ import annotations

import frappe

from erpnext_extensions.iran_accounting.manufacturing_dimension.repair import (
	apply_dimension_repair,
	assert_repair_permission,
	dry_run_dimension_repair,
	preview_dimension_repair,
	scan_stock_entry_dimensions,
)


@frappe.whitelist()
def scan(stock_entry: str) -> dict:
	assert_repair_permission()
	return scan_stock_entry_dimensions(stock_entry)


@frappe.whitelist()
def preview(
	stock_entry: str,
	selected_row_names: list | str | None = None,
	target_department: str | None = None,
	target_cost_center: str | None = None,
) -> dict:
	assert_repair_permission()
	return preview_dimension_repair(
		stock_entry,
		_as_list(selected_row_names),
		target_department or "",
		target_cost_center or "",
	)


@frappe.whitelist()
def dry_run(
	stock_entry: str,
	selected_row_names: list | str | None = None,
	target_department: str | None = None,
	target_cost_center: str | None = None,
) -> dict:
	assert_repair_permission()
	return dry_run_dimension_repair(
		stock_entry,
		_as_list(selected_row_names),
		target_department or "",
		target_cost_center or "",
	)


@frappe.whitelist()
def apply(
	stock_entry: str,
	selected_row_names: list | str | None = None,
	target_department: str | None = None,
	target_cost_center: str | None = None,
	fingerprint: str | None = None,
	confirm: bool | int | str = False,
) -> dict:
	assert_repair_permission()
	return apply_dimension_repair(
		stock_entry,
		_as_list(selected_row_names),
		target_department or "",
		target_cost_center or "",
		fingerprint or "",
		confirm=_as_bool(confirm),
	)


def _as_list(value) -> list:
	if value is None:
		return []
	if isinstance(value, str):
		value = value.strip()
		if not value:
			return []
		if value.startswith("["):
			parsed = frappe.parse_json(value)
			return list(parsed) if isinstance(parsed, list) else [str(parsed)]
		return [v.strip() for v in value.split(",") if v.strip()]
	if isinstance(value, (list, tuple, set)):
		return [str(v) for v in value]
	return [str(value)]


def _as_bool(value) -> bool:
	if isinstance(value, bool):
		return value
	if isinstance(value, (int, float)):
		return bool(value)
	return str(value or "").strip().lower() in {"1", "true", "yes", "y"}

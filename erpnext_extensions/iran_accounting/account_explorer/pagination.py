# Copyright (c) 2026, Farbod Siyahpoosh and contributors

from __future__ import annotations

from frappe.utils import flt

from erpnext_extensions.iran_accounting.account_explorer.constants import (
	VIRTUAL_DIMENSION_UNSPECIFIED_PREFIX,
	VIRTUAL_PARTY_UNSPECIFIED_KEY,
	VIRTUAL_UNCLASSIFIED_KEY,
	VIRTUAL_UNIFIED_UNMAPPED_KEY,
)
from erpnext_extensions.iran_accounting.account_explorer.measures import (
	finalize_measures,
	row_has_activity,
	sum_measure_rows,
)
from erpnext_extensions.iran_accounting.account_explorer.schemas import AccountExplorerQuerySpec

# Forbidden synthetic / blank classification display markers (all axes).
_FORBIDDEN_SYNTHETIC_DISPLAY_CODES = frozenset(
	{
		"__UNSPECIFIED__",
		"__UNMAPPED__",
		"__UNCLASSIFIED__",
		"-",
	}
)
_FORBIDDEN_SYNTHETIC_TITLES = frozenset(
	{
		"unclassified",
		"unspecified",
		"unassigned",
		"unmapped",
		"not specified",
		"unknown",
	}
)


def cstr_lower(value):
	return (value or "").casefold()


def is_empty_classification_value(value) -> bool:
	"""True for null / blank / whitespace-only classification keys."""
	if value is None:
		return True
	if isinstance(value, str) and value.strip() == "":
		return True
	return False


def is_empty_classification_presentation_row(row: dict | None) -> bool:
	"""True for forbidden synthetic / blank classification buckets on any axis.

	v5.1.1: Account ``__UNCLASSIFIED__`` is excluded here as well — never a grid row.
	"""
	if not row:
		return False
	row_key = str(row.get("row_key") or "")
	if row_key == VIRTUAL_UNCLASSIFIED_KEY:
		return True
	if row_key == VIRTUAL_PARTY_UNSPECIFIED_KEY:
		return True
	if row_key == VIRTUAL_UNIFIED_UNMAPPED_KEY:
		return True
	if row_key.startswith(VIRTUAL_DIMENSION_UNSPECIFIED_PREFIX):
		return True
	display_code = str(row.get("display_code") or "")
	if display_code in _FORBIDDEN_SYNTHETIC_DISPLAY_CODES:
		return True
	display_title = cstr_lower(row.get("display_title"))
	if display_title in _FORBIDDEN_SYNTHETIC_TITLES:
		return True
	if "dimension_value" in row and is_empty_classification_value(row.get("dimension_value")):
		return True
	# Empty party / unified-party keys are excluded regardless of virtual markers.
	if "party" in row and is_empty_classification_value(row.get("party")):
		return True
	if "party_type" in row and is_empty_classification_value(row.get("party_type")) and "party" in row:
		return True
	if "unified_party" in row and is_empty_classification_value(row.get("unified_party")):
		return True
	if "currency" in row and is_empty_classification_value(row.get("currency")):
		return True
	if "item_code" in row and is_empty_classification_value(row.get("item_code")):
		return True
	if "item_group" in row and is_empty_classification_value(row.get("item_group")):
		return True
	return False


def exclude_empty_classification_rows(rows: list[dict]) -> list[dict]:
	"""Drop forbidden synthetic / blank classification buckets before totals / pagination."""
	return [row for row in rows if not is_empty_classification_presentation_row(row)]


def sort_rows(rows: list[dict], spec: AccountExplorerQuerySpec, sortable_fields) -> list:
	field = spec.pagination.sort_field
	if field not in sortable_fields:
		field = "display_code"
	reverse = spec.pagination.sort_order == "desc"

	def sort_key(row):
		value = row.get(field)
		if isinstance(value, int | float):
			return (0, flt(value))
		return (1, cstr_lower(value))

	return sorted(rows, key=sort_key, reverse=reverse)


def paginate_summary_rows(rows: list[dict], spec: AccountExplorerQuerySpec) -> dict:
	for row in rows:
		finalize_measures(row)

	if spec.hide_zero_rows:
		rows = [row for row in rows if row_has_activity(row)]

	# v4.6.2: empty classification is excluded before aggregation/totals/pagination.
	rows = exclude_empty_classification_rows(rows)
	totals = sum_measure_rows(rows)

	total_rows = len(rows)
	page = spec.pagination.page
	page_size = spec.pagination.page_size
	offset = (page - 1) * page_size
	page_rows = rows[offset : offset + page_size]

	return {
		"rows": page_rows,
		"totals": totals,
		"pagination": {
			"page": page,
			"page_size": page_size,
			"total_rows": total_rows,
			"has_next": offset + page_size < total_rows,
		},
	}


def paginate_stock_summary_rows(
	rows: list[dict], spec: AccountExplorerQuerySpec, *, include_qty: bool = True
) -> dict:
	"""Pagination for Item / Item Group stock measures (qty/value)."""
	from erpnext_extensions.iran_accounting.account_explorer.stock_measures import (
		finalize_stock_measures,
		row_has_stock_activity,
		sum_stock_measure_rows,
	)

	for row in rows:
		finalize_stock_measures(row, include_qty=include_qty)

	if spec.hide_zero_rows:
		rows = [row for row in rows if row_has_stock_activity(row)]

	rows = exclude_empty_classification_rows(rows)
	totals = sum_stock_measure_rows(rows, include_qty=include_qty)

	total_rows = len(rows)
	page = spec.pagination.page
	page_size = spec.pagination.page_size
	offset = (page - 1) * page_size
	page_rows = rows[offset : offset + page_size]

	return {
		"rows": page_rows,
		"totals": totals,
		"pagination": {
			"page": page,
			"page_size": page_size,
			"total_rows": total_rows,
			"has_next": offset + page_size < total_rows,
		},
	}

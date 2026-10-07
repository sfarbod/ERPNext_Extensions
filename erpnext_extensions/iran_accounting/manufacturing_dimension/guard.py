# Copyright (c) 2026, ERPNext Extensions contributors
"""Uniform Department + Cost Center guard for manufacturing Stock Entries.

Applies only to:
- Material Transfer for Manufacture
- Manufacture

Rule: every item row must share exactly one Department and exactly one Cost Center.
Empty and populated values are mixed and must block.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe import _

from erpnext_extensions.iran_accounting.manufacturing_dimension import GUARD_PURPOSES


def _norm(value: Any) -> str:
	return str(value or "").strip()


def _item_rows(doc) -> list:
	rows = []
	items = getattr(doc, "items", None)
	if items is None and hasattr(doc, "get"):
		items = doc.get("items")
	for row in items or []:
		if not getattr(row, "item_code", None):
			continue
		rows.append(row)
	return rows


def collect_dimension_sets(doc) -> tuple[list, set[str], set[str]]:
	rows = _item_rows(doc)
	departments = {_norm(getattr(r, "department", None)) for r in rows}
	cost_centers = {_norm(getattr(r, "cost_center", None)) for r in rows}
	return rows, departments, cost_centers


def is_guard_applicable(doc) -> bool:
	return bool(doc) and getattr(doc, "purpose", None) in GUARD_PURPOSES


def validate_manufacturing_dimension_uniformity(doc, method: str | None = None) -> None:
	"""Authoritative submit-time guard. Also safe to call from validate for early UX."""
	if not is_guard_applicable(doc):
		return

	rows, departments, cost_centers = collect_dimension_sets(doc)
	if not rows:
		return

	dept_mixed = len(departments) > 1
	cc_mixed = len(cost_centers) > 1
	if not dept_mixed and not cc_mixed:
		return

	detail_lines = []
	for row in rows:
		detail_lines.append(
			_(
				"Row {idx}: Item {item_code} | Department={department} | Cost Center={cost_center}"
			).format(
				idx=row.idx,
				item_code=row.item_code,
				department=_norm(getattr(row, "department", None)) or _("(blank)"),
				cost_center=_norm(getattr(row, "cost_center", None)) or _("(blank)"),
			)
		)

	reasons = []
	if dept_mixed:
		reasons.append(_("multiple Department values: {0}").format(", ".join(sorted(departments) or [_("(blank)")])))
	if cc_mixed:
		reasons.append(_("multiple Cost Center values: {0}").format(", ".join(sorted(cost_centers) or [_("(blank)")])))

	frappe.throw(
		_(
			"<b>Manufacturing Dimension Mismatch — Submit Blocked</b><br><br>"
			"All rows in Material Transfer for Manufacture / Manufacture "
			"must use the same Department and Cost Center.<br><br>"
			"Detected: {reasons}.<br><br>"
			"{details}<br><br>"
			"Correct the document manually before submit."
		).format(reasons="; ".join(reasons), details="<br>".join(detail_lines)),
		title=_("Manufacturing Dimension Mismatch — Submit Blocked"),
	)

# Copyright (c) 2026, ERPNext Extensions contributors
"""Complete historical stock-affecting document discovery."""

from __future__ import annotations

import hashlib
from typing import Any

import frappe
from frappe.utils import getdate

from erpnext_extensions.iran_accounting.historical_rebuild.constants import SOURCE_SPECS


def _posting_time_key(value) -> str:
	if value is None:
		return ""
	return str(value)


def _row_identity(row: dict) -> str:
	return "|".join(
		[
			str(row["doctype"]),
			str(row["name"]),
			str(row["posting_date"]),
			_posting_time_key(row.get("posting_time")),
			str(row.get("creation") or ""),
			str(row.get("company") or ""),
		]
	)


def build_source_manifest(
	company: str | None = None,
	posting_boundary_from=None,
) -> list[dict[str, Any]]:
	"""All submitted stock-affecting vouchers in deterministic chronology."""
	boundary = getdate(posting_boundary_from) if posting_boundary_from else None
	rows: list[dict[str, Any]] = []

	for spec in SOURCE_SPECS:
		doctype = spec["doctype"]
		if not frappe.db.exists("DocType", doctype):
			continue
		filters = dict(spec["filters"])
		if company:
			filters["company"] = company
		if boundary:
			filters["posting_date"] = (">=", boundary)

		fields = ["name", "company", "posting_date", "posting_time", "creation"]
		# Some doctypes may lack posting_time
		meta = frappe.get_meta(doctype)
		if not meta.has_field("posting_time"):
			fields = ["name", "company", "posting_date", "creation"]

		found = frappe.get_all(
			doctype,
			filters=filters,
			fields=fields,
			order_by="posting_date asc, creation asc, name asc",
			limit_page_length=0,
		)
		for r in found:
			rows.append(
				{
					"doctype": doctype,
					"name": r.name,
					"company": r.company,
					"posting_date": r.posting_date,
					"posting_time": getattr(r, "posting_time", None),
					"creation": r.creation,
				}
			)

	rows.sort(
		key=lambda r: (
			str(r["posting_date"] or ""),
			_posting_time_key(r.get("posting_time")),
			str(r.get("creation") or ""),
			str(r["doctype"]),
			str(r["name"]),
		)
	)
	# Deduplicate by doctype+name (safety).
	seen: set[tuple[str, str]] = set()
	unique: list[dict[str, Any]] = []
	for r in rows:
		key = (r["doctype"], r["name"])
		if key in seen:
			continue
		seen.add(key)
		unique.append(r)

	for i, r in enumerate(unique, start=1):
		r["ordinal"] = i
	return unique


def manifest_checksum(rows: list[dict[str, Any]]) -> str:
	h = hashlib.sha256()
	for r in rows:
		h.update(_row_identity(r).encode("utf-8"))
		h.update(b"\n")
	return h.hexdigest()


def doctype_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
	out: dict[str, int] = {}
	for r in rows:
		out[r["doctype"]] = out.get(r["doctype"], 0) + 1
	return out

# Copyright (c) 2026, ERPNext Extensions contributors
"""Shared parsers and row helpers for historical stock repair."""

from __future__ import annotations

import json
import re
from typing import Any

from frappe.utils import flt

_NUM = re.compile(r"-?\d+(?:[.,]\d+)?")


def g(row: Any, key: str, default=None):
	if row is None:
		return default
	if isinstance(row, dict):
		return row.get(key, default)
	if hasattr(row, "get"):
		value = row.get(key)
		return default if value is None else value
	return getattr(row, key, default)


def parse_numeric(value) -> float:
	"""Parse Version currency strings such as ``ریال 40,378`` or ``0.0``."""
	if value in (None, ""):
		return 0.0
	if isinstance(value, (int, float)):
		return flt(value)
	text = str(value).replace("\u066c", ",").replace("\u066b", ".")
	text = text.replace(",", "")
	match = _NUM.search(text.replace(" ", ""))
	if not match:
		# try original with commas as thousands
		compact = re.sub(r"[^\d.\-]", "", str(value).replace(",", ""))
		return flt(compact or 0)
	return flt(match.group(0).replace(",", ""))


def parse_version_blob(data) -> dict:
	if not data:
		return {}
	if isinstance(data, dict):
		return data
	try:
		return json.loads(data)
	except (TypeError, ValueError, json.JSONDecodeError):
		return {}


def version_row_rates(blob: dict) -> dict[str, dict]:
	"""Map Stock Entry Detail name → last {field: (old, new)} for rate fields."""
	out: dict[str, dict] = {}
	for entry in blob.get("row_changed") or []:
		if not entry or entry[0] != "items" or len(entry) < 4:
			continue
		rowname = entry[2]
		changes = entry[3] or []
		bucket = out.setdefault(rowname, {})
		for change in changes:
			if not change or len(change) < 3:
				continue
			field, old, new = change[0], change[1], change[2]
			if field in ("basic_rate", "valuation_rate", "basic_amount", "amount"):
				bucket[field] = (parse_numeric(old), parse_numeric(new))
	header = {}
	for change in blob.get("changed") or []:
		if not change or len(change) < 3:
			continue
		field, old, new = change[0], change[1], change[2]
		if field in ("total_outgoing_value", "total_incoming_value", "total_amount"):
			header[field] = (parse_numeric(old), parse_numeric(new))
	if header:
		out["__header__"] = header
	return out


def last_nonzero_from_version(changes: dict) -> float:
	"""Prefer the non-zero side of a Version rate change."""
	for field in ("basic_rate", "valuation_rate"):
		pair = changes.get(field)
		if not pair:
			continue
		old, new = pair
		if abs(old) > 0.0001:
			return old
		if abs(new) > 0.0001:
			return new
	return 0.0


def resolve_company(company=None) -> str:
	"""Resolve company for Historical Repair.

	Never falls back to a hard-coded tenant name. Empty string is treated as missing.
	Order: explicit arg → user default → Global Defaults → throw.
	"""
	import frappe

	company = (company or "").strip() if isinstance(company, str) else company
	if company:
		return company
	company = frappe.defaults.get_user_default("Company")
	if company:
		return company
	company = frappe.db.get_single_value("Global Defaults", "default_company")
	if company:
		return company
	frappe.throw("Company is required for Historical Repair", frappe.ValidationError)


def dump_artifact(subdir: str, name: str, data) -> str | None:
	"""Optionally write JSON under HISTORICAL_REPAIR_ARTIFACT_DIR.

	No-op in published runtime unless the operator explicitly sets the env var.
	Never writes under the app tree or .local-backups by default.
	"""
	import json
	import os

	base = (os.environ.get("HISTORICAL_REPAIR_ARTIFACT_DIR") or "").strip()
	if not base:
		return None
	path = os.path.join(base, subdir, name)
	os.makedirs(os.path.dirname(path) or base, exist_ok=True)
	with open(path, "w", encoding="utf-8") as f:
		json.dump(data, f, indent=2, default=str, ensure_ascii=False)
	return path

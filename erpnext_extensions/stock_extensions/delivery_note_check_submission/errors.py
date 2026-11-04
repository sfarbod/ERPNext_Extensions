# Copyright (c) 2026, ERPNext Extensions contributors
"""Structured validation error helpers for Check Submission."""

from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import strip_html


def normalize_message(exc: BaseException | str) -> str:
	if isinstance(exc, str):
		msg = exc
	else:
		msg = frappe.as_unicode(exc)
		if not msg or msg == type(exc).__name__:
			for entry in reversed(getattr(frappe.local, "message_log", None) or []):
				if isinstance(entry, dict) and entry.get("message"):
					msg = frappe.as_unicode(entry.get("message"))
					break
				if isinstance(entry, str) and entry:
					msg = entry
					break
	return (strip_html(msg) or (type(exc).__name__ if not isinstance(exc, str) else "Error")).strip()


def make_error(
	*,
	category: str,
	message: str | BaseException,
	exc_type: str | None = None,
	item_code: str | None = None,
	row: int | None = None,
	warehouse: str | None = None,
	batch_no: str | None = None,
	serial_no: str | None = None,
	table: str | None = None,
	extra: dict | None = None,
) -> dict[str, Any]:
	out: dict[str, Any] = {
		"category": category,
		"exc_type": exc_type or "ValidationError",
		"message": normalize_message(message),
	}
	if item_code:
		out["item_code"] = item_code
	if row is not None:
		out["row"] = row
	if warehouse:
		out["warehouse"] = warehouse
	if batch_no:
		out["batch_no"] = batch_no
	if serial_no:
		out["serial_no"] = serial_no
	if table:
		out["table"] = table
	if extra:
		out.update(extra)
	return out


def error_fingerprint(err: dict) -> str:
	return "|".join(
		[
			str(err.get("category") or ""),
			str(err.get("exc_type") or ""),
			str(err.get("row") or ""),
			str(err.get("item_code") or ""),
			str(err.get("warehouse") or ""),
			str(err.get("batch_no") or ""),
			str(err.get("message") or ""),
		]
	)


def dedupe_errors(errors: list[dict]) -> list[dict]:
	seen: set[str] = set()
	out: list[dict] = []
	for err in errors:
		key = error_fingerprint(err)
		if key in seen:
			continue
		seen.add(key)
		out.append(err)
	return out


def catch_validation(category: str, fn, *, default_exc_type: str = "ValidationError") -> list[dict]:
	"""Run ``fn`` and return a one-item error list if it raises a non-permission error."""
	try:
		fn()
		return []
	except frappe.PermissionError:
		raise
	except Exception as exc:
		# Clear message_log noise so subsequent groups stay clean.
		frappe.clear_messages()
		return [
			make_error(
				category=category,
				message=exc,
				exc_type=type(exc).__name__ or default_exc_type,
			)
		]

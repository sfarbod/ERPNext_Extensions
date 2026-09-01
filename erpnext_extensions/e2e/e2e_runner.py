"""Stable bench entry point for Playwright prep — avoids Frappe execute eval fallback."""

from __future__ import annotations

import frappe


@frappe.whitelist()
def run_e2e_method(method: str, kwargs: dict | None = None) -> dict:
	"""Invoke an E2E prep callable and return JSON (never raises to bench execute)."""
	method = (method or "").strip()
	if not method:
		return {"ok": False, "error": "method is required", "exc_type": "ValueError"}
	try:
		fn = frappe.get_attr(method)
		result = fn(**(kwargs or {}))
		if frappe.db:
			frappe.db.commit()
		return {"ok": True, "result": result}
	except Exception as exc:
		if frappe.db:
			frappe.db.rollback()
		return {
			"ok": False,
			"error": frappe.as_unicode(exc),
			"exc_type": type(exc).__name__,
		}

"""Submit MAT-STE-2026-40972-1 on development.localhost (diagnostic)."""

from __future__ import annotations

import frappe


def run(name: str = "MAT-STE-2026-40972-1") -> dict:
	from erpnext_extensions.iran_accounting.integration.bootstrap import apply as apply_iran

	apply_iran()
	doc = frappe.get_doc("Stock Entry", name)
	before = {
		"docstatus": doc.docstatus,
		"purpose": doc.purpose,
		"items": len(doc.items or []),
	}
	try:
		doc.submit()
		frappe.db.commit()
		return {
			"ok": True,
			"before": before,
			"docstatus": frappe.db.get_value("Stock Entry", name, "docstatus"),
			"name": name,
		}
	except Exception as e:
		frappe.db.rollback()
		return {
			"ok": False,
			"before": before,
			"error": str(e)[:800],
			"exc_type": type(e).__name__,
		}

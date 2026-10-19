# Copyright (c) 2026, ERPNext Extensions contributors
"""Drain Queued Core RIVs synchronously on development sites (v5.6.1)."""

from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import cint

from erpnext.stock.doctype.repost_item_valuation.repost_item_valuation import (
	execute_reposting_entry,
)


def drain_queued(limit: int = 20) -> dict[str, Any]:
	names = frappe.get_all(
		"Repost Item Valuation",
		filters={"docstatus": 1, "status": "Queued"},
		fields=["name", "item_code", "warehouse", "posting_date"],
		order_by="creation asc",
		limit_page_length=cint(limit) or 20,
	)
	results = []
	for row in names:
		try:
			execute_reposting_entry(row.name)
			frappe.db.commit()
			st = frappe.db.get_value("Repost Item Valuation", row.name, "status")
			results.append({"name": row.name, "status": st, "item_code": row.item_code})
		except Exception as e:
			frappe.db.rollback()
			results.append({"name": row.name, "status": "error", "error": str(e)[:300]})
	return {
		"attempted": len(names),
		"completed": sum(1 for r in results if r.get("status") == "Completed"),
		"failed": sum(1 for r in results if r.get("status") == "Failed"),
		"results": results,
	}

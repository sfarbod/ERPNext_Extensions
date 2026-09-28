# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Apply v5.3.32 safe defaults to existing Asset Depreciation Repair Campaigns.

Does NOT start/stop campaigns. Only updates orchestration fields when the
previous unsafe defaults (chunk_size>=10 or missing max_jes_per_chunk) are present.
"""

from __future__ import annotations

import frappe


def execute() -> None:
	if not frappe.db.exists("DocType", "Asset Depreciation Repair Campaign"):
		return
	if not frappe.db.has_column("Asset Depreciation Repair Campaign", "max_jes_per_chunk"):
		return

	rows = frappe.get_all(
		"Asset Depreciation Repair Campaign",
		fields=["name", "chunk_size", "max_jes_per_chunk", "status"],
		limit_page_length=0,
	)
	for r in rows:
		updates = {}
		if int(r.chunk_size or 0) > 8 or int(r.chunk_size or 0) < 1:
			updates["chunk_size"] = 3
		if not int(r.max_jes_per_chunk or 0):
			updates["max_jes_per_chunk"] = 150
		if updates:
			frappe.db.set_value(
				"Asset Depreciation Repair Campaign",
				r.name,
				updates,
				update_modified=False,
			)
	frappe.db.commit()

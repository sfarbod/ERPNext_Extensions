# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

"""v4.8.8: repair invalid Asset Request document-level flags on permlevel 1.

Role Permissions Manager validates every DocPerm row when adding a role.
Asset Manager permlevel 1 historically had cancel=1 with submit=0, which
raises: "Cannot set Cancel without Submit". Document-level create / submit /
cancel / amend / delete belong at permlevel 0; permlevel 1 is field-level
fulfillment access (read/write).

Also clears cancel when submit is not set on any Asset Request permission
row (Accounts Manager had the same invalid pair at permlevel 0).

Idempotent: only clears flags that are wrongly set. Does not insert rows,
does not copy DocPerm into Custom DocPerm, and does not change unrelated
administrator Custom DocPerm values.
"""

from __future__ import annotations

import frappe
from frappe.utils import cint

DOCTYPE = "Asset Request"
ROLE_ASSET_MANAGER = "Asset Manager"
DOCUMENT_LEVEL_FLAGS = ("create", "submit", "cancel", "amend", "delete")
PERM_TABLES = ("DocPerm", "Custom DocPerm")


def execute():
	if not frappe.db.exists("DocType", DOCTYPE):
		return

	for table in PERM_TABLES:
		_repair_table(table)

	frappe.clear_cache(doctype=DOCTYPE)
	from erpnext_extensions.asset_usage_depreciation.workflow import _enable_employee_self_submit

	_enable_employee_self_submit()


def _repair_table(table: str) -> None:
	if table == "Custom DocPerm" and not frappe.db.table_exists("Custom DocPerm"):
		return
	if not frappe.db.table_exists(table):
		return

	rows = frappe.get_all(
		table,
		filters={"parent": DOCTYPE},
		fields=["name", "role", "permlevel", *DOCUMENT_LEVEL_FLAGS],
	)
	for row in rows:
		updates = {}
		if row.role == ROLE_ASSET_MANAGER and cint(row.permlevel) == 1:
			for flag in DOCUMENT_LEVEL_FLAGS:
				if cint(row.get(flag)):
					updates[flag] = 0
		# Invalid on any level: Cancel requires Submit.
		if cint(row.get("cancel")) and not cint(row.get("submit")):
			updates["cancel"] = 0
		if not updates:
			continue
		frappe.db.set_value(table, row.name, updates, update_modified=False)

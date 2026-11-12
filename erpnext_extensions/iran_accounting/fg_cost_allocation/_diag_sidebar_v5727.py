"""Reproduce Costing sidebar LinkValidationError conditions on development."""

from __future__ import annotations

import json
import traceback

import frappe
from frappe.utils import cint


def main():
	from erpnext_extensions.desk_navigation import (
		_sync_workspace_sidebar,
		has_modern_sidebar,
		sync_module_sidebar_items,
		workspace_sidebar_writable,
	)
	from erpnext_extensions.patches.post_model_sync.ensure_costing_english_desk_v5727 import (
		APP,
		DISPLAY,
		MODULE,
		_SIDEBAR_ITEMS,
	)

	REQUIRED = sorted(
		{
			row["link_to"]
			for row in _SIDEBAR_ITEMS
			if row.get("type") == "Link" and row.get("link_type") == "DocType" and row.get("link_to")
		}
	)

	out = {
		"writable_before_flags": workspace_sidebar_writable(),
		"has_modern_sidebar": has_modern_sidebar(),
		"doctype_exists": {n: bool(frappe.db.exists("DocType", n)) for n in REQUIRED},
	}

	frappe.flags.in_patch = True
	frappe.flags.in_migrate = True
	out["writable_during_patch"] = workspace_sidebar_writable()

	# 1) Healthy sync
	try:
		sync_module_sidebar_items(
			MODULE, _SIDEBAR_ITEMS, title=DISPLAY, header_icon="calculator", app=APP, standard=1
		)
		out["healthy_sync"] = {"ok": True}
	except Exception as e:
		out["healthy_sync"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}

	# 2) Broken link_to → expect LinkValidationError on Workspace Sidebar path
	items = [dict(x) for x in _SIDEBAR_ITEMS]
	for it in items:
		if it.get("link_to") == "MFG Cost Expense Pool":
			it["link_to"] = "This DocType Does Not Exist XYZ"
			break
	try:
		_sync_workspace_sidebar(
			MODULE, items, title="Costing QA Broken", header_icon="calculator", app=APP, standard=0
		)
		out["broken_link_sync"] = {"ok": True, "unexpected": True}
	except Exception as e:
		out["broken_link_sync"] = {
			"ok": False,
			"error_type": type(e).__name__,
			"error": str(e)[:800],
			"is_link_validation": "LinkValidation" in type(e).__name__ or "Could not find" in str(e),
		}
	finally:
		if frappe.db.exists("Workspace Sidebar", "Costing QA Broken"):
			frappe.delete_doc("Workspace Sidebar", "Costing QA Broken", force=1, ignore_permissions=True)

	# 3) Negative-cache simulation: prime cache miss then create? Skip destructive.
	# Instead: get_value cache after exists check
	name = "Finished Goods Cost Allocation Settings"
	frappe.db.value_cache = getattr(frappe.db, "value_cache", {})
	# Force a negative entry the way Document validation uses cache=True
	key = ("DocType", name)
	# Call get_value with cache
	val = frappe.db.get_value("DocType", name, "name", cache=True)
	out["cache_probe"] = {"get_value_cached": val, "exists": bool(frappe.db.exists("DocType", name))}

	frappe.db.commit()
	print(json.dumps(out, indent=2, default=str))
	return out

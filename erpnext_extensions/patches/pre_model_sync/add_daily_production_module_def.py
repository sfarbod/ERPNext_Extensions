# Copyright (c) 2026, ERPNext Extensions contributors
"""v4.7.3: register the new "Daily Production" module before its DocTypes are synced.

``bench migrate`` never calls ``add_module_defs`` for an already-installed app (only
``install-app`` does), and the module map may be served from a cache built before the
folder existed — so a module added to an installed app is silently skipped by
``sync_for``. Create the Module Def and rebuild the module map here (idempotent).
"""

from __future__ import annotations

import frappe

APP = "erpnext_extensions"
MODULE = "Daily Production"


def execute():
	if not frappe.db.exists("Module Def", MODULE):
		frappe.get_doc({"doctype": "Module Def", "module_name": MODULE, "app_name": APP, "custom": 0}).insert(
			ignore_permissions=True, ignore_if_duplicate=True
		)
		frappe.db.commit()

	# Forget every cached module list, then rebuild so sync_for() walks daily_production/.
	frappe.cache.delete_value("app_modules")
	frappe.cache.delete_value("module_app")
	if getattr(frappe, "client_cache", None):
		frappe.client_cache.delete_value("installed_app_modules")
	frappe.local.app_modules = None
	frappe.local.module_app = None
	frappe.setup_module_map()

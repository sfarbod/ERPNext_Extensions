# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Setup for the ``extentionhrms`` module.

Registers the module's **Module Def** and creates its required custom fields on
``after_migrate``.

Why the Module Def is created here: Frappe only auto-creates ``Module Def``
records from ``modules.txt`` during *app installation* (``bench install-app`` →
``add_module_defs``). A module added to ``modules.txt`` of an already-installed
app is **not** picked up by a plain ``bench migrate``, so it never appears in the
Module Def list. Creating it idempotently on ``after_migrate`` guarantees the
module is registered like any standard Frappe module.
"""

from __future__ import annotations

import frappe

from erpnext_extensions.extentionhrms.custom_fields import (
	create_hourly_leave_custom_fields,
	create_payroll_round_off_department_field,
)

MODULE_NAME = "Extentionhrms"
APP_NAME = "erpnext_extensions"


def ensure_module_def() -> None:
	"""Create/repair the ``Module Def`` for this module (idempotent)."""
	if frappe.db.exists("Module Def", MODULE_NAME):
		# Keep it linked to this app (and non-custom) if a stale record exists.
		current = frappe.db.get_value("Module Def", MODULE_NAME, ["app_name", "custom"], as_dict=True)
		if current and (current.app_name != APP_NAME or current.custom):
			frappe.db.set_value("Module Def", MODULE_NAME, {"app_name": APP_NAME, "custom": 0})
		return

	doc = frappe.new_doc("Module Def")
	doc.module_name = MODULE_NAME
	doc.app_name = APP_NAME
	doc.custom = 0
	doc.insert(ignore_permissions=True)


def after_migrate() -> None:
	ensure_module_def()
	create_payroll_round_off_department_field()
	create_hourly_leave_custom_fields()
	# Device-import role, retirement of the sandboxed manual-limit script,
	# and sync of the Excel import Client Script from the app file.
	from erpnext_extensions.extentionhrms.employee_checkin_import_setup import (
		ensure_device_import_setup,
	)

	ensure_device_import_setup()
	frappe.db.commit()

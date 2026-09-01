# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

"""v4.8.9: authorize Asset Request manager-stage actions for the stamped line manager.

Repairs Pending Manager workflow transitions so only ``doc.manager_approver``
(Employee role + native condition) can Approve / Reject / Send Back.
Removes Asset Request Manager role-based bypass at that stage.
Preserves System Manager break-glass Approve → Approved when no later stage.

Provisions DocShare (read/write/submit) and a single ToDo for existing
Pending Manager requests that already have manager_approver. Does not
alter stamps, workflow_state, or guess a manager when the stamp is missing.
"""

from __future__ import annotations

import frappe

from erpnext_extensions.asset_usage_depreciation.workflow import (
	_enable_employee_self_submit,
	_repair_manager_stage_transitions,
	ensure_asset_request_workflow,
)


def execute():
	if not frappe.db.exists("DocType", "Asset Request"):
		return

	ensure_asset_request_workflow()
	_repair_manager_stage_transitions()
	_enable_employee_self_submit()

	from erpnext_extensions.asset_usage_depreciation.services.manager_authorization import (
		provision_existing_pending_manager_requests,
	)

	result = provision_existing_pending_manager_requests()
	frappe.clear_cache(doctype="Asset Request")
	frappe.cache.hdel("workflow", "Asset Request")
	if result.get("unstamped"):
		print(
			f"v4.8.9: {result['unstamped']} Pending Manager Asset Request(s) "
			"have no valid manager_approver and were left unchanged."
		)

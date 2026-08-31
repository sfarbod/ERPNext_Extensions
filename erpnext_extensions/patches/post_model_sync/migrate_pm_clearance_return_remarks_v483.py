# Copyright (c) 2026, ERPNext Extensions contributors
"""v4.8.3: Sync PM Clearance workflow Return transitions (post v4.7.2 cutover only).

Requires authoritative v4.7.2 cutover completion (Patch Log + applied flag).
Aborts before any workflow change when v4.7.2 was deferred or not applied.
"""

from __future__ import annotations

import json

import frappe

from erpnext_extensions.patches.post_model_sync.migrate_pm_draft_approval_v472 import (
	CLEARANCE_PENDING_TITLES,
	assert_pm_draft_approval_v472_cutover_complete,
	_has_return_from_pending_states,
)


def execute():
	assert_pm_draft_approval_v472_cutover_complete()

	report: dict = {"workflow_rebuilt": False, "return_from_pending_ok": False}
	frappe.flags.in_patch = True
	try:
		from erpnext_extensions.patches.post_model_sync.migrate_pm_workflow_v402 import (
			_rebuild_pm_clearance_workflow,
		)

		_rebuild_pm_clearance_workflow()
		report["workflow_rebuilt"] = True
		report["return_from_pending_ok"] = _has_return_from_pending_states(
			"PM Clearance Workflow", CLEARANCE_PENDING_TITLES
		)
	finally:
		frappe.flags.in_patch = False

	frappe.cache().set_value("pm_clearance_return_remarks_v483_report", report)
	print(json.dumps({"pm_clearance_return_remarks_v483": report}, indent=2, default=str))

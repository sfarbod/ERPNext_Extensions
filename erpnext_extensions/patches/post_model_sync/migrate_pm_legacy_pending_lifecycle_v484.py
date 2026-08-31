# Copyright (c) 2026, ERPNext Extensions contributors
"""v4.8.4: Rewind legacy Pending* PM docs (docstatus 1→0) then complete v4.7.2 cutover."""

from __future__ import annotations

import json

import frappe

from erpnext_extensions.patches.post_model_sync.migrate_pm_draft_approval_v472 import (
	_apply_draft_approval_cutover,
	is_pm_draft_approval_v472_cutover_complete,
)
from erpnext_extensions.petty_management.services.legacy_pending_lifecycle_service import (
	V484_APPLIED_FLAG_KEY,
	assert_post_migration_lifecycle_invariants,
	find_legacy_pending_submitted_docs,
	migrate_all_legacy_pending_submitted_docs,
)


def execute():
	frappe.flags.in_patch = True
	report: dict = {
		"legacy_converted": False,
		"v472_cutover_applied": False,
		"already_complete": False,
	}
	try:
		legacy_stats = find_legacy_pending_submitted_docs()
		report["legacy_before"] = legacy_stats

		if legacy_stats["request_count"] or legacy_stats["clearance_count"]:
			conversion = migrate_all_legacy_pending_submitted_docs()
			report["legacy_converted"] = True
			report["conversion"] = conversion

		assert_post_migration_lifecycle_invariants(
			converted_names={
				"requests": [r["name"] for r in report.get("converted_requests") or []],
				"clearances": [r["name"] for r in report.get("converted_clearances") or []],
			}
			if report.get("legacy_converted")
			else None
		)

		if not is_pm_draft_approval_v472_cutover_complete():
			cutover_report: dict = {}
			_apply_draft_approval_cutover(cutover_report)
			report["v472_cutover_applied"] = True
			report["v472_cutover"] = cutover_report
		else:
			report["already_complete"] = True

		frappe.db.set_default(V484_APPLIED_FLAG_KEY, "1")
	finally:
		frappe.flags.in_patch = False

	frappe.cache().set_value("pm_legacy_pending_lifecycle_v484_report", report)
	print(json.dumps({"pm_legacy_pending_lifecycle_v484": report}, indent=2, default=str))

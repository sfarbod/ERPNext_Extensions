# Copyright (c) 2026, Farbod Siyahpoosh and contributors
# For license information, please see license.txt

"""Re-apply Petty Management Module Def + Workspace after migrate (same pattern as Payments sidebar patch)."""


def after_migrate():
	from erpnext_extensions.patches.post_model_sync.add_petty_management_workspace import execute
	from erpnext_extensions.patches.post_model_sync.migrate_pm_draft_approval_v472 import (
		count_in_flight_pending_pm_docs,
		is_draft_approval_workflow_applied,
		try_complete_deferred_pm_draft_approval,
	)
	from erpnext_extensions.patches.post_model_sync.migrate_pm_workflow_v402 import (
		_rebuild_pm_clearance_workflow,
		_rebuild_pm_request_workflow,
		_seed_assignment_rules,
		execute as migrate_v402,
	)

	# v4.0.2 rebuild helpers now encode v4.7.2 Pending*=0.
	# - Already cut over: refresh workflow *definitions* only (no document remaps).
	# - Not cut over + empty Pending* queue: full migrate_v402 (safe first apply).
	# - Not cut over + in-flight Pending*: skip rebuild so submitted Pending*
	#   docs are not left under draft Pending* states.
	if is_draft_approval_workflow_applied():
		_rebuild_pm_request_workflow()
		_rebuild_pm_clearance_workflow()
		_seed_assignment_rules()
	else:
		stats = count_in_flight_pending_pm_docs()
		if stats["request_count"] == 0 and stats["clearance_count"] == 0:
			migrate_v402()
		else:
			print(
				"PM workflow rebuild skipped after_migrate: "
				f"{stats['request_count']} Request / {stats['clearance_count']} Clearance "
				"still in Pending* (draft-approval cutover not yet applied)."
			)

	# Retry deferred v4.7.2 cutover when the Pending* queue becomes empty.
	try_complete_deferred_pm_draft_approval()
	execute()

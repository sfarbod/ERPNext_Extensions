# Copyright (c) 2026, Farbod Siyahpoosh and contributors
"""Bounded retention cleanup for Account Explorer prepared execution artifacts."""

from __future__ import annotations

import frappe
from frappe.utils import add_to_date, now_datetime

from erpnext_extensions.iran_accounting.account_explorer.prepared_report import (
	PREPARED_DOCTYPE,
	_unlink_local_artifact_path,
)

# Keep completed/error/stale execution artifacts for a day so polls/downloads can finish.
AE_PREPARED_RETENTION_HOURS = 24
AE_PREPARED_CLEANUP_BATCH = 40
_TERMINAL = ("Completed", "Error", "Stale")


def cleanup_expired_prepared_executions(batch_size: int | None = None) -> dict:
	"""Delete expired terminal prepared rows and their private gzip artifacts only.

	Never touches Queued/Started rows, GL, or unrelated private files.
	Runs in bounded batches to avoid long locks.
	"""
	limit = int(batch_size or AE_PREPARED_CLEANUP_BATCH)
	cutoff = add_to_date(now_datetime(), hours=-int(AE_PREPARED_RETENTION_HOURS), as_datetime=True)
	names = frappe.get_all(
		PREPARED_DOCTYPE,
		filters={
			"status": ("in", list(_TERMINAL)),
			"modified": ("<", cutoff),
		},
		order_by="modified asc",
		limit=limit,
		pluck="name",
	)
	deleted = 0
	files_removed = 0
	for name in names:
		doc = frappe.get_doc(PREPARED_DOCTYPE, name)
		if doc.status not in _TERMINAL:
			continue
		file_url = doc.result_file
		if file_url:
			file_name = frappe.db.get_value("File", {"file_url": file_url}, "name")
			_unlink_local_artifact_path(file_url)
			files_removed += 1
			if file_name:
				try:
					frappe.delete_doc("File", file_name, ignore_permissions=True, force=True)
				except Exception:
					pass
		frappe.delete_doc(PREPARED_DOCTYPE, name, ignore_permissions=True, force=True)
		deleted += 1
	if deleted:
		frappe.db.commit()
	return {
		"deleted": deleted,
		"files_removed": files_removed,
		"cutoff": str(cutoff),
		"retention_hours": AE_PREPARED_RETENTION_HOURS,
	}

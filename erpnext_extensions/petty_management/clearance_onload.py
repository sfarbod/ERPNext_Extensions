"""PM Clearance onload: repair stale business status vs accounting (v4.0.2).

Never coerces ``workflow_state`` to Settled / Pending JE.
"""

from __future__ import annotations

import frappe
from frappe.model.document import Document

from erpnext_extensions.petty_management.services.clearance_action_policy import (
	compute_lifecycle_status,
	sync_clearance_lifecycle,
)


def sync_pm_clearance_on_load(doc: Document, method=None) -> None:
	if not doc or not getattr(doc, "name", None):
		return
	if doc.doctype != "PM Clearance":
		return

	from erpnext_extensions.petty_management.services.clearance_action_policy import (
		heal_inactive_settlement_reference,
	)

	# CASE B: heal cancelled/missing JE links before status compare.
	heal_inactive_settlement_reference(doc, persist=True)
	doc.reload()

	computed = compute_lifecycle_status(doc)
	stored_status = (frappe.db.get_value("PM Clearance", doc.name, "status") or "").strip()

	if stored_status != computed:
		sync_clearance_lifecycle(doc, persist=True)
		doc.status = frappe.db.get_value("PM Clearance", doc.name, "status")

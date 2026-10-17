# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

"""v5.5.52b — harden Transfer delivery eligibility + refresh list qty fields.

Live hooks and historical reconcile now require Transfer ``to_employee`` to match
the Asset Request employee. Demotes Issued allocations that lack handover proof.
Does not create/delete Asset Movements or change Asset custody.
"""

from __future__ import annotations

import frappe


def execute():
	from erpnext_extensions.asset_usage_depreciation.services.delivery_sync import (
		reconcile_asset_request_fulfillment,
	)

	frappe.logger("asset_request").info("v5.5.52b transfer eligibility harden start")
	summary = reconcile_asset_request_fulfillment(dry_run=False)
	frappe.logger("asset_request").info(
		"v5.5.52b done scanned=%s updated=%s demoted=%s skipped=%s",
		summary.get("scanned"),
		len(summary.get("updated") or []),
		len((summary.get("demote") or {}).get("demoted") or []),
		len(summary.get("skipped") or []),
	)

	sample = "AUD-AR-2026-00008"
	if frappe.db.exists("Asset Request", sample):
		doc = frappe.get_doc("Asset Request", sample)
		try:
			doc.add_comment(
				"Info",
				"v5.5.52b transfer eligibility: "
				f"fulfillment={doc.fulfillment_status}, "
				f"requested={doc.requested_qty}, delivered={doc.delivered_qty}, "
				f"remaining={doc.remaining_qty}",
			)
		except Exception:
			pass

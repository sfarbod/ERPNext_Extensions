# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

"""v5.5.52 — reconcile Asset Request fulfillment from submitted delivery movements.

Idempotent. Does not create Asset Movements. Creates missing allocations only
when a submitted Issue / Transfer-and-Issue / linked Transfer references the
Asset Request. Leaves approval workflow_state/status untouched.
"""

from __future__ import annotations

import frappe


def execute():
	from erpnext_extensions.asset_usage_depreciation.services.delivery_sync import (
		reconcile_asset_request_fulfillment,
	)

	frappe.logger("asset_request").info("v5.5.52 reconcile_asset_request_fulfillment start")
	summary = reconcile_asset_request_fulfillment(dry_run=False)
	frappe.logger("asset_request").info(
		"v5.5.52 reconcile done scanned=%s updated=%s ambiguous=%s skipped=%s",
		summary.get("scanned"),
		len(summary.get("updated") or []),
		len(summary.get("ambiguous") or []),
		len(summary.get("skipped") or []),
	)
	# Persist a compact Comment on the known production-class sample if present.
	sample = "AUD-AR-2026-00008"
	if frappe.db.exists("Asset Request", sample):
		doc = frappe.get_doc("Asset Request", sample)
		try:
			doc.add_comment(
				"Info",
				f"v5.5.52 fulfillment reconcile: status={doc.status}, "
				f"fulfillment={doc.fulfillment_status}, delivered_qty={doc.delivered_qty}",
			)
		except Exception:
			pass

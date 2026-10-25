"""Dev E2E: campaign.status is read-only and progress scopes are consistent."""

from __future__ import annotations

import json

import frappe


def run(campaign: str = "HRC-00003") -> dict:
	from erpnext_extensions.iran_accounting.historical_rebuild import campaign as camp

	sle_before = frappe.db.count("Stock Ledger Entry")
	gl_before = frappe.db.count("GL Entry")
	riv_before = frappe.db.count("Repost Item Valuation")
	doc = frappe.get_doc("Historical Rebuild Campaign", campaign)
	checksum_before = doc.manifest_checksum
	skipped_before = frappe.db.count(
		"Historical Rebuild Campaign Item",
		{"campaign": campaign, "item_status": "Skipped Covered"},
	)
	pending_before = frappe.db.count(
		"Historical Rebuild Campaign Item",
		{"campaign": campaign, "item_status": "Pending"},
	)
	header_pending_before = doc.pending
	header_completed_before = doc.completed
	header_skipped_before = doc.skipped_covered

	out1 = camp.status(campaign)
	doc.reload()

	assert doc.manifest_checksum == checksum_before, "checksum mutated"
	assert frappe.db.count("Stock Ledger Entry") == sle_before
	assert frappe.db.count("GL Entry") == gl_before
	assert frappe.db.count("Repost Item Valuation") == riv_before
	assert (
		frappe.db.count(
			"Historical Rebuild Campaign Item",
			{"campaign": campaign, "item_status": "Skipped Covered"},
		)
		== skipped_before
	)
	assert (
		frappe.db.count(
			"Historical Rebuild Campaign Item",
			{"campaign": campaign, "item_status": "Pending"},
		)
		== pending_before
	)
	assert doc.pending == header_pending_before
	assert doc.completed == header_completed_before
	assert doc.skipped_covered == header_skipped_before

	assert out1.get("read_only") is True
	assert "progress" in out1
	assert "counter_semantics" in out1
	prog = out1["progress"]
	c = out1["counters"]
	assert prog["documents"]["verified"] == c["completed"] + c["skipped_covered"]
	assert prog["documents"]["invariant_ok"] is True
	assert c["total"] == (
		c["completed"] + c["pending"] + c["skipped_covered"] + c["failed"] + c["blocked"]
	)

	out2 = camp.status(campaign)
	assert out2["manifest_checksum"] == out1["manifest_checksum"]
	assert out2["counters"] == out1["counters"]

	return {
		"ok": True,
		"campaign": campaign,
		"status": out1["status"],
		"counters": out1["counters"],
		"progress_documents": prog["documents"],
		"progress_sequential_riv": prog["sequential_riv"],
		"progress_manifest_linked_riv": prog["manifest_linked_riv"],
		"read_only": out1["read_only"],
		"manifest_checksum": out1["manifest_checksum"],
		"sle": sle_before,
		"gl": gl_before,
		"riv": riv_before,
	}


if __name__ == "__main__":
	print(json.dumps(run(), indent=2, default=str))

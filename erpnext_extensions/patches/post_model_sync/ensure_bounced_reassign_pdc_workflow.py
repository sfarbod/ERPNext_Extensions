# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

"""Ensure Bounced → Sent to Bank / Assigned DP Desk Workflow transitions exist.

Reassignment after bounce reuses existing Workflow Actions:
* Send to Bank
* Assign for Debt Purchase

Receivable-only conditions match Registered → Sent / Assigned.
"""

from __future__ import annotations

import frappe


def execute():
	if not frappe.db.exists("Workflow", "PDC Workflow"):
		return

	doc = frappe.get_doc("Workflow", "PDC Workflow")
	changed = False
	existing_tr = {(t.state, t.next_state, t.action) for t in doc.transitions}
	wanted = [
		(
			"Bounced",
			"Sent to Bank",
			"Send to Bank",
			"doc.get('cheque_direction') == 'Receivable'",
		),
		(
			"Bounced",
			"Assigned to Bank for Debt Purchase",
			"Assign for Debt Purchase",
			"doc.get('cheque_direction') == 'Receivable'",
		),
	]
	for state, next_state, action, condition in wanted:
		key = (state, next_state, action)
		if key in existing_tr:
			continue
		doc.append(
			"transitions",
			{
				"state": state,
				"action": action,
				"next_state": next_state,
				"allowed": "All",
				"allow_self_approval": 1,
				"condition": condition,
			},
		)
		changed = True

	if changed:
		doc.save(ignore_permissions=True)

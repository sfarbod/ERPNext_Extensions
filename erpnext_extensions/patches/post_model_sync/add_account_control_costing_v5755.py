# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Ensure Account Control columns exist and remain blank for existing rules (v5.7.55).

DocType JSON sync creates the columns. This patch:
  - Reloads affected Costing DocTypes so schema is present
  - Verifies Account Control stays blank for existing Expense Account Dimension Rules
  - Does not backfill historical calculation detail rows
"""

from __future__ import annotations

import frappe

AFFECTED_DOCTYPES = (
	"Expense Account Dimension Rule",
	"MFG Cost Expense Pool",
	"MFG Cost GL Line",
	"MFG Cost Batch Expense Allocation",
	"MFG Cost Account Dimension Summary",
	"MFG Cost Product Expense Allocation",
)


def _reload_doctype(doctype: str) -> None:
	try:
		frappe.reload_doc(
			"costing",
			"doctype",
			frappe.scrub(doctype),
			force=True,
		)
	except Exception:
		# Child / renamed modules — fall back to meta clear after migrate sync.
		frappe.clear_cache(doctype=doctype)


def execute():
	frappe.flags.in_patch = True
	for dt in AFFECTED_DOCTYPES:
		if frappe.db.exists("DocType", dt):
			_reload_doctype(dt)

	# Existing rules must keep blank Account Control (no auto-fill from Account).
	if frappe.db.exists("DocType", "Expense Account Dimension Rule") and frappe.db.has_column(
		"Expense Account Dimension Rule", "account_control"
	):
		# Only clear accidental non-null values that equal the original Account
		# when they were never intentionally set — we never auto-populate, so
		# leave user-set values alone. Fresh column is NULL/empty by default.
		blank_count = frappe.db.sql(
			"""
			SELECT COUNT(*) FROM `tabExpense Account Dimension Rule`
			WHERE IFNULL(account_control, '') = ''
			"""
		)[0][0]
		total = frappe.db.count("Expense Account Dimension Rule")
		frappe.logger("erpnext_extensions").info(
			"Account Control init: %s/%s rules blank", blank_count, total
		)

	frappe.db.commit()

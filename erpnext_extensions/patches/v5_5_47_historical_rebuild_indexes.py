# Copyright (c) 2026, ERPNext Extensions contributors
"""Unique + performance indexes for Historical Rebuild Campaign Item."""

from __future__ import annotations

import frappe


def execute():
	if not frappe.db.exists("DocType", "Historical Rebuild Campaign Item"):
		return
	# Unique campaign+voucher identity
	try:
		frappe.db.sql(
			"""
			ALTER TABLE `tabHistorical Rebuild Campaign Item`
			ADD UNIQUE INDEX `uniq_hrc_campaign_voucher` (`campaign`, `voucher_type`(140), `voucher_no`(140))
			"""
		)
	except Exception:
		pass
	try:
		frappe.db.sql(
			"""
			ALTER TABLE `tabHistorical Rebuild Campaign Item`
			ADD INDEX `idx_hrc_campaign_status_ordinal` (`campaign`, `item_status`, `ordinal`)
			"""
		)
	except Exception:
		pass

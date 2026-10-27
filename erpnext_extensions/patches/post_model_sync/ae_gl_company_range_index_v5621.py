# Copyright (c) 2026, Farbod Siyahpoosh and contributors
"""Add covering GL Entry index for Account Explorer company-wide aggregates.

Index: (company, is_cancelled, posting_date, account)

Measured on restored Production snapshot (~766k GL rows):
Account Levels single-pass aggregate ~2700ms → ~420ms when forced.
"""

from __future__ import annotations

import frappe

INDEX_NAME = "ae_gl_co_iscancel_posting_acct"


def execute():
	exists = frappe.db.sql(
		"""
		SELECT 1
		FROM information_schema.statistics
		WHERE table_schema = DATABASE()
			AND table_name = 'tabGL Entry'
			AND index_name = %s
		LIMIT 1
		""",
		(INDEX_NAME,),
	)
	if exists:
		return
	frappe.db.sql(
		f"""
		ALTER TABLE `tabGL Entry`
		ADD INDEX `{INDEX_NAME}` (`company`, `is_cancelled`, `posting_date`, `account`)
		"""
	)

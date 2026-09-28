# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Add indexes used by Journal Entry.cancel() link/hook lookups during Asset repair.

Proven on Development (EXPLAIN + timed cancel):

- ``tabDepreciation Schedule.journal_entry``: ALL/165k rows → ref/1 row
- ``tabStock Entry.custom_consignment_recognition_je``: ALL → ref
- ``tabStock Entry.custom_consignment_settlement_je``: ALL → ref

Heavy Asset (120 JEs) cancel time: ~30s → ~11s (~2.7×).

Idempotent — skips each index if it already exists.
Online-friendly: ADD INDEX on InnoDB (may briefly lock writes on large tables;
Production Depreciation Schedule ~180k rows is acceptable for migrate windows).
"""

from __future__ import annotations

import frappe

INDEXES = (
	(
		"tabDepreciation Schedule",
		"Depreciation Schedule",
		"idx_journal_entry",
		"(`journal_entry`)",
	),
	(
		"tabStock Entry",
		"Stock Entry",
		"idx_custom_consignment_recognition_je",
		"(`custom_consignment_recognition_je`)",
	),
	(
		"tabStock Entry",
		"Stock Entry",
		"idx_custom_consignment_settlement_je",
		"(`custom_consignment_settlement_je`)",
	),
)


def _index_exists(table: str, index_name: str) -> bool:
	db_name = frappe.db.sql("SELECT DATABASE()")[0][0]
	return bool(
		frappe.db.sql(
			"""
			SELECT 1
			FROM INFORMATION_SCHEMA.STATISTICS
			WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND INDEX_NAME=%s
			LIMIT 1
			""",
			(db_name, table, index_name),
		)
	)


def execute() -> None:
	for table, doctype, index_name, columns in INDEXES:
		if not frappe.db.exists("DocType", doctype):
			continue
		if not frappe.db.table_exists(doctype):
			continue
		if _index_exists(table, index_name):
			continue
		frappe.db.sql(f"ALTER TABLE `{table}` ADD INDEX `{index_name}` {columns}")
		frappe.logger("erpnext_extensions").info(
			"Added index %s on %s %s", index_name, table, columns
		)

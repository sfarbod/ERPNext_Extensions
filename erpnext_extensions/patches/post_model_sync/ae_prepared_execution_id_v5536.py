# Copyright (c) 2026, Farbod Siyahpoosh and contributors
"""Backfill execution_id and relax fingerprint uniqueness for always-fresh AE (v5.5.36)."""

from __future__ import annotations

import frappe


def execute():
	table = "tabAccount Explorer Prepared Result"
	if not frappe.db.table_exists("Account Explorer Prepared Result"):
		return

	# Ensure column exists even if migrate order is unusual.
	columns = {row[0] for row in frappe.db.sql(f"SHOW COLUMNS FROM `{table}`")}
	if "execution_id" not in columns:
		frappe.db.sql_ddl(
			f"ALTER TABLE `{table}` ADD COLUMN `execution_id` varchar(64) DEFAULT NULL"
		)

	# Backfill legacy rows so the unique index can be applied.
	frappe.db.sql(
		f"""
		UPDATE `{table}`
		SET execution_id = CONCAT('legacy-', name)
		WHERE IFNULL(execution_id, '') = ''
		"""
	)

	# Drop legacy unique index on fingerprint (name varies by MariaDB/Frappe).
	index_rows = frappe.db.sql(f"SHOW INDEX FROM `{table}` WHERE Column_name='fingerprint' AND Non_unique=0")
	for row in index_rows or []:
		key_name = row[2]
		if key_name and key_name != "PRIMARY":
			try:
				frappe.db.sql_ddl(f"ALTER TABLE `{table}` DROP INDEX `{key_name}`")
			except Exception:
				frappe.log_error(title="AE fingerprint unique drop failed")

	# Unique execution_id
	existing = frappe.db.sql(
		f"SHOW INDEX FROM `{table}` WHERE Column_name='execution_id' AND Non_unique=0"
	)
	if not existing:
		try:
			frappe.db.sql_ddl(
				f"ALTER TABLE `{table}` ADD UNIQUE INDEX `unique_execution_id` (`execution_id`)"
			)
		except Exception:
			frappe.log_error(title="AE execution_id unique create failed")

	# Non-unique search index on fingerprint for diagnostics.
	fp_idx = frappe.db.sql(
		f"SHOW INDEX FROM `{table}` WHERE Column_name='fingerprint' AND Key_name='fingerprint'"
	)
	if not fp_idx:
		try:
			frappe.db.sql_ddl(f"ALTER TABLE `{table}` ADD INDEX `fingerprint` (`fingerprint`)")
		except Exception:
			pass

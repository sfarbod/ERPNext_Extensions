# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""CLI helpers for purchasing/asset DECIMAL(30,9) audit (v5.2.4).

bench --site SITE execute \\
  erpnext_extensions.scripts.audit_purchasing_v524.print_post_migration_stats
"""

from __future__ import annotations

from erpnext_extensions.purchasing_decimal_precision_v524 import (
	ALREADY_HARDENED_BY_STOCK_REPOST,
	PURCHASING_MONETARY_FIELDS_BY_DOCTYPE,
	PURCHASING_MONETARY_RATE_FIELDS,
	audit_report_rows,
	get_purchasing_precision_status,
	print_audit_summary,
	purchasing_field_targets,
	purchasing_graph_doctypes,
)


def print_post_migration_stats() -> None:
	print_audit_summary()
	status = get_purchasing_precision_status()
	print("--- status ---")
	for key in (
		"version_layer",
		"total_registered",
		"correct",
		"incorrect",
		"missing",
		"critical_asset",
	):
		print(f"{key}={status.get(key)}")
	if status["incorrect_targets"]:
		print("incorrect_targets:")
		for row in status["incorrect_targets"]:
			print(f"  {row}")
	print("--- counts by doctype (owned) ---")
	for dt, fields in sorted(PURCHASING_MONETARY_FIELDS_BY_DOCTYPE.items()):
		print(f"  {dt}: {len(fields)}")
	print(f"monetary_rate_pairs={len(PURCHASING_MONETARY_RATE_FIELDS)}")
	print(f"already_stock_repost_doctypes={len(ALREADY_HARDENED_BY_STOCK_REPOST)}")
	print(f"graph_doctypes={len(purchasing_graph_doctypes())}")
	print(f"allowlist={len(list(purchasing_field_targets()))}")
	print(f"audit_rows={len(audit_report_rows())}")


def run() -> None:
	print_post_migration_stats()

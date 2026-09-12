"""Print v5.1.8 audit stats."""
from __future__ import annotations

from erpnext_extensions.stock_repost_decimal_precision_v518 import (
	REPOST_MONETARY_FIELDS_BY_DOCTYPE,
	REPOST_MONETARY_RATE_FIELDS,
	audit_report_rows,
	repost_field_targets,
	repost_graph_doctypes,
)


def run() -> None:
	rows = audit_report_rows()
	amount = [r for r in rows if r["classification"] == "Monetary Amount"]
	rates = [r for r in rows if r["classification"] == "Monetary Rate"]
	excluded = [r for r in rows if "Excluded" in r["classification"]]
	print("doctypes", len(repost_graph_doctypes()))
	print("inspected", len(rows))
	print("allowlist", len(repost_field_targets()))
	print("monetary_amount", len(amount))
	print("monetary_rate", len(rates))
	print("excluded", len(excluded))
	print("rate_field_pairs", len(REPOST_MONETARY_RATE_FIELDS))
	print("by_doctype")
	for dt, fields in sorted(REPOST_MONETARY_FIELDS_BY_DOCTYPE.items()):
		print(f"  {dt}: {len(fields)}")

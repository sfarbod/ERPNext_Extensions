# Copyright (c) 2026, ERPNext Extensions contributors
"""Authorized RIV cleanup helpers for TEST / development sites only (v5.6.1).

Never run against Production. Exports an audit snapshot before deletion.
"""

from __future__ import annotations

import json
import os
from typing import Any

import frappe
from frappe.utils import cint, now_datetime


def _assert_dev_site() -> None:
	site = getattr(frappe.local, "site", "") or ""
	marker = frappe.conf.get("e2e_marker") or frappe.conf.get("restored_from_backup")
	if site in ("erp.espadpharmed.com",) or (frappe.conf.get("developer_mode") is None and not marker):
		# Soft guard: require either developer_mode or restore isolation marker
		if not frappe.conf.get("developer_mode") and not marker:
			frappe.throw(
				"riv_cleanup refused: site is not marked as development/test "
				"(set e2e_marker or restored_from_backup / developer_mode)."
			)
	if "espadpharmed.com" in site and "development" not in site:
		frappe.throw(f"riv_cleanup refused on production-like site: {site}")


def export_riv_audit_snapshot(
	riv_names: list[str], *, reason: str, out_dir: str | None = None
) -> str:
	_assert_dev_site()
	bench = frappe.utils.get_bench_path()
	path_dir = out_dir or os.path.join(
		bench, "sites", frappe.local.site, "private", "files", "hrc_riv_cleanup"
	)
	os.makedirs(path_dir, exist_ok=True)
	rows = []
	for name in riv_names:
		row = frappe.db.get_value(
			"Repost Item Valuation",
			name,
			[
				"name",
				"status",
				"item_code",
				"warehouse",
				"posting_date",
				"posting_time",
				"voucher_type",
				"voucher_no",
				"based_on",
				"error_log",
				"docstatus",
			],
			as_dict=True,
		)
		if not row:
			continue
		campaign_items = frappe.db.sql(
			"""
			select name, campaign, item_status, voucher_type, voucher_no
			from `tabHistorical Rebuild Campaign Item`
			where covering_riv=%s
			   or riv_names like %s
			limit 50
			""",
			(name, f"%{name}%"),
			as_dict=True,
		)
		rows.append({"riv": row, "campaign_items": campaign_items or []})
	payload = {
		"exported_at": str(now_datetime()),
		"site": frappe.local.site,
		"reason": reason,
		"count": len(rows),
		"rows": rows,
	}
	path = os.path.join(
		path_dir, f"riv_audit_{now_datetime().strftime('%Y%m%d_%H%M%S')}.json"
	)
	with open(path, "w", encoding="utf-8") as fh:
		json.dump(payload, fh, ensure_ascii=False, indent=2, default=str)
	return path


def cleanup_rivs(
	riv_names: list[str],
	*,
	reason: str,
	force_delete: bool = False,
) -> dict[str, Any]:
	"""Cancel/delete listed RIVs after audit snapshot. Dev/test only."""
	_assert_dev_site()
	names = [n for n in riv_names if n]
	if not names:
		return {"deleted": 0, "cancelled": 0, "skipped": 0, "audit_path": None}

	# Ensure no In Progress
	busy = frappe.db.sql(
		"""
		select name, status from `tabRepost Item Valuation`
		where name in ({})
		  and status='In Progress'
		""".format(",".join(["%s"] * len(names))),
		tuple(names),
		as_dict=True,
	)
	if busy:
		frappe.throw(f"Cannot cleanup RIVs still In Progress: {[b.name for b in busy]}")

	audit_path = export_riv_audit_snapshot(names, reason=reason)
	deleted = 0
	cancelled = 0
	skipped = 0
	for name in names:
		if not frappe.db.exists("Repost Item Valuation", name):
			skipped += 1
			continue
		doc = frappe.get_doc("Repost Item Valuation", name)
		try:
			if cint(doc.docstatus) == 1 and doc.status not in ("Completed",):
				doc.cancel()
				cancelled += 1
			if force_delete or doc.status in ("Failed", "Queued", "Skipped"):
				frappe.delete_doc(
					"Repost Item Valuation",
					name,
					force=1,
					ignore_permissions=True,
				)
				deleted += 1
		except Exception as e:
			frappe.log_error(f"riv_cleanup {name}: {e}")
			skipped += 1
	frappe.db.commit()
	return {
		"deleted": deleted,
		"cancelled": cancelled,
		"skipped": skipped,
		"audit_path": audit_path,
		"reason": reason,
	}

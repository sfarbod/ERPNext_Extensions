# Copyright (c) 2026, ERPNext Extensions contributors
"""Identity/voucher-scoped Historical Rebuild Campaign construction (v5.6.9).

Builds a Manifest from seed vouchers and Item/Warehouse identities by expanding
to all stock vouchers that touch those identities — without post-filtering a
full-company Manifest. Intended for Development validation only.
"""

from __future__ import annotations

import json
from typing import Any

import frappe
from frappe.utils import getdate, now_datetime

from erpnext_extensions.iran_accounting.historical_rebuild.constants import (
	CAMPAIGN_DOCTYPE,
	ITEM_DOCTYPE,
	ITEM_PENDING,
	STATUS_DRAFT,
	STATUS_READY,
)
from erpnext_extensions.iran_accounting.historical_rebuild.discovery import (
	_posting_time_key,
	doctype_counts,
	manifest_checksum,
)

SCOPED_REMARK_PREFIX = "SCOPED_HRC_TEST:"


def is_scoped_campaign(campaign: str) -> bool:
	"""True when campaign remarks carry the scoped-test marker.

	Sequential sessions prepend ``SEQ_SESSION=…``, so the scoped marker may
	not be on the first line — scan all lines.
	"""
	remarks = frappe.db.get_value(CAMPAIGN_DOCTYPE, campaign, "remarks") or ""
	for ln in str(remarks).splitlines():
		if ln.strip().startswith(SCOPED_REMARK_PREFIX):
			return True
	return SCOPED_REMARK_PREFIX in str(remarks)


def load_scoped_allowlist(campaign: str) -> list[tuple[str, str]] | None:
	"""Return Item/Warehouse allowlist for a scoped campaign, else None.

	Full-company campaigns return None so chronological_identities / coverage
	retain their production-wide behaviour.
	"""
	if not is_scoped_campaign(campaign):
		return None
	path = frappe.get_site_path(
		"private", "files", "hrc_scoped_campaigns", f"{campaign}_scope.json"
	)
	try:
		with open(path, encoding="utf-8") as fh:
			payload = json.load(fh)
	except FileNotFoundError:
		return None
	idents = []
	for row in payload.get("identities") or []:
		item = row.get("item_code")
		wh = row.get("warehouse")
		if item and wh:
			idents.append((str(item), str(wh)))
	return list(dict.fromkeys(idents)) or None


def _normalize_seed_vouchers(seed_vouchers: list[dict | tuple] | None) -> list[tuple[str, str]]:
	out: list[tuple[str, str]] = []
	for row in seed_vouchers or []:
		if isinstance(row, dict):
			vt = row.get("voucher_type") or row.get("doctype")
			vn = row.get("voucher_no") or row.get("name")
		else:
			vt, vn = row[0], row[1]
		if vt and vn:
			out.append((str(vt), str(vn)))
	return list(dict.fromkeys(out))


def _normalize_seed_identities(seed_identities: list[dict | tuple] | None) -> list[tuple[str, str]]:
	out: list[tuple[str, str]] = []
	for row in seed_identities or []:
		if isinstance(row, dict):
			item = row.get("item_code")
			wh = row.get("warehouse")
		else:
			item, wh = row[0], row[1]
		if item and wh:
			out.append((str(item), str(wh)))
	return list(dict.fromkeys(out))


def expand_identities_from_vouchers(vouchers: list[tuple[str, str]]) -> list[tuple[str, str]]:
	"""Distinct Item/Warehouse pairs present on seed vouchers' SLEs."""
	if not vouchers:
		return []
	idents: list[tuple[str, str]] = []
	for vt, vn in vouchers:
		rows = frappe.db.sql(
			"""
			select distinct item_code, warehouse
			from `tabStock Ledger Entry`
			where is_cancelled=0 and voucher_type=%s and voucher_no=%s
			  and ifnull(item_code,'')!='' and ifnull(warehouse,'')!=''
			""",
			(vt, vn),
			as_dict=True,
		)
		for r in rows or []:
			idents.append((r.item_code, r.warehouse))
	return list(dict.fromkeys(idents))


def collect_vouchers_for_identities(
	identities: list[tuple[str, str]],
	*,
	posting_boundary_from=None,
	company: str | None = None,
) -> list[dict[str, Any]]:
	"""All stock vouchers that touch the given identities, chronological."""
	if not identities:
		return []
	boundary = getdate(posting_boundary_from) if posting_boundary_from else None
	seen: set[tuple[str, str]] = set()
	rows: list[dict[str, Any]] = []
	for item_code, warehouse in identities:
		params: list[Any] = [item_code, warehouse]
		company_sql = ""
		if company:
			company_sql = " and company=%s"
			params.append(company)
		boundary_sql = ""
		if boundary:
			boundary_sql = " and posting_date>=%s"
			params.append(boundary)
		found = frappe.db.sql(
			f"""
			select voucher_type as doctype, voucher_no as name,
			       min(posting_date) as posting_date,
			       substring_index(
			         group_concat(posting_time order by posting_date, posting_time, creation),
			         ',', 1
			       ) as posting_time,
			       min(creation) as creation,
			       max(company) as company
			from `tabStock Ledger Entry`
			where is_cancelled=0
			  and item_code=%s and warehouse=%s
			  {company_sql}
			  {boundary_sql}
			group by voucher_type, voucher_no
			""",
			tuple(params),
			as_dict=True,
		)
		for r in found or []:
			key = (r.doctype, r.name)
			if key in seen:
				continue
			seen.add(key)
			rows.append(
				{
					"doctype": r.doctype,
					"name": r.name,
					"company": r.company,
					"posting_date": r.posting_date,
					"posting_time": r.posting_time,
					"creation": r.creation,
				}
			)

	rows.sort(
		key=lambda r: (
			str(r.get("posting_date") or ""),
			_posting_time_key(r.get("posting_time")),
			str(r.get("creation") or ""),
			str(r.get("doctype") or ""),
			str(r.get("name") or ""),
		)
	)
	for i, r in enumerate(rows, start=1):
		r["ordinal"] = i
	return rows


def build_scoped_manifest(
	*,
	company: str | None = None,
	seed_vouchers: list[dict | tuple] | None = None,
	seed_identities: list[dict | tuple] | None = None,
	posting_boundary_from=None,
) -> dict[str, Any]:
	"""Expand seeds → identities → dependency vouchers (deterministic)."""
	vouchers = _normalize_seed_vouchers(seed_vouchers)
	idents = _normalize_seed_identities(seed_identities)
	idents = list(dict.fromkeys(idents + expand_identities_from_vouchers(vouchers)))
	if not idents:
		frappe.throw("scoped manifest requires seed_vouchers and/or seed_identities")

	rows = collect_vouchers_for_identities(
		idents,
		posting_boundary_from=posting_boundary_from,
		company=company,
	)
	# Ensure every seed voucher is present even if somehow missed (safety).
	present = {(r["doctype"], r["name"]) for r in rows}
	for vt, vn in vouchers:
		if (vt, vn) in present:
			continue
		meta = frappe.db.get_value(
			vt,
			vn,
			["company", "posting_date", "posting_time", "creation"],
			as_dict=True,
		)
		if not meta:
			frappe.throw(f"Seed voucher missing: {vt} {vn}")
		rows.append(
			{
				"doctype": vt,
				"name": vn,
				"company": meta.company,
				"posting_date": meta.posting_date,
				"posting_time": getattr(meta, "posting_time", None),
				"creation": meta.creation,
			}
		)
	rows.sort(
		key=lambda r: (
			str(r.get("posting_date") or ""),
			_posting_time_key(r.get("posting_time")),
			str(r.get("creation") or ""),
			str(r.get("doctype") or ""),
			str(r.get("name") or ""),
		)
	)
	for i, r in enumerate(rows, start=1):
		r["ordinal"] = i

	return {
		"rows": rows,
		"identities": [{"item_code": i, "warehouse": w} for i, w in idents],
		"seed_vouchers": [{"voucher_type": a, "voucher_no": b} for a, b in vouchers],
		"checksum": manifest_checksum(rows),
		"doctype_counts": doctype_counts(rows),
		"total": len(rows),
		"identity_count": len(idents),
	}


def create_scoped_campaign(
	*,
	company: str | None = None,
	seed_vouchers: list[dict | tuple] | None = None,
	seed_identities: list[dict | tuple] | None = None,
	posting_boundary_from=None,
	scope_label: str = "5.6.9-validation",
	remarks: str | None = None,
) -> dict[str, Any]:
	"""Create Draft scoped campaign and build its Manifest in one step."""
	if not company:
		company = frappe.db.get_single_value("Global Defaults", "default_company")
	if not company:
		company = frappe.db.get_value("Company", {"is_group": 0}, "name", order_by="creation asc")
	if not company:
		frappe.throw("company is required")

	built = build_scoped_manifest(
		company=company,
		seed_vouchers=seed_vouchers,
		seed_identities=seed_identities,
		posting_boundary_from=posting_boundary_from,
	)
	scope_payload = {
		"label": scope_label,
		"seed_vouchers": built["seed_vouchers"],
		"identities": built["identities"],
		"posting_boundary_from": str(posting_boundary_from) if posting_boundary_from else None,
		"identity_count": built["identity_count"],
		"manifest_total": built["total"],
	}
	# Always keep SCOPED_HRC_TEST marker so allowlist filtering cannot be
	# accidentally disabled by custom remarks (SEQ_SESSION may prepend later).
	base = remarks.strip() if remarks else scope_label
	if SCOPED_REMARK_PREFIX not in base:
		base = f"{SCOPED_REMARK_PREFIX}{base}"
	doc = frappe.get_doc(
		{
			"doctype": CAMPAIGN_DOCTYPE,
			"company": company,
			"status": STATUS_DRAFT,
			"posting_boundary_from": posting_boundary_from,
			"remarks": base[:1000],
		}
	)
	doc.insert(ignore_permissions=True)
	frappe.db.commit()

	# Persist scope JSON under site private files.
	import os

	scope_dir = frappe.get_site_path("private", "files", "hrc_scoped_campaigns")
	os.makedirs(scope_dir, exist_ok=True)
	scope_path = os.path.join(scope_dir, f"{doc.name}_scope.json")
	with open(scope_path, "w", encoding="utf-8") as fh:
		json.dump(scope_payload, fh, indent=2, default=str)

	# Insert campaign items from scoped rows (not full discovery).
	for r in built["rows"]:
		frappe.get_doc(
			{
				"doctype": ITEM_DOCTYPE,
				"campaign": doc.name,
				"ordinal": r["ordinal"],
				"company": r.get("company") or company,
				"voucher_type": r["doctype"],
				"voucher_no": r["name"],
				"posting_date": r["posting_date"],
				"posting_time": str(r.get("posting_time") or ""),
				"creation_ts": r.get("creation"),
				"item_status": ITEM_PENDING,
			}
		).insert(ignore_permissions=True)
	frappe.db.commit()

	frappe.db.set_value(
		CAMPAIGN_DOCTYPE,
		doc.name,
		{
			"status": STATUS_READY,
			"manifest_checksum": built["checksum"],
			"total_documents": built["total"],
			"completed": 0,
			"pending": built["total"],
			"failed": 0,
			"blocked": 0,
			"skipped_covered": 0,
			"last_ordinal": 0,
			"last_activity": now_datetime(),
		},
		update_modified=False,
	)
	frappe.db.commit()

	return {
		"campaign": doc.name,
		"status": STATUS_READY,
		"company": company,
		"scoped": True,
		"scope_label": scope_label,
		"scope_path": scope_path,
		"total": built["total"],
		"identity_count": built["identity_count"],
		"identities": built["identities"],
		"seed_vouchers": built["seed_vouchers"],
		"checksum": built["checksum"],
		"doctype_counts": built["doctype_counts"],
		"expected_riv_count": built["identity_count"],
	}


def default_569_validation_scope() -> dict[str, Any]:
	"""Canonical seeds for the 5.6.9 scoped validation campaign."""
	seed_vouchers = [{"voucher_type": "Stock Entry", "voucher_no": "MAT-STE-2026-27800"}]
	seed_identities = [
		{"item_code": "30100055", "warehouse": "انبار پایکار خط تولید اسپاد فارمد"},
		{"item_code": "30200041", "warehouse": "انبار Quarantine محصول نیمه ساخته اسپاد"},
		{"item_code": "30200041", "warehouse": "انبار Reject محصولات نیمه ساخته"},
		{"item_code": "30200041", "warehouse": "انبار پایکار خط تولید اسپاد فارمد"},
	]
	# Production failure identities (original RIV anchors).
	for riv_name in ("h6vr92mt90", "tnf88cqqer", "7oivgc47g8"):
		row = frappe.db.get_value(
			"Repost Item Valuation",
			riv_name,
			["item_code", "warehouse"],
			as_dict=True,
		)
		if row and row.item_code and row.warehouse:
			seed_identities.append({"item_code": row.item_code, "warehouse": row.warehouse})
	# Deduplicate
	seen = set()
	uniq = []
	for r in seed_identities:
		key = (r["item_code"], r["warehouse"])
		if key in seen:
			continue
		seen.add(key)
		uniq.append(r)
	return {"seed_vouchers": seed_vouchers, "seed_identities": uniq}

# Copyright (c) 2026, ERPNext Extensions contributors
"""Service helpers for Historical Rebuild REST API (no whitelist here).

Delegates to existing campaign / scoped / sequential / integrity modules.
"""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

import frappe
from frappe.utils import cint, flt, now_datetime

from erpnext_extensions.iran_accounting.historical_rebuild.constants import (
	CAMPAIGN_DOCTYPE,
	ITEM_DOCTYPE,
	STATUS_READY,
	STATUS_RUNNING,
)
from erpnext_extensions.iran_accounting.historical_rebuild.discovery import manifest_checksum
from erpnext_extensions.iran_accounting.historical_rebuild.scoped_campaign import (
	_normalize_seed_identities,
	_normalize_seed_vouchers,
	build_scoped_manifest,
	expand_identities_from_vouchers,
	is_scoped_campaign,
	load_scoped_allowlist,
)
from erpnext_extensions.iran_accounting.historical_stock.permissions import (
	require_read,
	require_repair,
)

API_VERSION = "1.0.0"
MAX_VOUCHERS = 200
MAX_IDENTITIES = 2000
ALLOWED_VOUCHER_TYPES = frozenset(
	{
		"Stock Entry",
		"Purchase Receipt",
		"Delivery Note",
		"Stock Reconciliation",
		"Subcontracting Receipt",
		"Purchase Invoice",
		"Sales Invoice",
	}
)


def _parse_jsonish(value):
	if isinstance(value, str):
		value = value.strip()
		if not value:
			return None
		try:
			return json.loads(value)
		except json.JSONDecodeError:
			return value
	return value


def parse_voucher_list(vouchers) -> list[tuple[str, str]]:
	parsed = _parse_jsonish(vouchers)
	if not parsed:
		frappe.throw("vouchers is required (non-empty list)", frappe.ValidationError)
	normalized = _normalize_seed_vouchers(parsed if isinstance(parsed, list) else [parsed])
	if not normalized:
		frappe.throw("vouchers is required (non-empty list)", frappe.ValidationError)
	if len(normalized) > MAX_VOUCHERS:
		frappe.throw(f"vouchers exceed max {MAX_VOUCHERS}", frappe.ValidationError)
	for vt, _vn in normalized:
		if vt not in ALLOWED_VOUCHER_TYPES:
			frappe.throw(f"unsupported voucher_type: {vt}", frappe.ValidationError)
	return normalized


def parse_identity_list(identities) -> list[tuple[str, str]]:
	parsed = _parse_jsonish(identities)
	if parsed is None:
		return []
	if not isinstance(parsed, list):
		frappe.throw("identities_allowlist must be a list", frappe.ValidationError)
	out = _normalize_seed_identities(parsed)
	if len(out) > MAX_IDENTITIES:
		frappe.throw(f"identities exceed max {MAX_IDENTITIES}", frappe.ValidationError)
	return out


def require_company(company: str | None) -> str:
	if not company:
		frappe.throw("company is required", frappe.ValidationError)
	if not frappe.db.exists("Company", company):
		frappe.throw(f"unknown company: {company}", frappe.ValidationError)
	return company


def reject_unknown_keys(payload: dict | None, allowed: set[str], *, label: str) -> None:
	if not payload:
		return
	unknown = sorted(set(payload) - allowed)
	if unknown:
		frappe.throw(f"{label}: unexpected fields {unknown}", frappe.ValidationError)


def append_api_audit(campaign: str, message: str, *, level: str = "INFO") -> None:
	from erpnext_extensions.iran_accounting.historical_rebuild.campaign import _append_log

	user = frappe.session.user
	_append_log(campaign, f"[api:{user}] {message}", level=level)


def get_rebuild_api_status(*, company: str | None = None) -> dict[str, Any]:
	require_read()
	versions = {}
	try:
		from frappe.utils.change_log import get_versions

		raw = get_versions()
		for app in ("frappe", "erpnext", "hrms", "erpnext_extensions"):
			if app in raw:
				versions[app] = (raw[app] or {}).get("version")
	except Exception:
		versions = {"error": "version_lookup_failed"}

	active_riv = frappe.get_all(
		"Repost Item Valuation",
		filters={"status": ("in", ["Queued", "In Progress"]), "docstatus": 1},
		fields=["name", "status", "item_code", "warehouse", "posting_date", "modified"],
		limit_page_length=50,
		order_by="modified desc",
	)
	campaign_filters = {}
	if company:
		campaign_filters["company"] = company
	active_campaigns = frappe.get_all(
		CAMPAIGN_DOCTYPE,
		filters={**campaign_filters, "status": ("in", ["Running", "Paused", "Ready"])},
		fields=["name", "status", "company", "modified", "manifest_checksum", "total_documents"],
		order_by="modified desc",
		limit_page_length=20,
	)
	from erpnext_extensions import __version__ as app_version
	from erpnext_extensions.iran_accounting.historical_rebuild.sequential_rebuild import (
		get_seq_session,
		parse_campaign_session_id,
	)

	sess = get_seq_session()
	return {
		"ok": True,
		"read_only": True,
		"site": getattr(frappe.local, "site", None),
		"user": frappe.session.user,
		"api_version": API_VERSION,
		"app_version": app_version,
		"versions": versions,
		"active_queued_riv_count": len(active_riv),
		"active_queued_rivs": active_riv,
		"active_campaigns": active_campaigns,
		"sequential_session": {
			"session_id": (sess or {}).get("session_id"),
			"campaign": (sess or {}).get("campaign"),
			"status": (sess or {}).get("status"),
		}
		if sess
		else None,
		"capabilities": {
			"get_rebuild_api_status": True,
			"inspect_vouchers": True,
			"discover_dependencies": True,
			"create_exact_campaign": True,
			"preflight_campaign": True,
			"start_sequential_campaign": True,
			"get_campaign_status": True,
			"request_campaign_stop": True,
			"validate_campaign_results": True,
			"prepare_clean_start_exposed": False,
			"site_wide_riv_exposed": False,
		},
		"notes": [
			"Campaign create/start require System Manager (repair) role.",
			"prepare_sequential_rebuild / prepare_clean_start are intentionally not exposed.",
			"execute_repost_item_valuation is intentionally not exposed.",
		],
	}


def inspect_vouchers(*, company: str, vouchers) -> dict[str, Any]:
	require_read()
	company = require_company(company)
	pairs = parse_voucher_list(vouchers)
	rows = []
	for vt, vn in pairs:
		if not frappe.db.exists(vt, vn):
			rows.append({"voucher_type": vt, "voucher_no": vn, "exists": False})
			continue
		doc = frappe.get_doc(vt, vn)
		if getattr(doc, "company", None) and doc.company != company:
			rows.append(
				{
					"voucher_type": vt,
					"voucher_no": vn,
					"exists": True,
					"company_mismatch": True,
					"company": doc.company,
				}
			)
			continue
		items = []
		child = getattr(doc, "items", None) or []
		for it in child:
			items.append(
				{
					"name": it.name,
					"item_code": it.item_code,
					"qty": flt(it.qty),
					"transfer_qty": flt(getattr(it, "transfer_qty", 0) or 0),
					"s_warehouse": getattr(it, "s_warehouse", None),
					"t_warehouse": getattr(it, "t_warehouse", None),
					"basic_rate": flt(getattr(it, "basic_rate", 0) or 0),
					"valuation_rate": flt(getattr(it, "valuation_rate", 0) or 0),
					"basic_amount": flt(getattr(it, "basic_amount", 0) or 0),
					"amount": flt(getattr(it, "amount", 0) or 0),
					"additional_cost": flt(getattr(it, "additional_cost", 0) or 0),
					"landed_cost_voucher_amount": flt(
						getattr(it, "landed_cost_voucher_amount", 0) or 0
					),
					"is_finished_item": cint(getattr(it, "is_finished_item", 0) or 0),
					"is_scrap_item": cint(getattr(it, "is_scrap_item", 0) or 0),
				}
			)
		sles = frappe.get_all(
			"Stock Ledger Entry",
			filters={"voucher_type": vt, "voucher_no": vn, "is_cancelled": 0},
			fields=[
				"name",
				"item_code",
				"warehouse",
				"actual_qty",
				"stock_value_difference",
				"valuation_rate",
				"serial_and_batch_bundle",
				"posting_date",
				"posting_time",
			],
			limit_page_length=2000,
		)
		gls = frappe.get_all(
			"GL Entry",
			filters={"voucher_type": vt, "voucher_no": vn, "is_cancelled": 0},
			fields=["name", "account", "debit", "credit"],
			limit_page_length=2000,
		)
		sa_debit = sa_credit = oh_debit = oh_credit = 0.0
		for g in gls:
			acc = g.account or ""
			if "621301" in acc or "تعدیلات موجودی" in acc:
				sa_debit += flt(g.debit)
				sa_credit += flt(g.credit)
			if "511101" in acc or acc.startswith("511101"):
				oh_debit += flt(g.debit)
				oh_credit += flt(g.credit)
		gl_debit = sum(flt(g.debit) for g in gls)
		gl_credit = sum(flt(g.credit) for g in gls)
		rows.append(
			{
				"voucher_type": vt,
				"voucher_no": vn,
				"exists": True,
				"docstatus": cint(doc.docstatus),
				"purpose": getattr(doc, "purpose", None),
				"stock_entry_type": getattr(doc, "stock_entry_type", None),
				"company": getattr(doc, "company", None),
				"posting_date": str(getattr(doc, "posting_date", "") or ""),
				"posting_time": str(getattr(doc, "posting_time", "") or ""),
				"total_additional_costs": flt(getattr(doc, "total_additional_costs", 0) or 0),
				"value_difference": flt(getattr(doc, "value_difference", 0) or 0),
				"items": items,
				"sle_count": len(sles),
				"sle_sum": sum(flt(s.stock_value_difference) for s in sles),
				"gl_count": len(gls),
				"gl_debit": gl_debit,
				"gl_credit": gl_credit,
				"gl_balanced": abs(gl_debit - gl_credit) < 0.5,
				"stock_adjustment": {
					"debit": sa_debit,
					"credit": sa_credit,
					"net": sa_debit - sa_credit,
					"gross": sa_debit + sa_credit,
				},
				"overhead": {
					"debit": oh_debit,
					"credit": oh_credit,
					"net": oh_debit - oh_credit,
				},
				"sles_sample": sles[:20],
			}
		)
	return {"ok": True, "read_only": True, "company": company, "count": len(rows), "vouchers": rows}


def discover_dependencies(
	*,
	company: str,
	vouchers,
	mode: str = "exact",
	posting_boundary_from: str | None = None,
) -> dict[str, Any]:
	"""Build proposed allowlist + checksum without creating a campaign.

	mode=exact: identities from seed vouchers only; manifest = seed vouchers.
	mode=identity_closure: expand to all vouchers touching those identities
	(same as build_scoped_manifest) — preview only unless create_scoped is used.
	"""
	require_read()
	company = require_company(company)
	pairs = parse_voucher_list(vouchers)
	mode = (mode or "exact").strip().lower()
	if mode not in ("exact", "identity_closure"):
		frappe.throw("mode must be 'exact' or 'identity_closure'", frappe.ValidationError)

	seed_idents = expand_identities_from_vouchers(pairs)
	by_voucher: dict[str, list[dict]] = {}
	shared_map: dict[tuple[str, str], list[str]] = defaultdict(list)
	for vt, vn in pairs:
		sles = frappe.get_all(
			"Stock Ledger Entry",
			filters={"voucher_type": vt, "voucher_no": vn, "is_cancelled": 0},
			fields=["item_code", "warehouse", "posting_date", "posting_time"],
			limit_page_length=5000,
		)
		key = f"{vt}/{vn}"
		by_voucher[key] = []
		seen = set()
		for s in sles:
			pair = (s.item_code, s.warehouse)
			if not s.item_code or not s.warehouse or pair in seen:
				continue
			seen.add(pair)
			by_voucher[key].append(
				{
					"item_code": s.item_code,
					"warehouse": s.warehouse,
					"posting_date": str(s.posting_date),
					"posting_time": str(s.posting_time or ""),
				}
			)
			shared_map[pair].append(key)

	earliest = []
	for item, wh in seed_idents:
		row = frappe.db.sql(
			"""
			select min(posting_date) as posting_date,
			       substring_index(
			         group_concat(posting_time order by posting_date, posting_time, creation),
			         ',', 1
			       ) as posting_time,
			       count(*) as sle_count
			from `tabStock Ledger Entry`
			where is_cancelled=0 and item_code=%s and warehouse=%s and company=%s
			""",
			(item, wh, company),
			as_dict=True,
		)
		r = (row or [None])[0]
		earliest.append(
			{
				"item_code": item,
				"warehouse": wh,
				"earliest_posting_date": str(r.posting_date) if r and r.posting_date else None,
				"earliest_posting_time": str(r.posting_time) if r and r.posting_time else None,
				"sle_count": cint(r.sle_count) if r else 0,
				"shared_by_seeds": shared_map.get((item, wh), []),
			}
		)

	if mode == "exact":
		rows = []
		for vt, vn in pairs:
			meta = frappe.db.get_value(
				vt, vn, ["company", "posting_date", "posting_time", "creation"], as_dict=True
			)
			if not meta:
				frappe.throw(f"Voucher missing: {vt} {vn}")
			rows.append(
				{
					"doctype": vt,
					"name": vn,
					"company": meta.company or company,
					"posting_date": meta.posting_date,
					"posting_time": getattr(meta, "posting_time", None),
					"creation": meta.creation,
				}
			)
		rows.sort(
			key=lambda r: (
				str(r.get("posting_date") or ""),
				str(r.get("posting_time") or ""),
				str(r.get("creation") or ""),
				str(r.get("doctype") or ""),
				str(r.get("name") or ""),
			)
		)
		for i, r in enumerate(rows, start=1):
			r["ordinal"] = i
		idents = [{"item_code": i, "warehouse": w} for i, w in seed_idents]
		checksum = manifest_checksum(rows)
		manifest_total = len(rows)
		expanded = None
	else:
		built = build_scoped_manifest(
			company=company,
			seed_vouchers=[{"voucher_type": a, "voucher_no": b} for a, b in pairs],
			posting_boundary_from=posting_boundary_from,
		)
		idents = built["identities"]
		checksum = built["checksum"]
		manifest_total = built["total"]
		expanded = {
			"doctype_counts": built["doctype_counts"],
			"sample_vouchers": [
				{"doctype": r["doctype"], "name": r["name"], "posting_date": str(r["posting_date"])}
				for r in built["rows"][:30]
			],
		}
		rows = built["rows"]

	dates = [str(r.get("posting_date") or "") for r in rows if r.get("posting_date")]
	return {
		"ok": True,
		"read_only": True,
		"company": company,
		"mode": mode,
		"seed_vouchers": [{"voucher_type": a, "voucher_no": b} for a, b in pairs],
		"identities": idents,
		"identity_count": len(idents),
		"expected_riv_count": len(idents),
		"manifest_total": manifest_total,
		"checksum": checksum,
		"posting_date_from": min(dates) if dates else None,
		"posting_date_to": max(dates) if dates else None,
		"identities_detail": earliest,
		"identities_by_seed": by_voucher,
		"shared_identities": [
			{"item_code": i, "warehouse": w, "seeds": seeds}
			for (i, w), seeds in shared_map.items()
			if len(seeds) > 1
		],
		"exclusions": {
			"previous_campaigns_not_modified": True,
			"prepare_clean_start": True,
			"site_wide_riv": True,
		},
		"identity_closure_preview": expanded,
		"warning": (
			None
			if mode == "exact"
			else "identity_closure expands beyond seed vouchers; create_exact_campaign "
			"uses exact seeds only. Use create_scoped (Bench) for expanded manifests."
		),
	}


def create_exact_campaign(
	*,
	company: str,
	vouchers,
	identities_allowlist,
	expected_checksum: str,
	scope_label: str = "rest-exact-campaign",
	remarks: str | None = None,
	confirm: str | None = None,
) -> dict[str, Any]:
	require_repair()
	company = require_company(company)
	if str(confirm or "").strip() != "CREATE_EXACT_CAMPAIGN":
		frappe.throw(
			"confirm must equal CREATE_EXACT_CAMPAIGN",
			frappe.ValidationError,
		)
	pairs = parse_voucher_list(vouchers)
	allow = parse_identity_list(identities_allowlist)
	if not allow:
		frappe.throw("identities_allowlist is required", frappe.ValidationError)
	if not expected_checksum:
		frappe.throw("expected_checksum is required", frappe.ValidationError)

	from erpnext_extensions.iran_accounting.historical_rebuild.scoped_campaign import (
		create_exact_voucher_campaign,
	)

	result = create_exact_voucher_campaign(
		company=company,
		vouchers=[{"voucher_type": a, "voucher_no": b} for a, b in pairs],
		scope_label=scope_label,
		remarks=remarks or scope_label,
		identities_allowlist=[{"item_code": i, "warehouse": w} for i, w in allow],
		expected_checksum=expected_checksum,
	)
	append_api_audit(
		result["campaign"],
		f"created exact campaign checksum={result['checksum'][:12]}… "
		f"vouchers={result['total']} identities={result['identity_count']}",
	)
	result["ok"] = True
	result["created_by"] = frappe.session.user
	result["created_at"] = str(now_datetime())
	result["api_version"] = API_VERSION
	return result


def preflight_campaign(*, campaign: str) -> dict[str, Any]:
	require_read()
	if not campaign or not frappe.db.exists(CAMPAIGN_DOCTYPE, campaign):
		return {"ok": False, "verdict": "BLOCKED", "reasons": ["campaign_missing"]}

	doc = frappe.get_doc(CAMPAIGN_DOCTYPE, campaign)
	reasons = []
	if doc.status not in (STATUS_READY, "Paused"):
		reasons.append(f"status_not_ready:{doc.status}")
	if not is_scoped_campaign(campaign):
		reasons.append("not_scoped_campaign_missing_SCOPED_HRC_TEST_marker")
	allow = load_scoped_allowlist(campaign)
	if not allow:
		reasons.append("allowlist_missing_or_unreadable")
	items = frappe.get_all(
		ITEM_DOCTYPE,
		filters={"campaign": campaign},
		fields=["voucher_type", "voucher_no", "posting_date", "posting_time", "creation_ts"],
		order_by="ordinal asc",
		limit_page_length=0,
	)
	# Recompute checksum from live voucher meta (same inputs as create/discover).
	# Campaign Item.creation_ts may drop microsecond precision and would falsely
	# trip manifest_checksum_mismatch (HRC-00011 / v5.7.10 Production preflight).
	rows = []
	for i in items:
		meta = frappe.db.get_value(
			i.voucher_type,
			i.voucher_no,
			["company", "posting_date", "posting_time", "creation"],
			as_dict=True,
		)
		if not meta:
			reasons.append(f"voucher_missing_for_checksum:{i.voucher_type}/{i.voucher_no}")
			continue
		rows.append(
			{
				"doctype": i.voucher_type,
				"name": i.voucher_no,
				"posting_date": meta.posting_date,
				"posting_time": getattr(meta, "posting_time", None),
				"creation": meta.creation,
				"company": meta.company or doc.company,
			}
		)
	rows.sort(
		key=lambda r: (
			str(r.get("posting_date") or ""),
			str(r.get("posting_time") or ""),
			str(r.get("creation") or ""),
			str(r.get("doctype") or ""),
			str(r.get("name") or ""),
		)
	)
	computed = manifest_checksum(rows) if rows else ""
	stored = (doc.manifest_checksum or "").strip()
	if not stored:
		reasons.append("manifest_checksum_empty")
	elif computed.lower() != stored.lower():
		reasons.append("manifest_checksum_mismatch")

	# eligibility
	for i in items:
		ds = frappe.db.get_value(i.voucher_type, i.voucher_no, "docstatus")
		if cint(ds) != 1:
			reasons.append(f"voucher_not_submitted:{i.voucher_type}/{i.voucher_no}")

	active_riv = cint(
		frappe.db.count(
			"Repost Item Valuation",
			{"docstatus": 1, "status": ("in", ["Queued", "In Progress"])},
		)
	)
	if active_riv:
		reasons.append(f"active_queued_riv_count:{active_riv}")

	other_running = frappe.get_all(
		CAMPAIGN_DOCTYPE,
		filters={"status": "Running", "name": ("!=", campaign)},
		pluck="name",
		limit_page_length=10,
	)
	if other_running:
		reasons.append(f"other_running_campaigns:{other_running}")

	from erpnext_extensions.iran_accounting.historical_rebuild.sequential_rebuild import (
		_executor_lock_key,
	)

	lock = frappe.cache().get_value(_executor_lock_key(campaign))
	if lock:
		reasons.append(f"executor_lock_held:{lock}")

	# overlap: allowlisted identities currently in active RIV
	overlap = []
	if allow:
		for item, wh in allow:
			hit = frappe.db.exists(
				"Repost Item Valuation",
				{
					"item_code": item,
					"warehouse": wh,
					"docstatus": 1,
					"status": ("in", ["Queued", "In Progress"]),
				},
			)
			if hit:
				overlap.append({"item_code": item, "warehouse": wh, "riv": hit})
	if overlap:
		reasons.append("identity_riv_overlap")

	verdict = "PASS" if not reasons else "BLOCKED"
	return {
		"ok": verdict == "PASS",
		"verdict": verdict,
		"campaign": campaign,
		"status": doc.status,
		"company": doc.company,
		"manifest_checksum": stored,
		"computed_checksum": computed,
		"allowlist_count": len(allow or []),
		"manifest_total": len(items),
		"active_queued_riv_count": active_riv,
		"executor_lock": lock,
		"identity_riv_overlap": overlap,
		"reasons": reasons,
		"read_only": True,
	}


def start_sequential_campaign(
	*,
	campaign: str,
	expected_checksum: str,
	confirm: str | None = None,
	stop_on_failure: int | bool = 1,
	async_run: int | bool = 1,
) -> dict[str, Any]:
	require_repair()
	if str(confirm or "").strip() != "START_SEQUENTIAL":
		frappe.throw("confirm must equal START_SEQUENTIAL", frappe.ValidationError)
	if not expected_checksum:
		frappe.throw("expected_checksum is required", frappe.ValidationError)

	pre = preflight_campaign(campaign=campaign)
	if pre.get("verdict") != "PASS":
		return {
			"ok": False,
			"blocked": True,
			"reason": "preflight_failed",
			"preflight": pre,
		}
	stored = (frappe.db.get_value(CAMPAIGN_DOCTYPE, campaign, "manifest_checksum") or "").strip()
	if stored.lower() != str(expected_checksum).strip().lower():
		frappe.throw(
			f"expected_checksum mismatch before start: client={expected_checksum!r} stored={stored!r}",
			frappe.ValidationError,
		)

	from erpnext_extensions.iran_accounting.historical_rebuild.sequential_rebuild import (
		_executor_lock_key,
		get_seq_session,
		run_sequential,
		start_sequential_session,
	)

	# Idempotent: if same campaign session already Running under lock, return it.
	existing = get_seq_session()
	lock = frappe.cache().get_value(_executor_lock_key(campaign))
	if (
		existing
		and existing.get("campaign") == campaign
		and existing.get("status") == "Running"
		and lock
		and lock == existing.get("session_id")
	):
		return {
			"ok": True,
			"idempotent": True,
			"campaign": campaign,
			"session_id": existing.get("session_id"),
			"status": "Running",
			"message": "sequential session already running",
		}

	sess = start_sequential_session(campaign)
	session_id = sess["session_id"]
	append_api_audit(
		campaign,
		f"start_sequential session={session_id} stop_on_failure={bool(cint(stop_on_failure))}",
	)
	stop = bool(cint(stop_on_failure) if stop_on_failure is not None else 1)
	do_async = bool(cint(async_run) if async_run is not None else 1)

	if do_async:
		frappe.enqueue(
			"erpnext_extensions.iran_accounting.historical_rebuild.api_service._job_run_sequential",
			queue="long",
			timeout=60 * 60 * 12,
			job_name=f"hrc_seq_{campaign}_{session_id}",
			campaign=campaign,
			stop_on_failure=stop,
			enqueue_after_commit=True,
		)
		frappe.db.set_value(
			CAMPAIGN_DOCTYPE,
			campaign,
			{"status": STATUS_RUNNING, "last_activity": now_datetime()},
			update_modified=True,
		)
		return {
			"ok": True,
			"async": True,
			"campaign": campaign,
			"session_id": session_id,
			"status": "Running",
			"stop_on_failure": stop,
			"job_name": f"hrc_seq_{campaign}_{session_id}",
			"message": "Sequential rebuild enqueued on long queue",
		}

	# Synchronous path (tests / small scopes)
	result = run_sequential(campaign, stop_on_failure=stop)
	return {
		"ok": True,
		"async": False,
		"campaign": campaign,
		"session_id": session_id,
		"result": result,
	}


def _job_run_sequential(campaign: str, stop_on_failure: bool = True) -> dict[str, Any]:
	from erpnext_extensions.iran_accounting.historical_rebuild.sequential_rebuild import (
		run_sequential,
	)

	return run_sequential(campaign, stop_on_failure=stop_on_failure)


def get_campaign_status(*, campaign: str) -> dict[str, Any]:
	require_read()
	from erpnext_extensions.iran_accounting.historical_rebuild.campaign import status

	out = status(campaign)
	out["ok"] = True
	out["api_version"] = API_VERSION
	from erpnext_extensions.iran_accounting.historical_rebuild.sequential_rebuild import (
		_executor_lock_key,
		parse_campaign_session_id,
	)

	out["executor_lock"] = frappe.cache().get_value(_executor_lock_key(campaign))
	out["session_id_from_remarks"] = parse_campaign_session_id(campaign)
	return out


def request_campaign_stop(
	*,
	campaign: str,
	session_id: str | None = None,
	reason: str = "operator",
) -> dict[str, Any]:
	require_repair()
	if not frappe.db.exists(CAMPAIGN_DOCTYPE, campaign):
		frappe.throw(f"campaign missing: {campaign}", frappe.ValidationError)
	from erpnext_extensions.iran_accounting.historical_rebuild.sequential_rebuild import (
		get_seq_session,
		parse_campaign_session_id,
		request_sequential_stop,
	)

	active_sid = parse_campaign_session_id(campaign) or (get_seq_session() or {}).get("session_id")
	if session_id and active_sid and str(session_id) != str(active_sid):
		frappe.throw(
			f"session_id mismatch: provided={session_id} active={active_sid}",
			frappe.ValidationError,
		)
	res = request_sequential_stop(reason=reason or "operator")
	# Also set DocType stop_requested if field exists
	if frappe.get_meta(CAMPAIGN_DOCTYPE).has_field("stop_requested"):
		frappe.db.set_value(
			CAMPAIGN_DOCTYPE,
			campaign,
			{"stop_requested": 1, "last_activity": now_datetime()},
			update_modified=True,
		)
	append_api_audit(campaign, f"stop_requested reason={reason} session={active_sid}")
	return {
		"ok": True,
		"campaign": campaign,
		"session_id": active_sid,
		"stop": res,
		"note": "Current RIV is not cancelled; executor will halt after terminal state.",
	}


def validate_campaign_results(*, campaign: str) -> dict[str, Any]:
	require_read()
	if not frappe.db.exists(CAMPAIGN_DOCTYPE, campaign):
		frappe.throw(f"campaign missing: {campaign}", frappe.ValidationError)
	from erpnext_extensions.iran_accounting.historical_stock.integrity import voucher_integrity

	items = frappe.get_all(
		ITEM_DOCTYPE,
		filters={"campaign": campaign},
		fields=["voucher_type", "voucher_no"],
		order_by="ordinal asc",
		limit_page_length=0,
	)
	voucher_results = []
	for it in items:
		try:
			vi = voucher_integrity(it.voucher_no) if it.voucher_type == "Stock Entry" else None
		except Exception as e:
			vi = {"error": str(e)[:200]}
		gl = frappe.get_all(
			"GL Entry",
			filters={
				"voucher_type": it.voucher_type,
				"voucher_no": it.voucher_no,
				"is_cancelled": 0,
			},
			fields=["debit", "credit"],
			limit_page_length=5000,
		)
		d = sum(flt(g.debit) for g in gl)
		c = sum(flt(g.credit) for g in gl)
		voucher_results.append(
			{
				"voucher_type": it.voucher_type,
				"voucher_no": it.voucher_no,
				"gl_balanced": abs(d - c) < 0.5,
				"gl_debit": d,
				"gl_credit": c,
				"integrity": vi,
			}
		)

	allow = load_scoped_allowlist(campaign) or []
	neg_sv = []
	for item, wh in allow:
		n = frappe.db.sql(
			"""
			select count(*) from `tabStock Ledger Entry`
			where is_cancelled=0 and item_code=%s and warehouse=%s
			  and qty_after_transaction>0.0000001 and stock_value < -0.5
			""",
			(item, wh),
		)[0][0]
		if cint(n):
			neg_sv.append({"item_code": item, "warehouse": wh, "count": cint(n)})

	status = get_campaign_status(campaign=campaign)
	issues = []
	if any(not v["gl_balanced"] for v in voucher_results):
		issues.append("gl_imbalance")
	if neg_sv:
		issues.append("negative_stock_value_with_positive_qty")
	if status.get("status") == "Failed":
		issues.append("campaign_failed")

	return {
		"ok": not issues,
		"verdict": "PASS" if not issues else "FAILED",
		"campaign": campaign,
		"campaign_status": status.get("status"),
		"manifest_checksum": status.get("manifest_checksum")
		or frappe.db.get_value(CAMPAIGN_DOCTYPE, campaign, "manifest_checksum"),
		"voucher_results": voucher_results,
		"negative_stock_value_identities": neg_sv,
		"issues": issues,
		"note": (
			"Campaign Completed alone is not proof of SA residual elimination; "
			"inspect Stock Adjustment account economics separately."
		),
		"read_only": True,
	}

# Copyright (c) 2026, ERPNext Extensions contributors
"""Secure token-compatible API for approved SABE-integer Historical RIV scopes.

Authentication
--------------
Uses Frappe's supported mechanisms only (session cookie or
``Authorization: token <api_key>:<api_secret>``). No API secrets are stored
in this module. Callers must use a dedicated integration User with
System Manager (or a role listed in site config) and generate keys via Desk.

Authorization
-------------
Every mutating call must:
1. Pass permission checks (System Manager by default).
2. Match an allowlisted scope from ``site_config.iran_sabe_integer_riv_allowlist``
   (or the Development approved constant when ``allow_dev_approved_scopes`` is set).
3. Pass ``confirm_execution="EXECUTE"`` for mutations (dry_run is default).

Dry-run never mutates. Execute creates/submits/runs one Item+Warehouse RIV
through ``riv_execute_guard`` (same Iran bootstrap / GL handlers as UI).
"""

from __future__ import annotations

import json
from typing import Any

import frappe
from frappe import _
from frappe.utils import cint, now_datetime

from erpnext_extensions.iran_accounting.domain.riv_sabe_integer_phase3 import (
	APPROVED_SCOPES,
)
from erpnext_extensions.iran_accounting.domain.riv_valuation_guard import (
	ensure_iran_riv_gl_handlers_active,
	ensure_iran_riv_recalculate_wrapper_active,
)
from erpnext_extensions.iran_accounting.integration.bootstrap import apply as bootstrap_apply
from erpnext_extensions.iran_accounting.integration.riv_execute_guard import (
	execute_reposting_entry,
)


def _site_cfg() -> dict:
	return frappe.get_site_config() or {}


def _allowlist() -> list[dict[str, str]]:
	cfg = _site_cfg()
	raw = cfg.get("iran_sabe_integer_riv_allowlist") or []
	if isinstance(raw, str):
		raw = json.loads(raw)
	scopes = list(raw or [])
	if cfg.get("iran_sabe_integer_riv_allow_dev_approved_scopes"):
		# Development convenience — Production must set explicit allowlist only.
		for s in APPROVED_SCOPES:
			if s not in scopes:
				scopes.append(dict(s))
	return scopes


def _require_riv_api_permission() -> None:
	user = frappe.session.user
	if user in ("Guest", None):
		frappe.throw(_("Login required"), frappe.AuthenticationError)
	roles = set(frappe.get_roles(user))
	allowed = set(
		_site_cfg().get("iran_sabe_integer_riv_roles")
		or ["System Manager", "Administrator"]
	)
	if user == "Administrator" or roles.intersection(allowed):
		return
	frappe.throw(
		_("Not permitted to use SABE integer RIV API"),
		frappe.PermissionError,
	)


def _normalize_scope(item_code: str, warehouse: str, posting_date: str, company: str | None) -> dict:
	item_code = (item_code or "").strip()
	warehouse = (warehouse or "").strip()
	posting_date = (posting_date or "").strip()[:10]
	if not item_code or not warehouse or not posting_date:
		frappe.throw(_("item_code, warehouse, and posting_date are required"))
	wh_company = frappe.db.get_value("Warehouse", warehouse, "company")
	if not wh_company:
		frappe.throw(_("Warehouse {0} not found").format(warehouse))
	if company and company != wh_company:
		frappe.throw(_("company does not match warehouse company"))
	return {
		"item_code": item_code,
		"warehouse": warehouse,
		"posting_date": posting_date,
		"company": wh_company,
	}


def _assert_scope_allowed(scope: dict[str, str]) -> None:
	allowed = _allowlist()
	for a in allowed:
		if (
			a.get("item_code") == scope["item_code"]
			and a.get("warehouse") == scope["warehouse"]
			and str(a.get("posting_date") or "")[:10] == scope["posting_date"]
		):
			return
	frappe.throw(
		_(
			"Scope not allowlisted for SABE integer RIV API. "
			"Configure site_config.iran_sabe_integer_riv_allowlist."
		),
		frappe.PermissionError,
	)


def _audit(event: str, payload: dict[str, Any]) -> str:
	"""Persist an auditable trail without storing secrets."""
	name = frappe.generate_hash(length=10)
	doc = {
		"doctype": "Comment",
		"comment_type": "Info",
		"reference_doctype": "Repost Item Valuation",
		"reference_name": payload.get("riv") or "SABE-INTEGER-RIV-API",
		"content": json.dumps(
			{
				"event": event,
				"audit_id": name,
				"user": frappe.session.user,
				"at": str(now_datetime()),
				"site": frappe.local.site,
				**{k: v for k, v in payload.items() if k not in ("password", "api_secret", "token")},
			},
			ensure_ascii=False,
			default=str,
		)[:10000],
	}
	try:
		frappe.get_doc(doc).insert(ignore_permissions=True)
		frappe.db.commit()
	except Exception:
		frappe.log_error(title=f"SABE integer RIV audit {name}", message=doc["content"])
	return name


def _dry_run_preview(scope: dict[str, str]) -> dict[str, Any]:
	from erpnext_extensions.iran_accounting.domain.riv_sabe_integer_campaign import (
		dry_run_riv_sabe_integer_campaign,
	)

	# Narrow preview to the scope item (warehouse filter applied in report)
	full = dry_run_riv_sabe_integer_campaign(item_codes=[scope["item_code"]])
	rows = [
		r
		for r in (full.get("rows") or [])
		if r.get("item_code") == scope["item_code"] and r.get("warehouse") == scope["warehouse"]
	]
	return {
		"mode": "dry_run",
		"scope": scope,
		"fractional_sabe_in_scope": len(rows),
		"a2_net_in_scope": sum(float(r.get("localization_difference") or 0) for r in rows),
		"campaign_fractional_sabe_count": full.get("fractional_sabe_count"),
		"campaign_outstanding_a2_net_after_dedupe": full.get("outstanding_a2_net_after_dedupe"),
		"audit_path": full.get("audit_path"),
		"mutation": False,
	}


@frappe.whitelist()
def dry_run_scope(item_code: str, warehouse: str, posting_date: str, company: str | None = None):
	"""Read-only SABE-integer RIV preview for one allowlisted Item+Warehouse scope."""
	_require_riv_api_permission()
	bootstrap_apply()
	ensure_iran_riv_recalculate_wrapper_active(bootstrap=False)
	ensure_iran_riv_gl_handlers_active(bootstrap=False)
	scope = _normalize_scope(item_code, warehouse, posting_date, company)
	_assert_scope_allowed(scope)
	result = _dry_run_preview(scope)
	audit_id = _audit("dry_run_scope", {"scope": scope, "result_summary": {
		"fractional_sabe_in_scope": result["fractional_sabe_in_scope"],
		"a2_net_in_scope": result["a2_net_in_scope"],
	}})
	result["audit_id"] = audit_id
	return result


@frappe.whitelist()
def execute_scope(
	item_code: str,
	warehouse: str,
	posting_date: str,
	company: str | None = None,
	confirm_execution: str | None = None,
	dry_run: int | str | None = 1,
):
	"""Execute one allowlisted RIV scope. Default dry_run=1 (no mutation).

	Mutating calls require ``confirm_execution='EXECUTE'`` and dry_run=0.
	"""
	_require_riv_api_permission()
	bootstrap_apply()
	ensure_iran_riv_recalculate_wrapper_active(bootstrap=False)
	ensure_iran_riv_gl_handlers_active(bootstrap=False)
	scope = _normalize_scope(item_code, warehouse, posting_date, company)
	_assert_scope_allowed(scope)

	is_dry = True if dry_run in (None, "") else bool(cint(dry_run))
	if is_dry:
		result = _dry_run_preview(scope)
		result["audit_id"] = _audit("execute_scope_dry_run", {"scope": scope})
		return result

	if (confirm_execution or "").strip() != "EXECUTE":
		frappe.throw(
			_("Mutating execute_scope requires confirm_execution='EXECUTE'"),
			frappe.ValidationError,
		)
	# Fail closed unless site explicitly enables mutations
	if not _site_cfg().get("iran_sabe_integer_riv_allow_execute"):
		frappe.throw(
			_(
				"Mutating SABE integer RIV API is disabled. "
				"Set site_config.iran_sabe_integer_riv_allow_execute=1 after backup + approval."
			),
			frappe.ValidationError,
		)

	riv = frappe.new_doc("Repost Item Valuation")
	riv.based_on = "Item and Warehouse"
	riv.item_code = scope["item_code"]
	riv.warehouse = scope["warehouse"]
	riv.posting_date = scope["posting_date"]
	riv.posting_time = "00:00:01"
	riv.allow_negative_stock = 1
	riv.company = scope["company"]
	riv.flags.ignore_permissions = True
	riv.insert()
	riv.submit()
	frappe.db.commit()
	try:
		execute_reposting_entry(riv.name, continue_reposting=False)
		frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		frappe.db.commit()
		status = frappe.db.get_value(
			"Repost Item Valuation", riv.name, ["status", "error_log"], as_dict=True
		)
		payload = {
			"ok": False,
			"mode": "execute",
			"riv": riv.name,
			"scope": scope,
			"status": status.status if status else "Failed",
			"error_log": ((status.error_log or "")[:2000] if status else frappe.get_traceback()),
		}
		payload["audit_id"] = _audit("execute_scope_failed", payload)
		return payload

	status = frappe.db.get_value(
		"Repost Item Valuation", riv.name, ["status", "error_log"], as_dict=True
	)
	payload = {
		"ok": bool(status and status.status == "Completed"),
		"mode": "execute",
		"riv": riv.name,
		"scope": scope,
		"status": status.status if status else None,
		"error_log": ((status.error_log or "")[:2000] if status else None),
		"mutation": True,
	}
	payload["audit_id"] = _audit("execute_scope_completed", payload)
	return payload


@frappe.whitelist()
def list_allowlisted_scopes():
	"""Return allowlisted scopes visible to the caller (no secrets)."""
	_require_riv_api_permission()
	return {
		"scopes": _allowlist(),
		"execute_enabled": bool(_site_cfg().get("iran_sabe_integer_riv_allow_execute")),
		"dev_approved_scopes_enabled": bool(
			_site_cfg().get("iran_sabe_integer_riv_allow_dev_approved_scopes")
		),
	}

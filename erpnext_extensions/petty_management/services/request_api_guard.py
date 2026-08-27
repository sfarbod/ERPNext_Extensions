"""Secure PM Request access for whitelisted Desk APIs and internal system loaders."""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint

# Only this module may call frappe.get_doc("PM Request", …) directly (see static scan test).


def _version_cache_key(pm_request: str) -> str:
	return f"pm_request_pe_list_version:{pm_request}"


def get_pm_request_response_version(pm_request: str) -> str:
	version = frappe.cache.get_value(_version_cache_key(pm_request))
	if version is None:
		version = 0
	return str(cint(version))


def bump_pm_request_response_version(pm_request: str) -> str:
	key = _version_cache_key(pm_request)
	next_v = cint(frappe.cache.get_value(key) or 0) + 1
	frappe.cache.set_value(key, next_v)
	return str(next_v)


def notify_pm_request_funding_updated(pm_request: str, event: str) -> str:
	"""Bump server list version and push Desk realtime refresh (event-driven UI sync)."""
	version = bump_pm_request_response_version(pm_request)
	frappe.publish_realtime(
		"pm_request_funding_updated",
		{
			"pm_request": pm_request,
			"response_version_id": version,
			"event": event,
		},
		doctype="PM Request",
		docname=pm_request,
	)
	return version


def assert_pm_request_company_user_permission(doc: Document) -> None:
	if frappe.session.user == "Administrator":
		return
	user_perms = frappe.permissions.get_user_permissions(frappe.session.user)
	company_perms = user_perms.get("Company") or []
	if not company_perms:
		return
	allowed: set[str] = set()
	for row in company_perms:
		docname = getattr(row, "doc", None) or (row.get("doc") if isinstance(row, dict) else None)
		if docname:
			allowed.add(docname)
	if doc.company not in allowed:
		frappe.throw(
			_("Not permitted to access PM Request for company {0}").format(doc.company),
			frappe.PermissionError,
		)


def _load_pm_request_doc(name: str, *, for_update: bool = False) -> Document:
	if for_update:
		return frappe.get_doc("PM Request", name, for_update=True)
	return frappe.get_doc("PM Request", name)


def _resolve_pm_request_name(pm_request: str | Document | None) -> str:
	if isinstance(pm_request, Document):
		return pm_request.name
	name = (pm_request or "").strip()
	if not name:
		frappe.throw(_("PM Request is required"), frappe.ValidationError)
	if not frappe.db.exists("PM Request", name):
		frappe.throw(_("PM Request {0} not found").format(name), frappe.DoesNotExistError)
	return name


def get_pm_request_doc_internal(pm_request: str | Document | None) -> Document:
	"""Hooks, reconciliation, migrations, server-side sync (no permission checks)."""
	name = _resolve_pm_request_name(pm_request)
	return _load_pm_request_doc(name)


def get_pm_request_doc_internal_lock(pm_request: str | Document | None) -> Document:
	"""Row lock for internal funding sync jobs after name resolution."""
	name = _resolve_pm_request_name(pm_request)
	return _load_pm_request_doc(name, for_update=True)


def get_pm_request_doc_for_read(pm_request: str | None) -> Document:
	"""User/API: read permission + company user-permission rules.

	v4.7.2: during PM workflow apply / patch, skip Desk permission so finance
	reviewers without PM Request read can still complete Clearance Approve submit
	(allocation integrity checks remain).
	"""
	name = _resolve_pm_request_name(pm_request)
	doc = _load_pm_request_doc(name)
	if getattr(frappe.flags, "in_pm_workflow_apply", False) or getattr(frappe.flags, "in_patch", False):
		return doc
	assert_pm_request_company_user_permission(doc)
	doc.check_permission("read")
	return doc


def get_pm_request_doc_for_write(pm_request: str | None) -> Document:
	doc = get_pm_request_doc_for_read(pm_request)
	doc.check_permission("write")
	return doc


def get_pm_request_doc_for_write_lock(pm_request: str | None) -> Document:
	"""User/API row lock after read + write permission checks."""
	doc = get_pm_request_doc_for_write(pm_request)
	return get_pm_request_doc_internal_lock(doc.name)


def pm_request_names_for_report(filters: dict | None = None) -> list[str]:
	"""Report rows: respect company filter and per-document read permission."""
	filters = filters or {}
	flt: dict = {}
	if filters.get("company"):
		flt["company"] = filters["company"]

	if frappe.session.user != "Administrator":
		user_perms = frappe.permissions.get_user_permissions(frappe.session.user)
		company_perms = user_perms.get("Company") or []
		if company_perms:
			allowed = set()
			for row in company_perms:
				docname = getattr(row, "doc", None) or (row.get("doc") if isinstance(row, dict) else None)
				if docname:
					allowed.add(docname)
			if flt.get("company") and flt["company"] not in allowed:
				return []
			if not flt.get("company"):
				names: list[str] = []
				for company in allowed:
					for pr in frappe.get_all("PM Request", filters={"company": company}, pluck="name"):
						try:
							get_pm_request_doc_for_read(pr)
							names.append(pr)
						except (frappe.PermissionError, frappe.DoesNotExistError):
							continue
				return names

	out: list[str] = []
	for pr in frappe.get_all("PM Request", filters=flt or None, pluck="name"):
		try:
			get_pm_request_doc_for_read(pr)
			out.append(pr)
		except (frappe.PermissionError, frappe.DoesNotExistError):
			continue
	return out


def build_pm_request_payment_entries_payload(pm_request: str | None) -> dict:
	from erpnext_extensions.petty_management.services.funding_queries import (
		list_payment_entries_for_pm_request,
	)

	doc = get_pm_request_doc_for_read(pm_request)
	return {
		"pm_request": doc.name,
		"response_version_id": get_pm_request_response_version(doc.name),
		"payment_entries": list_payment_entries_for_pm_request(doc.name),
	}


def wrap_action_flags_with_version(flags: dict, pm_request: str) -> dict:
	out = dict(flags)
	out["response_version_id"] = get_pm_request_response_version(pm_request)
	return out

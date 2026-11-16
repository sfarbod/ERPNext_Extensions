# Copyright (c) 2026, ERPNext Extensions contributors
"""Authorization gate for staggered HRC execution (v5.7.51).

Development remains available via existing site markers.
Production is disabled by default and requires:
  - hrc_stagger_allow_production = 1
  - hrc_stagger_production_site = exact Frappe site name (no wildcards)
  - operator confirm token START_STAGGERED_PRODUCTION

developer_mode is never accepted as a Production authorization signal.
Read-only status APIs do not call this gate.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import cint

CONFIRM_DEV = "START_STAGGERED"
CONFIRM_PRODUCTION = "START_STAGGERED_PRODUCTION"


def current_site() -> str:
	return str(frappe.local.site or "").strip()


def is_production_like_site(site: str | None = None) -> bool:
	"""True for live Production hostname; Dev/restored clones with 'development' excluded."""
	s = (site if site is not None else current_site()).strip()
	if not s:
		return False
	return "espadpharmed.com" in s and "development" not in s


def is_development_site(site: str | None = None) -> bool:
	"""Dev/test site markers — never used to authorize Production."""
	if is_production_like_site(site):
		return False
	return bool(
		frappe.conf.get("developer_mode")
		or frappe.conf.get("e2e_marker")
		or frappe.conf.get("restored_from_backup")
	)


def _conf_int_strict(key: str) -> int | None:
	"""Return int config value or None if absent; reject malformed / boolean values."""
	raw = frappe.conf.get(key)
	if raw is None:
		return None
	if isinstance(raw, bool):
		return None
	if isinstance(raw, str) and not str(raw).strip():
		return None
	try:
		return int(cint(raw))
	except Exception:
		return None


def production_authorization_state(site: str | None = None) -> dict[str, Any]:
	"""Read-only probe of Production authorization settings (never enables execution)."""
	s = (site if site is not None else current_site()).strip()
	allow = _conf_int_strict("hrc_stagger_allow_production")
	allowed_site = str(frappe.conf.get("hrc_stagger_production_site") or "").strip()
	wildcard = ("*" in allowed_site) or ("?" in allowed_site) or (allowed_site.endswith("%"))
	exact_ok = bool(allowed_site) and not wildcard and allowed_site == s
	return {
		"site": s,
		"is_production_like": is_production_like_site(s),
		"hrc_stagger_allow_production": allow,
		"hrc_stagger_production_site": allowed_site or None,
		"production_site_exact_match": exact_ok,
		"wildcard_rejected": wildcard,
		"authorized": bool(
			is_production_like_site(s) and allow == 1 and exact_ok
		),
	}


def require_operator_confirm(confirm: str | None, *, production: bool) -> str:
	"""Validate start/enqueue confirm token for the environment."""
	token = str(confirm or "").strip()
	needed = CONFIRM_PRODUCTION if production else CONFIRM_DEV
	if token != needed:
		frappe.throw(
			f"confirm must equal {needed}",
			frappe.ValidationError,
		)
	return token


def assert_staggered_site_allowed() -> dict[str, Any]:
	"""Fail-closed site gate for any staggered execution path (no confirm)."""
	site = current_site()
	if not site:
		frappe.throw("Refused staggered HRC: empty Frappe site name", frappe.ValidationError)

	if is_production_like_site(site):
		state = production_authorization_state(site)
		if state["hrc_stagger_allow_production"] is None:
			frappe.throw(
				"Refused staggered HRC on Production: hrc_stagger_allow_production is absent",
				frappe.ValidationError,
			)
		if state["hrc_stagger_allow_production"] != 1:
			frappe.throw(
				"Refused staggered HRC on Production: hrc_stagger_allow_production is not enabled",
				frappe.ValidationError,
			)
		if not state.get("hrc_stagger_production_site"):
			frappe.throw(
				"Refused staggered HRC on Production: hrc_stagger_production_site is absent",
				frappe.ValidationError,
			)
		if state.get("wildcard_rejected"):
			frappe.throw(
				"Refused staggered HRC on Production: wildcard site names are not allowed",
				frappe.ValidationError,
			)
		if not state.get("production_site_exact_match"):
			frappe.throw(
				"Refused staggered HRC on Production: hrc_stagger_production_site "
				f"must exactly equal current site {site!r}",
				frappe.ValidationError,
			)
		# Explicitly ignore developer_mode on Production — never treat it as auth.
		return {
			"ok": True,
			"environment": "production",
			"site": site,
			"allow_production": 1,
		}

	if not is_development_site(site):
		frappe.throw(
			"Refused staggered HRC: site is not marked development/test "
			"(developer_mode / e2e_marker / restored_from_backup)",
			frappe.ValidationError,
		)
	return {"ok": True, "environment": "development", "site": site}


def assert_staggered_execution_allowed(
	*,
	confirm: str | None = None,
	for_start: bool = False,
) -> dict[str, Any]:
	"""Site gate + optional operator confirm for starts / enqueue / recovery."""
	gate = assert_staggered_site_allowed()
	if for_start:
		token = require_operator_confirm(
			confirm, production=(gate["environment"] == "production")
		)
		gate["confirm"] = token
	return gate


# Back-compat name used by older call sites / benchmarks that mean "dev only".
def assert_dev_only_clone() -> None:
	"""Clone remains Development/restored-only — never Production."""
	site = current_site()
	if is_production_like_site(site):
		frappe.throw(f"Refused campaign clone on production-like site: {site}")
	if not is_development_site(site):
		frappe.throw("Refused: site not marked as development/test")

# Copyright (c) 2026, ERPNext Extensions contributors
"""Whitelisted REST API for campaign-scoped Historical Rebuild (v5.7.10).

Thin wrappers over existing scoped_campaign / sequential_rebuild / campaign.status.
Does **not** expose prepare_clean_start, prepare_sequential_rebuild, or
site-wide execute_repost_item_valuation.
"""

from __future__ import annotations

import frappe

from erpnext_extensions.iran_accounting.historical_rebuild import api_service as svc

API_VERSION = svc.API_VERSION


@frappe.whitelist(methods=["GET", "POST"])
def get_rebuild_api_status(company=None):
	"""Read-only environment + capability probe for Historical Rebuild REST."""
	return svc.get_rebuild_api_status(company=company or None)


@frappe.whitelist(methods=["GET", "POST"])
def inspect_vouchers(company=None, vouchers=None):
	"""Read-only inspection of an explicit voucher allowlist."""
	return svc.inspect_vouchers(company=company, vouchers=vouchers)


@frappe.whitelist(methods=["GET", "POST"])
def discover_dependencies(
	company=None,
	vouchers=None,
	mode="exact",
	posting_boundary_from=None,
):
	"""Read-only dependency / checksum proposal (no campaign create)."""
	return svc.discover_dependencies(
		company=company,
		vouchers=vouchers,
		mode=mode or "exact",
		posting_boundary_from=posting_boundary_from or None,
	)


@frappe.whitelist(methods=["POST"])
def create_exact_campaign(
	company=None,
	vouchers=None,
	identities_allowlist=None,
	expected_checksum=None,
	scope_label="rest-exact-campaign",
	remarks=None,
	confirm=None,
):
	"""Create a new exact-voucher scoped campaign. Never resumes old campaigns.

	confirm must equal ``CREATE_EXACT_CAMPAIGN``.
	"""
	return svc.create_exact_campaign(
		company=company,
		vouchers=vouchers,
		identities_allowlist=identities_allowlist,
		expected_checksum=expected_checksum,
		scope_label=scope_label or "rest-exact-campaign",
		remarks=remarks,
		confirm=confirm,
	)


@frappe.whitelist(methods=["GET", "POST"])
def preflight_campaign(campaign=None):
	"""Read-only preflight → PASS / BLOCKED."""
	if not campaign:
		frappe.throw("campaign is required")
	return svc.preflight_campaign(campaign=campaign)


@frappe.whitelist(methods=["POST"])
def start_sequential_campaign(
	campaign=None,
	expected_checksum=None,
	confirm=None,
	stop_on_failure=1,
	async_run=1,
):
	"""Start sequential rebuild for an approved campaign (fail-fast default).

	confirm must equal ``START_SEQUENTIAL``. Defaults to async long-queue job.
	"""
	if not campaign:
		frappe.throw("campaign is required")
	return svc.start_sequential_campaign(
		campaign=campaign,
		expected_checksum=expected_checksum,
		confirm=confirm,
		stop_on_failure=stop_on_failure,
		async_run=async_run,
	)


@frappe.whitelist(methods=["GET", "POST"])
def get_campaign_status(campaign=None):
	"""Read-only campaign + sequential session status."""
	if not campaign:
		frappe.throw("campaign is required")
	return svc.get_campaign_status(campaign=campaign)


@frappe.whitelist(methods=["POST"])
def request_campaign_stop(campaign=None, session_id=None, reason="operator"):
	"""Request safe sequential stop (does not cancel the in-flight RIV)."""
	if not campaign:
		frappe.throw("campaign is required")
	return svc.request_campaign_stop(
		campaign=campaign, session_id=session_id, reason=reason or "operator"
	)


@frappe.whitelist(methods=["GET", "POST"])
def validate_campaign_results(campaign=None):
	"""Read-only post-rebuild accounting / integrity validation."""
	if not campaign:
		frappe.throw("campaign is required")
	return svc.validate_campaign_results(campaign=campaign)

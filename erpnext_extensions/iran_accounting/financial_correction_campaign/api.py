# Copyright (c) 2026, ERPNext Extensions contributors
"""Whitelisted dry-run APIs for financial correction campaigns (5.7.18)."""

from __future__ import annotations

import frappe

from erpnext_extensions.iran_accounting.financial_correction_campaign import (
	API_VERSION,
	CONFIRM_EXECUTE_ASSET_PR,
	CONFIRM_EXECUTE_STE_FRAC,
	DENYLIST_JOURNAL_ENTRIES,
)
from erpnext_extensions.iran_accounting.financial_correction_campaign.cohort import (
	assert_journal_denylist_intact,
	dry_run_asset_pr_cohort,
	dry_run_ste_fractional_cohort,
	require_privileged_user,
)


@frappe.whitelist(methods=["GET", "POST"])
def get_financial_correction_campaign_status(company=None):
	require_privileged_user()
	return {
		"api_version": API_VERSION,
		"company": company,
		"denylist_journal_entries": sorted(DENYLIST_JOURNAL_ENTRIES),
		"journal_gate": assert_journal_denylist_intact(),
		"confirm_execute_asset_pr": CONFIRM_EXECUTE_ASSET_PR,
		"confirm_execute_ste_frac": CONFIRM_EXECUTE_STE_FRAC,
		"capabilities": ["dry_run_asset_pr_cohort", "dry_run_ste_fractional_cohort"],
		"restrictions": [
			"no_company_wide_rebuild",
			"no_mutation_without_explicit_future_authorization",
			"never_touch_ACC-JV-1405-169046",
			"dry_run_is_not_completed_correction",
		],
	}


@frappe.whitelist(methods=["GET", "POST"])
def dry_run_asset_purchase_receipt_cohort(company=None):
	"""Dry-run Cohort A (8 Asset PRs). No ledger mutation."""
	return dry_run_asset_pr_cohort(company=company or None)


@frappe.whitelist(methods=["GET", "POST"])
def dry_run_stock_entry_fractional_cohort(company=None):
	"""Dry-run Cohort B (fractional STE identities). No ledger mutation."""
	return dry_run_ste_fractional_cohort(company=company or None)

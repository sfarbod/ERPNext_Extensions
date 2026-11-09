# Copyright (c) 2026, ERPNext Extensions contributors
"""Read-only Asset / depreciation impact analysis for Cohort A Asset PRs.

Never mutates Asset, Depreciation Schedule, Depreciation Entry, or GL.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import cint, flt


def expected_pi_aligned_acquisition_amount(pr_item) -> float:
	"""Invoice-aligned acquisition under FA GL fix / UVR with amt_diff.

	base_net + item_tax + LCV + amount_difference_with_purchase_invoice
	"""
	return (
		flt(pr_item.get("base_net_amount"))
		+ flt(pr_item.get("item_tax_amount"))
		+ flt(pr_item.get("landed_cost_voucher_amount"))
		+ flt(pr_item.get("amount_difference_with_purchase_invoice"))
	)


def valuation_amount_from_rate(pr_item) -> float:
	qty = flt(pr_item.get("stock_qty") or pr_item.get("qty") or 0)
	return flt(pr_item.get("valuation_rate")) * qty


def decide_asset_rebuild(
	*,
	asset: dict[str, Any],
	expected_acquisition: float,
	posted_depr_count: int,
	posted_depr_amount: float,
	tolerance: float = 0.5,
) -> dict[str, Any]:
	"""Case A–E decision tree. No mutation."""
	docstatus = cint(asset.get("docstatus"))
	current_cost = flt(
		asset.get("gross_purchase_amount")
		or asset.get("purchase_amount")
		or asset.get("net_purchase_amount")
		or 0
	)
	cost_changed = abs(current_cost - expected_acquisition) > tolerance if expected_acquisition else False

	# Case B — Draft
	if docstatus == 0:
		return {
			"case": "B_draft",
			"rebuild_required": False,
			"update_draft_purchase_amount_recommended": cost_changed and expected_acquisition > 0,
			"reason": (
				"Draft Asset — update purchase fields before submit if cost differs; "
				"do not rebuild a submitted schedule"
			),
			"safe_standard_rebuild": False,
			"historical_depr_preserve": True,
			"cost_changed": cost_changed,
			"current_acquisition_cost": current_cost,
			"corrected_acquisition_cost": expected_acquisition,
		}

	# Case A handled by caller when no asset

	# Case D — submitted, cost unchanged
	if not cost_changed:
		return {
			"case": "D_submitted_cost_unchanged",
			"rebuild_required": False,
			"update_draft_purchase_amount_recommended": False,
			"reason": "Submitted Asset acquisition cost already matches PI-aligned expectation",
			"safe_standard_rebuild": False,
			"historical_depr_preserve": True,
			"cost_changed": False,
			"current_acquisition_cost": current_cost,
			"corrected_acquisition_cost": expected_acquisition,
		}

	# Case C / E — submitted, cost changed
	if posted_depr_count > 0 or abs(posted_depr_amount) > tolerance:
		return {
			"case": "E_submitted_cost_changed_with_posted_depr",
			"rebuild_required": True,
			"update_draft_purchase_amount_recommended": False,
			"reason": (
				"Submitted Asset cost differs from PI-aligned amount and posted depreciation exists. "
				"Do not auto-cancel/reverse historical Depreciation Entries or their GL. "
				"Requires separate Finance-approved future-only adjustment design; "
				"core reset_depreciation_schedule / reschedule_depreciation is NOT safe to run blindly."
			),
			"safe_standard_rebuild": False,
			"historical_depr_preserve": True,
			"cost_changed": True,
			"current_acquisition_cost": current_cost,
			"corrected_acquisition_cost": expected_acquisition,
			"posted_depreciation_entries": posted_depr_count,
			"posted_depreciation_amount": posted_depr_amount,
		}

	return {
		"case": "C_submitted_cost_changed_no_posted_depr",
		"rebuild_required": True,
		"update_draft_purchase_amount_recommended": False,
		"reason": (
			"Submitted Asset cost differs and no posted depreciation journal found. "
			"A controlled schedule rebuild via supported Asset APIs may be appropriate "
			"after Finance approval — still not auto-executed by Cohort A."
		),
		"safe_standard_rebuild": True,
		"historical_depr_preserve": True,
		"cost_changed": True,
		"current_acquisition_cost": current_cost,
		"corrected_acquisition_cost": expected_acquisition,
		"posted_depreciation_entries": 0,
		"posted_depreciation_amount": 0.0,
	}


def inspect_assets_for_purchase_receipt(pr_name: str) -> dict[str, Any]:
	"""Read-only discovery + rebuild decision for all Assets linked to a PR."""
	pr = frappe.get_doc("Purchase Receipt", pr_name)
	fa_items = [it for it in pr.items if cint(it.is_fixed_asset)]
	# Aggregate expected acquisition across FA rows (PI-aligned).
	expected_total = sum(expected_pi_aligned_acquisition_amount(it.as_dict()) for it in fa_items)
	vr_total = sum(valuation_amount_from_rate(it.as_dict()) for it in fa_items)
	pr_amount_total = sum(flt(it.base_net_amount) for it in fa_items)
	amt_diff_total = sum(flt(it.amount_difference_with_purchase_invoice) for it in fa_items)

	assets = frappe.db.sql(
		"""
		SELECT name, docstatus, status, asset_category, item_code,
		       purchase_receipt, purchase_invoice, purchase_receipt_item,
		       gross_purchase_amount, purchase_amount, net_purchase_amount,
		       available_for_use_date, value_after_depreciation,
		       calculate_depreciation, opening_accumulated_depreciation,
		       is_existing_asset, asset_quantity
		FROM `tabAsset`
		WHERE purchase_receipt=%s AND docstatus < 2
		ORDER BY name
		""",
		pr_name,
		as_dict=True,
	)

	per_asset = []
	for a in assets:
		# Finance books / method
		fb = frappe.db.sql(
			"""
			SELECT finance_book, depreciation_method, frequency_of_depreciation,
			       total_number_of_depreciations, depreciation_start_date,
			       expected_value_after_useful_life, value_after_depreciation,
			       daily_prorata_based
			FROM `tabAsset Finance Book`
			WHERE parent=%s
			ORDER BY idx
			""",
			a.name,
			as_dict=True,
		)
		schedules = frappe.db.sql(
			"""
			SELECT name, docstatus, status, finance_book, opening_accumulated_depreciation
			FROM `tabAsset Depreciation Schedule`
			WHERE asset=%s AND docstatus < 2
			ORDER BY name
			""",
			a.name,
			as_dict=True,
		)
		# Posted depreciation journals linked from schedule rows
		posted = frappe.db.sql(
			"""
			SELECT ds.name AS schedule, dsd.schedule_date, dsd.depreciation_amount,
			       dsd.journal_entry, dsd.accumulated_depreciation_amount
			FROM `tabDepreciation Schedule` dsd
			JOIN `tabAsset Depreciation Schedule` ds ON ds.name = dsd.parent
			WHERE ds.asset=%s AND ds.docstatus < 2
			  AND IFNULL(dsd.journal_entry, '') != ''
			ORDER BY dsd.schedule_date, dsd.idx
			""",
			a.name,
			as_dict=True,
		)
		posted_amt = sum(flt(p.depreciation_amount) for p in posted)
		# Per-asset expected from linked PRI (or item_code match). Never assign the
		# whole PR total to every asset — that false-positives rebuild Case E.
		expected = None
		row = None
		if a.purchase_receipt_item:
			row = next((it for it in fa_items if it.name == a.purchase_receipt_item), None)
		if row is None and a.item_code:
			cands = [it for it in fa_items if it.item_code == a.item_code]
			row = cands[0] if len(cands) == 1 else None
		if row is not None:
			qty = cint(a.asset_quantity or 1)
			vr = flt(row.valuation_rate)
			if vr and qty:
				expected = vr * qty * flt(row.conversion_factor or 1)
			else:
				# Single asset for the row → full PI-aligned row amount
				same_item_assets = sum(
					1 for x in assets if x.item_code == row.item_code
				)
				if same_item_assets <= 1:
					expected = expected_pi_aligned_acquisition_amount(row.as_dict())
				else:
					expected = expected_pi_aligned_acquisition_amount(row.as_dict()) / same_item_assets
		if expected is None:
			# Cannot allocate safely — compare against current cost only (no false rebuild).
			expected = flt(
				a.gross_purchase_amount or a.purchase_amount or a.net_purchase_amount or 0
			)

		decision = decide_asset_rebuild(
			asset=a,
			expected_acquisition=expected,
			posted_depr_count=len(posted),
			posted_depr_amount=posted_amt,
		)
		accum = flt(a.opening_accumulated_depreciation) + posted_amt
		nbv = flt(a.value_after_depreciation)
		if not nbv and flt(a.gross_purchase_amount):
			nbv = flt(a.gross_purchase_amount) - accum

		per_asset.append(
			{
				"asset": a.name,
				"docstatus": a.docstatus,
				"status": a.status,
				"item_code": a.item_code,
				"asset_category": a.asset_category,
				"purchase_receipt": a.purchase_receipt,
				"purchase_invoice": a.purchase_invoice,
				"purchase_receipt_item": a.purchase_receipt_item,
				"gross_purchase_amount": flt(a.gross_purchase_amount),
				"purchase_amount": flt(a.purchase_amount),
				"net_purchase_amount": flt(a.net_purchase_amount),
				"available_for_use_date": str(a.available_for_use_date) if a.available_for_use_date else None,
				"asset_quantity": cint(a.asset_quantity),
				"calculate_depreciation": cint(a.calculate_depreciation),
				"finance_books": fb,
				"schedules": schedules,
				"posted_depreciation_rows": posted,
				"posted_depreciation_count": len(posted),
				"posted_depreciation_amount": posted_amt,
				"accumulated_depreciation_estimate": accum,
				"net_book_value": nbv,
				"decision": decision,
			}
		)

	return {
		"purchase_receipt": pr_name,
		"fa_item_count": len(fa_items),
		"pr_base_net_total": pr_amount_total,
		"amount_difference_total": amt_diff_total,
		"valuation_rate_amount_total": vr_total,
		"expected_pi_aligned_acquisition_total": expected_total,
		"linked_asset_count": len(assets),
		"assets": per_asset,
		"case_a_no_linked_asset": len(assets) == 0,
		"any_rebuild_required": any(
			(a.get("decision") or {}).get("rebuild_required") for a in per_asset
		),
		"mutated": False,
	}

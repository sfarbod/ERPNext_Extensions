# Copyright (c) 2026, ERPNext Extensions contributors
"""Iran accounting deterministic layer after ERPNext engine repost (RIV / RAL).

REPOST DETERMINISTIC ARCHITECTURE
---------------------------------

ENGINE LAYER (ERPNext): RIV, RAL — SLE regeneration and valuation recomputation only.
**Strict rule:** engine is NOT accounting truth; engine output is **untrusted** until this module runs.

DETERMINISTIC LAYER (this module): row truth, SR header, SLE mirror, GL = Σ amount_difference, avg rate.

MANDATORY PIPELINE (after every repost)
---------------------------------------
1. ``reconcile_irr_after_repost(doc)`` — or ``(voucher_type, voucher_no, …)``
2. ``validate_deterministic_state_after_repost(doc)`` — logs ``DETERMINISTIC_LAYER_ASSERTED``
3. ``run_post_repost_deterministic_pipeline(doc)`` — strict order: 1 then 2

OPERATIONAL CONTRACT
--------------------
Supported only: ``run_repost_for_voucher`` / ``run_repost_for_voucher_impl`` (engine + pipeline, ``raise_on_fail=True``).
Hooks (RIV Completed, RAL module ``repost`` / legacy ``start_repost``):
``run_post_repost_deterministic_pipeline(doc, raise_on_fail=False)``.
Forbidden: raw RIV/RAL; treating engine as final truth; skipping reconcile + validate.

GUARANTEE
---------
ENGINE → reconcile_irr_after_repost → validate_deterministic_state_after_repost → DETERMINISTIC_LAYER_ASSERTED
"""

from __future__ import annotations

import logging

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.domain.currency import get_company_currency, is_irr_company
from erpnext_extensions.iran_accounting.domain.qty_rate_amount import (
	align_stock_entry_item_amounts,
	override_difference_amount,
	sum_stock_reconciliation_amount_difference,
)
from erpnext_extensions.iran_accounting.domain.stock_entry_sync import (
	assert_stock_entry_row_sle_mirror,
	sync_irr_sle_from_stock_entry_row,
)
from erpnext_extensions.iran_accounting.domain.stock_ledger_deterministic import (
	apply_irr_deterministic_sle_valuation,
	irr_avg_rate_from_balance,
	resolve_irr_balance_avg_rate,
)
from erpnext_extensions.iran_accounting.domain.stock_reconciliation_sync import (
	assert_stock_reconciliation_row_sle_mirror,
	sync_irr_sle_from_stock_reconciliation_row,
)
from erpnext_extensions.iran_accounting.manufacture_rounding import (
	align_manufacture_finished_good_residual,
	align_manufacture_finished_good_to_outgoing,
)
from erpnext_extensions.iran_accounting.domain.ledger_rounding import round_stock_entry_totals
from erpnext_extensions.iran_accounting.validation import fetch_gl_rows, gl_debit_credit_totals

logger = logging.getLogger(__name__)

REPOST_SUPPORTED_DOCTYPES = ("Stock Reconciliation", "Stock Entry")


def run_post_repost_deterministic_pipeline(
	doc,
	*,
	raise_on_fail: bool = False,
) -> dict:
	"""Mandatory steps 1–2 after engine-layer repost (see module docstring)."""
	voucher_type = doc.doctype
	voucher_no = doc.name
	company = doc.company

	reconcile_out = reconcile_irr_after_repost(doc, validate=False, raise_on_fail=False)
	validation = validate_deterministic_state_after_repost(doc, raise_on_fail=raise_on_fail)
	status = "OK" if validation.get("status") == "PASS" else "FAIL"
	return {
		"status": status,
		"voucher_type": voucher_type,
		"voucher_no": voucher_no,
		"reconcile": reconcile_out,
		"validation": validation,
	}


def reconcile_irr_after_repost(
	doc_or_voucher_type,
	voucher_no: str | None = None,
	company: str | None = None,
	*,
	validate: bool = True,
	raise_on_fail: bool = False,
) -> dict:
	"""Re-assert row → SLE → GL truth after repost (step 1; step 2 is validate_*)."""
	if hasattr(doc_or_voucher_type, "doctype") and hasattr(doc_or_voucher_type, "name"):
		doc = doc_or_voucher_type
		voucher_type = doc.doctype
		voucher_no = doc.name
		company = doc.company
	else:
		voucher_type = str(doc_or_voucher_type)
	from erpnext_extensions.iran_accounting.integration.bootstrap import apply

	apply()

	if voucher_type not in REPOST_SUPPORTED_DOCTYPES:
		return {"status": "SKIP", "reason": "unsupported_doctype"}
	if not frappe.db.exists(voucher_type, voucher_no):
		return {"status": "SKIP", "reason": "missing_voucher"}

	company = company or frappe.db.get_value(voucher_type, voucher_no, "company")
	if not is_irr_company(company):
		return {"status": "SKIP", "reason": "not_irr"}

	actions: list[str] = []
	if voucher_type == "Stock Reconciliation":
		actions.extend(_reconcile_stock_reconciliation_after_repost(voucher_no, company))
	elif voucher_type == "Stock Entry":
		actions.extend(_reconcile_stock_entry_after_repost(voucher_no, company))

	validation: dict = {"status": "SKIP"}
	if validate:
		validation = validate_deterministic_state_after_repost(
			voucher_type, voucher_no, company, raise_on_fail=raise_on_fail
		)
		actions.append(f"validation:{validation.get('status')}")

	logger.info(
		"IRR_REPOST_RECONCILE voucher_type=%s voucher_no=%s actions=%s validation=%s",
		voucher_type,
		voucher_no,
		actions,
		validation.get("status"),
	)
	return {
		"status": "OK",
		"voucher_type": voucher_type,
		"voucher_no": voucher_no,
		"actions": actions,
		"validation": validation,
	}


def _reconcile_stock_reconciliation_after_repost(voucher_no: str, company: str) -> list[str]:
	from erpnext_extensions.iran_accounting.domain.stock_reconciliation import (
		ensure_stock_reconciliation_gl_entries,
	)

	actions: list[str] = []
	doc = frappe.get_doc("Stock Reconciliation", voucher_no)
	if doc.docstatus != 1:
		return ["skip_not_submitted"]

	override_difference_amount(doc)
# Persist valuation_rate / current_valuation_rate after rate-first SR normalize
	for row in doc.items:
		frappe.db.set_value(
			"Stock Reconciliation Item",
			row.name,
			{
				"amount": row.amount,
				"current_amount": row.current_amount,
				"amount_difference": row.amount_difference,
				"quantity_difference": row.quantity_difference,
				"valuation_rate": row.get("valuation_rate"),
				"current_valuation_rate": row.get("current_valuation_rate"),
			},
			update_modified=False,
		)
	frappe.db.set_value(
		"Stock Reconciliation",
		doc.name,
		"difference_amount",
		doc.difference_amount,
		update_modified=False,
	)
	actions.append("reapplied_sr_row_truth")

	for sle_name in frappe.get_all(
		"Stock Ledger Entry",
		pluck="name",
		filters={"voucher_type": "Stock Reconciliation", "voucher_no": voucher_no, "is_cancelled": 0},
	):
		sle = frappe.get_doc("Stock Ledger Entry", sle_name)
		sync_irr_sle_from_stock_reconciliation_row(sle)
		apply_irr_deterministic_sle_valuation(sle, company)
		sle.db_update()
	actions.append("resynced_sr_sle")

	net = sum_stock_reconciliation_amount_difference(doc)
	gl_rows = fetch_gl_rows("Stock Reconciliation", voucher_no)
	debit, credit = gl_debit_credit_totals(gl_rows)
	gl_mag = max(flt(debit), flt(credit))
	if net and gl_mag != abs(net):
		ensure_stock_reconciliation_gl_entries(doc)
		actions.append("regenerated_sr_gl")
	elif not gl_rows and net:
		ensure_stock_reconciliation_gl_entries(doc)
		actions.append("created_sr_gl")

	from erpnext_extensions.iran_accounting.domain.irr_rounding_residual import (
		rebuild_irr_rate_rounding_residual_after_repost,
	)

	actions.extend(rebuild_irr_rate_rounding_residual_after_repost(doc))
	return actions


def _reconcile_stock_entry_after_repost(voucher_no: str, company: str) -> list[str]:
	"""Re-assert capitalization-aware row truth after engine repost.

	Preserves additional_cost / landed_cost_voucher_amount via composition-preserving
	align_stock_entry_item_amounts; never reapplies amount = qty × basic_rate.
	"""
	actions: list[str] = []
	doc = frappe.get_doc("Stock Entry", voucher_no)
	if doc.docstatus != 1:
		return ["skip_not_submitted"]

	align_stock_entry_item_amounts(doc)
	round_stock_entry_totals(doc)
	align_manufacture_finished_good_residual(doc)
	for row in doc.items:
		frappe.db.set_value(
			"Stock Entry Detail",
			row.name,
			{
				"amount": row.amount,
				"basic_amount": row.get("basic_amount"),
				"basic_rate": row.get("basic_rate"),
				"valuation_rate": row.get("valuation_rate"),
				# Never clear capitalization fields — leave ERPNext-owned values intact.
				"additional_cost": row.get("additional_cost"),
				"landed_cost_voucher_amount": row.get("landed_cost_voucher_amount"),
			},
			update_modified=False,
		)
	doc.db_set(
		{
			"total_incoming_value": doc.total_incoming_value,
			"total_outgoing_value": doc.total_outgoing_value,
			"value_difference": doc.value_difference,
		},
		update_modified=False,
	)
	actions.append("reapplied_ste_row_truth_capitalization_aware")

	for sle_name in frappe.get_all(
		"Stock Ledger Entry",
		pluck="name",
		filters={"voucher_type": "Stock Entry", "voucher_no": voucher_no, "is_cancelled": 0},
	):
		sle = frappe.get_doc("Stock Ledger Entry", sle_name)
		sync_irr_sle_from_stock_entry_row(sle)
		apply_irr_deterministic_sle_valuation(sle, company)
		sle.db_update()
	actions.append("resynced_ste_sle")

	mirror_failures = assert_stock_entry_row_sle_mirror(voucher_no, company)
	if mirror_failures:
		actions.append(f"sle_mirror_warnings:{len(mirror_failures)}")

	from erpnext_extensions.iran_accounting.domain.irr_rounding_residual import (
		rebuild_irr_rate_rounding_residual_after_repost,
	)

	actions.extend(rebuild_irr_rate_rounding_residual_after_repost(doc))

	# Contract verifies composition + GL ownership after residual Round Off rebuild.
	from erpnext_extensions.iran_accounting.domain.stock_entry_ledger_contract import (
		collect_ledger_contract_failures,
	)

	contract_failures = collect_ledger_contract_failures(voucher_no, company)
	if contract_failures:
		actions.append(f"contract_warnings:{len(contract_failures)}")
	else:
		actions.append("contract_pass")
	return actions


def validate_deterministic_state_after_repost(
	doc_or_voucher_type,
	voucher_no: str | None = None,
	company: str | None = None,
	*,
	raise_on_fail: bool = False,
) -> dict:
	"""Step 2: SLE/GL/avg-rate assertions after repost (repost alone never passes)."""
	from frappe import _

	if hasattr(doc_or_voucher_type, "doctype") and hasattr(doc_or_voucher_type, "name"):
		doc = doc_or_voucher_type
		voucher_type = doc.doctype
		voucher_no = doc.name
		company = doc.company
	else:
		voucher_type = str(doc_or_voucher_type)

	company = company or frappe.db.get_value(voucher_type, voucher_no, "company")
	if not company or not is_irr_company(company):
		return {"status": "SKIP", "voucher_type": voucher_type, "voucher_no": voucher_no, "failures": []}

	failures: list[str] = []
	failures.extend(_avg_rate_determinism_failures(voucher_type, voucher_no, company))

	if voucher_type == "Stock Reconciliation":
		failures.extend(assert_stock_reconciliation_row_sle_mirror(voucher_no, company))
		from erpnext_extensions.iran_accounting.qty_rate_consistency import check_qty_rate_amount_consistency

		chk = check_qty_rate_amount_consistency("Stock Reconciliation", voucher_no, company)
		if chk.get("status") != "PASS":
			failures.extend(chk.get("consistency_failures") or ["qty_rate_consistency_fail"])
		totals = chk.get("totals") or {}
		if totals.get("difference_vs_gl_residual") not in (0, 0.0, None):
			failures.append(f"gl_residual={totals.get('difference_vs_gl_residual')}")
		if totals.get("difference_vs_sle_residual") not in (0, 0.0, None):
			failures.append(f"sle_residual={totals.get('difference_vs_sle_residual')}")

	elif voucher_type == "Stock Entry":
		from erpnext_extensions.iran_accounting.stock_gl_consistency import (
			assert_stock_entry_ledger_determinism,
		)

		det = assert_stock_entry_ledger_determinism(voucher_no, company)
		if det.get("status") != "PASS":
			failures.extend(det.get("failures") or ["stock_entry_ledger_determinism_fail"])
		mirror = assert_stock_entry_row_sle_mirror(voucher_no, company)
		failures.extend(mirror)

	else:
		return {
			"status": "SKIP",
			"voucher_type": voucher_type,
			"voucher_no": voucher_no,
			"failures": [],
		}

	status = "PASS" if not failures else "FAIL"
	out = {"status": status, "voucher_type": voucher_type, "voucher_no": voucher_no, "failures": failures}
	if failures and raise_on_fail:
		frappe.throw(
			_("Deterministic layer validation failed after repost ({0} {1}): {2}").format(
				voucher_type, voucher_no, "; ".join(failures[:5])
			),
			title=_("IRR Repost — deterministic reconcile required"),
		)
	logger.info(
		"DETERMINISTIC_LAYER_ASSERTED voucher_type=%s voucher_no=%s status=%s",
		voucher_type,
		voucher_no,
		status,
	)
	return out


def _avg_rate_determinism_failures(voucher_type: str, voucher_no: str, company: str) -> list[str]:
	ccy = get_company_currency(company)
	failures = []
	for sle in frappe.get_all(
		"Stock Ledger Entry",
		filters={"voucher_type": voucher_type, "voucher_no": voucher_no, "is_cancelled": 0},
		fields=["name", "stock_value", "qty_after_transaction", "valuation_rate", "voucher_detail_no"],
	):
		expected = resolve_irr_balance_avg_rate(
			{
				"stock_value": sle.get("stock_value"),
				"qty_after_transaction": sle.get("qty_after_transaction"),
				"voucher_type": voucher_type,
				"voucher_detail_no": sle.get("voucher_detail_no"),
			},
			company,
		)
		if flt(sle.get("valuation_rate")) != expected:
			failures.append(
				f"SLE {sle.name}: valuation_rate {sle.get('valuation_rate')} != deterministic avg {expected}"
			)
	return failures


def snapshot_stock_reconciliation_determinism(voucher_no: str, company: str) -> dict:
	"""Capture header, GL magnitude, and per-SLE balance fields for regression tests."""
	doc = frappe.get_doc("Stock Reconciliation", voucher_no)
	override_difference_amount(doc)
	net = sum_stock_reconciliation_amount_difference(doc)
	gl_rows = fetch_gl_rows("Stock Reconciliation", voucher_no)
	debit, credit = gl_debit_credit_totals(gl_rows)
	sles = frappe.get_all(
		"Stock Ledger Entry",
		filters={"voucher_type": "Stock Reconciliation", "voucher_no": voucher_no, "is_cancelled": 0},
		fields=[
			"name",
			"stock_value_difference",
			"stock_value",
			"qty_after_transaction",
			"valuation_rate",
			"incoming_rate",
		],
		order_by="creation asc",
	)
	for sle in sles:
		sle["expected_avg"] = irr_avg_rate_from_balance(
			sle.get("stock_value"), sle.get("qty_after_transaction"), "IRR"
		)
	return {
		"difference_amount": flt(doc.difference_amount),
		"net_row_movement": net,
		"gl_magnitude": max(flt(debit), flt(credit)),
		"sles": sles,
	}

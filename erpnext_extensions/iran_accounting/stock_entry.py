# Copyright (c) 2026, ERPNext Extensions contributors

from __future__ import annotations

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.manufacture_rounding import (
	align_manufacture_finished_good_residual,
	align_manufacture_finished_good_to_outgoing,
)
from erpnext_extensions.iran_accounting.qty_rate_amount import align_stock_entry_item_amounts
from erpnext_extensions.iran_accounting.domain.riv_valuation_guard import (
	assert_stock_entry_valuation_integrity,
)
from erpnext_extensions.iran_accounting.rounding import (
	get_company_currency,
	get_currency_precision,
	is_irr_company,
	round_currency,
	round_stock_entry_totals,
)
from erpnext_extensions.iran_accounting.scrap_costing import (
	apply_iran_manufacture_output_contract,
	permit_scrap_zero_valuation,
)
from erpnext_extensions.iran_accounting.zero_value_transfer import ZERO_VALUE_TRANSFER_STOCK_ENTRY_PURPOSES


def align_zero_value_transfer_totals(doc) -> None:
	"""For IRR internal transfers, collapse rounding-only incoming/outgoing mismatch."""
	if doc.doctype != "Stock Entry" or not is_irr_company(doc.company):
		return
	if doc.purpose not in ZERO_VALUE_TRANSFER_STOCK_ENTRY_PURPOSES:
		return
	company_currency = get_company_currency(doc.company)
	precision = get_currency_precision(company_currency)
	inc = round_currency(doc.total_incoming_value, company_currency)
	out = round_currency(doc.total_outgoing_value, company_currency)
	if inc == out:
		doc.total_incoming_value = inc
		doc.total_outgoing_value = out
		doc.value_difference = 0
		return
	diff = abs(flt(inc) - flt(out))
	max_tol = 1 if precision == 0 else (1.0 / (10**precision))
	if diff <= max_tol:
		aligned = max(inc, out) if precision == 0 else max(inc, out)
		doc.total_incoming_value = aligned
		doc.total_outgoing_value = aligned
		doc.value_difference = 0


def before_validate_stock_entry(doc, method=None):
	from erpnext_extensions.iran_accounting.e2e_bootstrap import apply_stock_entry_site_defaults

	apply_stock_entry_site_defaults(doc)
	# Unpriced scrap is otherwise rejected inside ERPNext's own validate(),
	# before the absorbed cost can be computed at all. See scrap_costing.
	permit_scrap_zero_valuation(doc)


def validate_stock_entry(doc, method=None):
	from erpnext_extensions.iran_accounting.e2e_bootstrap import apply_stock_entry_site_defaults

	apply_stock_entry_site_defaults(doc)
	if not is_irr_company(doc.company):
		return
	align_stock_entry_item_amounts(doc)
	round_stock_entry_totals(doc)
	# Runs after the controller's validate(), so the consumed rows are priced and
	# the cost pool is real. Must precede the residual alignment, which reads the
	# scrap amounts produced here. Same helper as RIV L1.
	apply_iran_manufacture_output_contract(doc)
	align_manufacture_finished_good_residual(doc)
	assert_stock_entry_valuation_integrity(doc)
	from erpnext_extensions.iran_accounting.domain.irr_rounding_residual import (
		assert_round_off_ready_if_needed,
	)

	assert_round_off_ready_if_needed(doc)


def before_submit_stock_entry(doc, method=None):
	validate_stock_entry(doc, method)
	if not is_irr_company(doc.company):
		return
	if hasattr(doc, "set_total_incoming_outgoing_value"):
		doc.set_total_incoming_outgoing_value()
	align_manufacture_finished_good_residual(doc)
	align_zero_value_transfer_totals(doc)
	from erpnext_extensions.iran_accounting.domain.irr_rounding_residual import (
		assert_round_off_ready_if_needed,
	)

	assert_round_off_ready_if_needed(doc)


def on_submit_stock_entry(doc, method=None):
	if not is_irr_company(doc.company):
		return
	# ERPNext may rewrite row rates from moving-average floats after before_submit.
	# Re-apply the Iran Manufacture output contract, rate-first integers, then persist.
	apply_iran_manufacture_output_contract(doc)
	align_stock_entry_item_amounts(doc)
	align_manufacture_finished_good_residual(doc)
	align_zero_value_transfer_totals(doc)
	if hasattr(doc, "set_total_incoming_outgoing_value"):
		doc.set_total_incoming_outgoing_value()
	assert_stock_entry_valuation_integrity(doc)
	for row in doc.get("items") or []:
		row.db_update()
	doc.db_set(
		{
			"total_incoming_value": doc.total_incoming_value,
			"total_outgoing_value": doc.total_outgoing_value,
			"value_difference": doc.value_difference,
		},
		update_modified=False,
	)
	from erpnext_extensions.iran_accounting.domain.stock_entry_ledger_contract import (
		enforce_stock_entry_ledger_contract,
	)

	enforce_stock_entry_ledger_contract(doc.name, doc.company, raise_on_fail=True)


def persist_irr_stock_entry_header_and_rows(doc) -> None:
	"""Persist IRR-aligned row economics and header totals (submit / LCV / RIV)."""
	if not is_irr_company(doc.company):
		return
	align_stock_entry_item_amounts(doc)
	if doc.purpose in ("Manufacture", "Repack"):
		align_manufacture_finished_good_residual(doc)
	align_zero_value_transfer_totals(doc)
	if hasattr(doc, "set_total_incoming_outgoing_value"):
		doc.set_total_incoming_outgoing_value()
	for row in doc.get("items") or []:
		frappe.db.set_value(
			"Stock Entry Detail",
			row.name,
			{
				"basic_rate": row.basic_rate,
				"basic_amount": row.get("basic_amount"),
				"amount": row.amount,
				"valuation_rate": row.valuation_rate,
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


def on_submit_landed_cost_voucher(doc, method=None):
	"""After LCV capitalizes into Stock Entry rows, refresh IRR header totals.

	ERPNext updates Stock Entry Detail amounts/LCV fields but may leave
	total_incoming_value / value_difference stale until a later RIV.
	"""
	for row in doc.get("purchase_receipts") or []:
		if row.receipt_document_type != "Stock Entry" or not row.receipt_document:
			continue
		company = frappe.db.get_value("Stock Entry", row.receipt_document, "company")
		if not company or not is_irr_company(company):
			continue
		se = frappe.get_doc("Stock Entry", row.receipt_document)
		persist_irr_stock_entry_header_and_rows(se)


def before_gl_preview_stock_entry(doc, method=None):
	if doc.doctype != "Stock Entry":
		return
	if hasattr(doc, "calculate_rate_and_amount"):
		doc.calculate_rate_and_amount(reset_outgoing_rate=False, raise_error_if_no_rate=False)
	if is_irr_company(doc.company) and hasattr(doc, "set_total_incoming_outgoing_value"):
		doc.set_total_incoming_outgoing_value()
	round_stock_entry_totals(doc)
	align_manufacture_finished_good_residual(doc)
	align_zero_value_transfer_totals(doc)


def patched_set_total_incoming_outgoing_value(self):
	"""IRR-aware totals; bound to Stock Entry when monkey patches apply."""
	if not is_irr_company(self.company):
		if _original_set_total_incoming_outgoing_value:
			return _original_set_total_incoming_outgoing_value(self)
		return

	company_currency = get_company_currency(self.company)
	self.total_incoming_value = self.total_outgoing_value = 0.0
	for d in self.get("items"):
		if d.t_warehouse:
			self.total_incoming_value += flt(d.amount)
		if d.s_warehouse:
			self.total_outgoing_value += flt(d.amount)

	# Do not round summed totals — rows are already IRR integers.
	self.value_difference = flt(self.total_incoming_value) - flt(self.total_outgoing_value)
	align_manufacture_finished_good_residual(self)
	align_zero_value_transfer_totals(self)


_original_set_total_incoming_outgoing_value = None

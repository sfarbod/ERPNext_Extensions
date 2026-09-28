# Copyright (c) 2026, ERPNext Extensions contributors
"""Fail-fast ledger contract: verify capitalization-aware Stock Entry row → SLE → GL (IRR)."""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt

from erpnext_extensions.iran_accounting.domain.currency import (
	get_company_currency,
	get_currency_precision,
	integer_valuation_rate_from_amount,
	is_irr_company,
	rate_is_fractional,
	round_currency,
	round_row_amount_financial,
)
from erpnext_extensions.iran_accounting.domain.qty_rate_amount import compose_stock_entry_row_amount
from erpnext_extensions.iran_accounting.domain.stock_entry_sync import (
	assert_stock_entry_row_sle_mirror,
	stock_entry_row_amount,
	sum_stock_entry_row_amounts,
)
from erpnext_extensions.iran_accounting.validation import (
	assert_no_fractional_irr_gl,
	assert_no_fractional_irr_sle,
	fetch_gl_rows,
	fractional_gl_fields,
	fractional_sle_fields,
	gl_debit_credit_totals,
)
from erpnext_extensions.iran_accounting.zero_value_transfer import (
	ZERO_VALUE_TRANSFER_STOCK_ENTRY_PURPOSES,
	expected_balanced_transfer_gl_magnitude,
)


def _assert_type_c_manufacture_residual(doc, sle_sum: float, signed_net: float, company: str) -> list[str]:
	"""Prove TYPE C voucher-level residual matches classifier exactly — no tolerance.

	For TYPE C: expected_net = Σ(incoming capitalization) − residual.
	With no real capitalization this is simply −residual (e.g. MAT-STE-2026-37762).
	"""
	if doc.purpose != "Manufacture":
		return []
	from erpnext_extensions.iran_accounting.domain.manufacture_irr_residual import (
		CLASS_TYPE_C,
		classify_manufacture_irr_residual,
		uses_type_c_sa_residual_policy,
	)

	if not uses_type_c_sa_residual_policy(doc):
		return []

	result = classify_manufacture_irr_residual(doc)
	if result.classification != CLASS_TYPE_C:
		return []

	ccy = get_company_currency(company)
	incoming_cap = sum(
		flt(row.get("additional_cost")) + flt(row.get("landed_cost_voucher_amount"))
		for row in (doc.get("items") or [])
		if row.get("t_warehouse")
	)
	expected_net = round_currency(incoming_cap - result.residual, ccy)
	failures = []
	if flt(signed_net) != flt(expected_net):
		failures.append(
			f"{doc.doctype} {doc.name}: TYPE C signed Σ row.amount {signed_net} "
			f"!= expected {expected_net} (cap {incoming_cap} − residual {result.residual})"
		)
	if flt(sle_sum) != flt(expected_net):
		failures.append(
			f"{doc.doctype} {doc.name}: TYPE C Σ SLE {sle_sum} "
			f"!= expected {expected_net} (cap {incoming_cap} − residual {result.residual})"
		)
	return failures


def _tol(company: str) -> float:
	ccy = get_company_currency(company)
	precision = get_currency_precision(ccy)
	return 1.0 if precision == 0 else (1.0 / (10**precision))


def _fail(doc, row, relationship: str, expected, actual) -> str:
	item = getattr(row, "item_code", None) or row.get("item_code")
	name = getattr(row, "name", None) or row.get("name")
	idx = getattr(row, "idx", None) or row.get("idx")
	return (
		f"{doc.doctype} {doc.name} row {idx} item={item} name={name}: "
		f"{relationship} expected={expected} actual={actual}"
	)


def signed_net_row_amount_sum(doc) -> float:
	"""Σ signed row.amount by warehouse direction (transfer rows excluded from net)."""
	total = 0.0
	for row in doc.get("items") or []:
		amt = stock_entry_row_amount(row, doc.company)
		if row.get("s_warehouse") and row.get("t_warehouse"):
			continue
		if row.get("t_warehouse"):
			total += amt
		elif row.get("s_warehouse"):
			total -= amt
	return total


def expected_header_totals(doc) -> tuple[float, float]:
	"""Incoming/outgoing header = Σ row.amount per leg (no post-sum round)."""
	inc = out = 0.0
	for row in doc.get("items") or []:
		amt = stock_entry_row_amount(row, doc.company)
		if row.get("t_warehouse"):
			inc += amt
		if row.get("s_warehouse"):
			out += amt
	return inc, out


def _gl_stock_account_net(gl_rows: list[dict], company: str) -> float:
	stock_accounts = set(
		frappe.db.sql_list(
			"""
			select name from `tabAccount`
			where company=%s and account_type='Stock' and is_group=0
			""",
			company,
		)
	)
	net = 0.0
	for row in gl_rows:
		if row.get("account") not in stock_accounts:
			continue
		net += flt(row.get("debit")) - flt(row.get("credit"))
	return net


def _single_manufacture_fg_row(doc, row) -> bool:
	"""True for the sole Manufacture finished-good incoming row (pool residual owner)."""
	if getattr(doc, "purpose", None) != "Manufacture":
		return False
	if not row.get("is_finished_item") or not row.get("t_warehouse"):
		return False
	fg_count = sum(
		1 for r in doc.get("items") or [] if r.get("is_finished_item") and r.get("t_warehouse")
	)
	return fg_count == 1


def _assert_row_composition(doc, company: str) -> list[str]:
	"""Verify rate-first IRR composition (verifier, not calculator).

	- basic_rate / valuation_rate must be integer (IRR)
	- basic_amount == ROUND_HALF_UP(transfer_qty × integer basic_rate)
	  except single-FG Manufacture rows, which may absorb a valid Manufacture
	  pool residual into basic_amount (amount remains authoritative for SLE)
	- amount == basic_amount + additional_cost + LCV
	- valuation_rate == ROUND_HALF_UP(amount / transfer_qty); amount remains authoritative
	"""
	failures = []
	ccy = get_company_currency(company)
	tol = _tol(company)
	for row in doc.get("items") or []:
		transfer_qty = flt(
			row.get("transfer_qty") if row.get("transfer_qty") not in (None, "") else row.get("qty")
		)
		if row.get("basic_rate") is not None and rate_is_fractional(row.basic_rate, ccy):
			failures.append(
				_fail(doc, row, "basic_rate integer IRR", "ROUND_HALF_UP(basic_rate,0)", row.basic_rate)
			)
		if row.get("valuation_rate") is not None and rate_is_fractional(row.valuation_rate, ccy):
			failures.append(
				_fail(
					doc,
					row,
					"valuation_rate integer IRR",
					"ROUND_HALF_UP(valuation_rate,0)",
					row.valuation_rate,
				)
			)

		if row.get("basic_rate") is not None and transfer_qty:
			exp_basic = round_row_amount_financial(transfer_qty, row.basic_rate, ccy)
			if abs(flt(row.basic_amount) - exp_basic) > tol and not _single_manufacture_fg_row(doc, row):
				failures.append(
					_fail(
						doc,
						row,
						"basic_amount == transfer_qty × integer basic_rate",
						exp_basic,
						row.basic_amount,
					)
				)

		exp_amount = compose_stock_entry_row_amount(row, ccy)
		if abs(flt(row.amount) - exp_amount) > tol:
			failures.append(
				_fail(
					doc,
					row,
					"amount ≈ basic_amount + additional_cost + landed_cost_voucher_amount",
					exp_amount,
					row.amount,
				)
			)

		if transfer_qty and flt(row.amount):
			exp_rate = integer_valuation_rate_from_amount(row.amount, transfer_qty, ccy)
			if abs(flt(row.valuation_rate) - exp_rate) > tol:
				failures.append(
					_fail(
						doc,
						row,
						"valuation_rate == ROUND_HALF_UP(amount / transfer_qty)",
						exp_rate,
						row.valuation_rate,
					)
				)
	return failures


def _assert_irr_rate_rounding_residual_gl(doc, gl_rows: list[dict], company: str) -> list[str]:
	"""Round Off GL must match IRR rate residual; zero residual → no residual lines."""
	from erpnext_extensions.iran_accounting.domain.irr_rounding_residual import (
		expected_round_off_gl_totals,
		is_irr_rate_rounding_residual_gl,
		resolve_company_round_off,
	)

	failures = []
	exp = expected_round_off_gl_totals(doc)
	residual_rows = [r for r in gl_rows if is_irr_rate_rounding_residual_gl(r, company=company)]
	if not exp["net_signed_debit"]:
		if residual_rows:
			failures.append(
				f"{doc.doctype} {doc.name}: zero IRR rate residual but Round Off residual GL present"
			)
		return failures

	if not residual_rows:
		# 3.8.6: missing partner / Class B must fail at apply — soft-skip is not allowed.
		# If expected net is non-zero but no residual GL, report failure (apply should have thrown).
		failures.append(
			f"{doc.doctype} {doc.name}: expected IRR Round Off residual GL "
			f"(net_signed_debit={exp['net_signed_debit']}) but none posted "
			f"(no Stock Adjustment fallback; partner/config must fail closed)"
		)
		return failures

	if len(residual_rows) != 1:
		failures.append(
			f"{doc.doctype} {doc.name}: expected 1 IRR Round Off residual GL row, got {len(residual_rows)}"
		)
		return failures

	cfg = resolve_company_round_off(company, require=False)
	row = residual_rows[0]
	if cfg.get("account") and row.get("account") != cfg["account"]:
		failures.append(
			f"{doc.doctype} {doc.name}: residual Round Off account {row.get('account')} "
			f"!= Company.round_off_account {cfg.get('account')}"
		)
	if cfg.get("cost_center") and row.get("cost_center") != cfg["cost_center"]:
		failures.append(
			f"{doc.doctype} {doc.name}: residual Round Off cost center {row.get('cost_center')} "
			f"!= Company.round_off_cost_center {cfg.get('cost_center')}"
		)
	if flt(row.get("debit")) != flt(exp["debit"]) or flt(row.get("credit")) != flt(exp["credit"]):
		failures.append(
			f"{doc.doctype} {doc.name}: residual Round Off GL debit/credit "
			f"{row.get('debit')}/{row.get('credit')} != expected {exp['debit']}/{exp['credit']}"
		)
	return failures


def _assert_additional_cost_gl(doc, gl_rows: list[dict]) -> list[str]:
	failures = []
	add_accounts = {
		t.expense_account
		for t in doc.get("additional_costs") or []
		if t.expense_account and flt(t.base_amount)
	}
	if not add_accounts:
		return failures
	posted = {r.get("account") for r in gl_rows if flt(r.get("credit")) or flt(r.get("debit"))}
	missing = add_accounts - posted
	for account in sorted(missing):
		failures.append(
			f"{doc.doctype} {doc.name}: Additional Cost GL missing for expense_account={account}"
		)
	return failures


def _assert_lcv_gl(doc, gl_rows: list[dict]) -> list[str]:
	failures = []
	has_lcv = any(flt(r.get("landed_cost_voucher_amount")) for r in doc.get("items") or [])
	if not has_lcv:
		return failures
	# Presence check: remarks or accounts from linked LCV — at minimum GL must be balanced
	# and inventory net should reflect capitalization (covered elsewhere). Detect LCV remarks.
	lcv_remarks = [
		r
		for r in gl_rows
		if "LCV" in (r.get("remarks") or "") or "Landed Cost" in (r.get("remarks") or "")
	]
	if not lcv_remarks:
		# Soft signal: if LCV amount on rows, expect non-transfer SE to have extra credit legs
		# beyond pure stock pairs. Fail explicitly for clarity.
		failures.append(
			f"{doc.doctype} {doc.name}: landed_cost_voucher_amount present on items "
			f"but no LCV-related GL remarks found"
		)
	return failures


def collect_ledger_contract_failures(voucher_no: str, company: str) -> list[str]:
	if not frappe.db.exists("Stock Entry", voucher_no):
		return [f"Stock Entry {voucher_no} not found"]

	doc = frappe.get_doc("Stock Entry", voucher_no)
	failures = list(assert_stock_entry_row_sle_mirror(voucher_no, company))
	failures.extend(_assert_row_composition(doc, company))

	row_gross = sum_stock_entry_row_amounts(doc)
	sle_sum = flt(
		frappe.db.sql(
			"""
			select coalesce(sum(stock_value_difference), 0)
			from `tabStock Ledger Entry`
			where voucher_type='Stock Entry' and voucher_no=%s and is_cancelled=0
			""",
			voucher_no,
		)[0][0]
	)
	sle_pos = flt(
		frappe.db.sql(
			"""
			select coalesce(sum(stock_value_difference), 0)
			from `tabStock Ledger Entry`
			where voucher_type='Stock Entry' and voucher_no=%s and is_cancelled=0
			  and stock_value_difference > 0
			""",
			voucher_no,
		)[0][0]
	)

	signed_net = signed_net_row_amount_sum(doc)
	if doc.purpose in ZERO_VALUE_TRANSFER_STOCK_ENTRY_PURPOSES and flt(doc.value_difference) == 0:
		if sle_sum != 0:
			failures.append(
				f"{doc.doctype} {doc.name}: transfer Σ SLE movement must be 0, got {sle_sum}"
			)
		if abs(sle_pos - row_gross) != 0:
			failures.append(
				f"{doc.doctype} {doc.name}: transfer |Σ SLE+| {sle_pos} != Σ row.amount {row_gross}"
			)
	else:
		if sle_sum != signed_net:
			failures.append(
				f"{doc.doctype} {doc.name}: Σ SLE {sle_sum} != signed Σ row.amount {signed_net}"
			)
		if abs(sle_sum) != row_gross and doc.purpose in ("Material Receipt", "Material Issue"):
			failures.append(
				f"{doc.doctype} {doc.name}: |Σ SLE| {abs(sle_sum)} != Σ row.amount {row_gross}"
			)
		failures.extend(_assert_type_c_manufacture_residual(doc, sle_sum, signed_net, company))


	exp_inc, exp_out = expected_header_totals(doc)
	if flt(doc.total_incoming_value) != exp_inc:
		failures.append(
			f"{doc.doctype} {doc.name}: header incoming {doc.total_incoming_value} != Σ row.amount in {exp_inc}"
		)
	if flt(doc.total_outgoing_value) != exp_out:
		failures.append(
			f"{doc.doctype} {doc.name}: header outgoing {doc.total_outgoing_value} != Σ row.amount out {exp_out}"
		)

	if doc.docstatus == 1:
		gl_rows = fetch_gl_rows("Stock Entry", voucher_no)
		debit, credit = gl_debit_credit_totals(gl_rows)
		gl_net = debit - credit
		gl_stock_net = _gl_stock_account_net(gl_rows, company)

		if doc.purpose in ZERO_VALUE_TRANSFER_STOCK_ENTRY_PURPOSES and flt(doc.value_difference) == 0:
			expected_gl_mag = expected_balanced_transfer_gl_magnitude(doc)
			if max(debit, credit) != expected_gl_mag:
				failures.append(
					f"{doc.doctype} {doc.name}: GL magnitude {max(debit, credit)} "
					f"!= expected transfer GL {expected_gl_mag}"
				)
		else:
			if abs(gl_stock_net) != abs(sle_sum):
				failures.append(
					f"{doc.doctype} {doc.name}: |GL stock net| {abs(gl_stock_net)} != |Σ SLE| {abs(sle_sum)}"
				)
			if gl_net != 0:
				failures.append(f"{doc.doctype} {doc.name}: GL debit-credit net must be 0, got {gl_net}")
			failures.extend(_assert_additional_cost_gl(doc, gl_rows))
			failures.extend(_assert_lcv_gl(doc, gl_rows))
			failures.extend(_assert_irr_rate_rounding_residual_gl(doc, gl_rows, company))

		if not assert_no_fractional_irr_gl("Stock Entry", voucher_no, company):
			bad = []
			for row in gl_rows:
				bad.extend(fractional_gl_fields(row, company))
			failures.append(f"{doc.doctype} {doc.name}: fractional IRR in GL: {bad[:3]}")
		if not assert_no_fractional_irr_sle("Stock Entry", voucher_no, company):
			from erpnext_extensions.iran_accounting.validation import fetch_sle_rows

			bad = []
			for row in fetch_sle_rows("Stock Entry", voucher_no):
				bad.extend(fractional_sle_fields(row, company))
			failures.append(f"{doc.doctype} {doc.name}: fractional IRR in SLE: {bad[:3]}")

	return failures


def enforce_stock_entry_ledger_contract(
	voucher_no: str,
	company: str | None = None,
	*,
	raise_on_fail: bool = True,
) -> dict:
	company = company or frappe.db.get_value("Stock Entry", voucher_no, "company")
	if not is_irr_company(company):
		return {"status": "SKIP", "voucher_no": voucher_no}

	failures = collect_ledger_contract_failures(voucher_no, company)
	status = "PASS" if not failures else "FAIL"
	out = {
		"status": status,
		"voucher_no": voucher_no,
		"company": company,
		"currency": get_company_currency(company),
		"failures": failures,
	}
	if failures and raise_on_fail:
		frappe.throw(
			_("Stock Entry ledger contract violation ({0}): {1}").format(voucher_no, "; ".join(failures[:5])),
			title=_("IRR Ledger Determinism"),
		)
	return out

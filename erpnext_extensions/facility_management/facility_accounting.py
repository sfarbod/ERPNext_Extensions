# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

from __future__ import annotations

from decimal import Decimal
from typing import Any

import frappe
from frappe.utils import cint, flt

from erpnext_extensions.facility_management.facility_balances import get_facility_balance_row
from erpnext_extensions.facility_management.facility_monetary import (
	get_exact_currency_decimal,
	parse_facility_amount,
)
from erpnext_extensions.facility_management.facility_settings_doc import (
	DEFAULT_RECEIPT_BANK_ROW,
	DEFAULT_RECEIPT_DEFERRED_ROW,
	DEFAULT_RECEIPT_LOAN_PRINCIPAL_ROW,
	DEFAULT_RECEIPT_LOAN_PROFIT_ROW,
	DEFAULT_RECEIPT_LOAN_ROW,
	DEFAULT_RECEIPT_REMARKS,
	DEFAULT_REPAYMENT_BANK_ROW,
	DEFAULT_REPAYMENT_PENALTY_ROW,
	DEFAULT_REPAYMENT_PRINCIPAL_ROW,
	DEFAULT_REPAYMENT_PROFIT_ROW,
	DEFAULT_REPAYMENT_REMARKS,
	_je_account_has_field,
	account_requires_cost_center,
	get_facility_settings_doc,
	resolve_account,
	resolve_dimension,
	resolve_repayment_cost_center,
	template_chain,
	validate_repayment_je_prerequisites,
)
from erpnext_extensions.facility_management.facility_templates import (
	build_template_context,
	render_facility_template,
)


def get_facility_dimension_fieldname() -> str | None:
	rows = frappe.get_all(
		"Accounting Dimension",
		filters={"document_type": "Facility", "disabled": 0},
		pluck="fieldname",
		limit=1,
	)
	if not rows:
		return None
	return (rows[0] or "").strip() or None


def facility_dimension_on_row(facility_name: str) -> dict[str, Any]:
	fn = get_facility_dimension_fieldname()
	if not fn or not facility_name:
		return {}
	return {fn: facility_name}


def _je_row_dim_fieldnames() -> list[str]:
	fields = ["cost_center", "department", "bank_dimension", "bank_account_dimension"]
	fn = get_facility_dimension_fieldname()
	if fn and fn not in fields:
		fields.append(fn)
	return [f for f in fields if _je_account_has_field(f)]


def _row_dimension_snapshot(row) -> dict[str, Any]:
	out: dict[str, Any] = {}
	for fn in _je_row_dim_fieldnames():
		val = getattr(row, fn, None)
		if val not in (None, ""):
			out[fn] = val
	return out


def _assert_row_dims_empty(row, forbidden: set[str], *, label: str) -> None:
	for fn in forbidden:
		if not _je_account_has_field(fn):
			continue
		val = getattr(row, fn, None)
		if val not in (None, ""):
			frappe.throw(
				frappe._("{0}: row {1} account {2} must not have {3} (got {4!r})").format(
					label, row.idx, row.account, fn, val
				)
			)


def _validate_receipt_je_dimensions(je_name: str, facility) -> None:
	plan = build_receipt_je_plan(facility)
	je = frappe.get_doc("Journal Entry", je_name)
	if len(je.accounts) != len(plan):
		frappe.throw(
			frappe._("Receipt JE row count {0} does not match expected {1}").format(
				len(je.accounts), len(plan)
			)
		)
	dim_fn = get_facility_dimension_fieldname()
	for row, spec in zip(je.accounts, plan, strict=True):
		if row.account != spec["account"]:
			frappe.throw(
				frappe._("Receipt JE row {0}: expected account {1}, got {2}").format(
					row.idx, spec["account"], row.account
				)
			)
		exp_debit = spec["debit"]
		if exp_debit:
			if flt(row.debit_in_account_currency) != flt(spec["amount"]):
				frappe.throw(frappe._("Receipt JE row {0}: debit amount mismatch").format(row.idx))
		else:
			if flt(row.credit_in_account_currency) != flt(spec["amount"]):
				frappe.throw(frappe._("Receipt JE row {0}: credit amount mismatch").format(row.idx))
		for fn, val in (spec.get("dims") or {}).items():
			if getattr(row, fn, None) != val:
				frappe.throw(
					frappe._("Receipt JE row {0}: expected {1}={2!r}, got {3!r}").format(
						row.idx, fn, val, getattr(row, fn, None)
					)
				)
		role = spec.get("role")
		if role == "bank":
			forbidden = {dim_fn, "department", "bank_account_dimension", "cost_center"} - set(
				(spec.get("dims") or {}).keys()
			)
			_assert_row_dims_empty(row, {f for f in forbidden if f}, label="Receipt JE bank row")
		elif role in ("deferred", "loan_principal", "loan_profit"):
			forbidden = {"department", "bank_dimension", "bank_account_dimension", "cost_center"}
			if dim_fn:
				forbidden.discard(dim_fn)
			_assert_row_dims_empty(row, forbidden, label=f"Receipt JE {role} row")


def _validate_repayment_je_dimensions(je_name: str, facility, repayment) -> None:
	plan = build_repayment_je_plan(repayment, facility)
	je = frappe.get_doc("Journal Entry", je_name)
	if len(je.accounts) != len(plan):
		frappe.throw(
			frappe._("Repayment JE row count {0} does not match expected {1}").format(
				len(je.accounts), len(plan)
			)
		)
	dim_fn = get_facility_dimension_fieldname()
	for row, spec in zip(je.accounts, plan, strict=True):
		if row.account != spec["account"]:
			frappe.throw(
				frappe._("Repayment JE row {0}: expected account {1}, got {2}").format(
					row.idx, spec["account"], row.account
				)
			)
		exp_debit = spec["debit"]
		if exp_debit:
			if flt(row.debit_in_account_currency) != flt(spec["amount"]):
				frappe.throw(frappe._("Repayment JE row {0}: debit amount mismatch").format(row.idx))
		else:
			if flt(row.credit_in_account_currency) != flt(spec["amount"]):
				frappe.throw(frappe._("Repayment JE row {0}: credit amount mismatch").format(row.idx))
		for fn, val in (spec.get("dims") or {}).items():
			if getattr(row, fn, None) != val:
				frappe.throw(
					frappe._("Repayment JE row {0}: expected {1}={2!r}, got {3!r}").format(
						row.idx, fn, val, getattr(row, fn, None)
					)
				)
		role = spec.get("role")
		if role in ("bank", "debt_purchase_in_collection"):
			forbidden = {dim_fn, "department", "bank_account_dimension", "cost_center"} - set(
				(spec.get("dims") or {}).keys()
			)
			_assert_row_dims_empty(row, {f for f in forbidden if f}, label=f"Repayment JE {role} row")
		elif role in ("loan", "loan_profit", "deferred_credit"):
			forbidden = {"department", "bank_dimension", "bank_account_dimension", "cost_center"}
			if dim_fn:
				forbidden.discard(dim_fn)
			_assert_row_dims_empty(row, forbidden, label=f"Repayment JE {role} row")
		elif role in ("penalty", "interest_expense"):
			forbidden = {"bank_dimension", "bank_account_dimension"}
			_assert_row_dims_empty(row, forbidden, label=f"Repayment JE {role} row")


def receipt_je_row_dimensions(
	row_role: str,
	facility_name: str,
	*,
	facility,
	settings=None,
) -> dict[str, Any]:
	"""Finance Excel — receipt JE row dimensions (row-specific, not global)."""
	if row_role == "bank":
		out: dict[str, Any] = {}
		bank_dim = resolve_dimension("bank_dimension", facility=facility, settings=settings)
		if bank_dim and _je_account_has_field("bank_dimension"):
			out["bank_dimension"] = bank_dim
		return out
	if row_role in ("deferred", "loan", "loan_principal", "loan_profit"):
		return facility_dimension_on_row(facility_name)
	return {}


def repayment_je_row_dimensions(
	row_role: str,
	facility_name: str,
	*,
	repayment,
	facility,
	settings=None,
	pdc_party: dict[str, str] | None = None,
) -> dict[str, Any]:
	"""Finance Excel — repayment JE row dimensions (independent from receipt rules)."""
	if row_role == "bank":
		out: dict[str, Any] = {}
		bank_dim = resolve_dimension(
			"bank_dimension", repayment=repayment, facility=facility, settings=settings
		)
		if bank_dim and _je_account_has_field("bank_dimension"):
			out["bank_dimension"] = bank_dim
		return out
	if row_role == "debt_purchase_in_collection":
		# Keep bank_dimension shape for settlement credit; add selected cheque Party only.
		out: dict[str, Any] = {}
		bank_dim = resolve_dimension(
			"bank_dimension", repayment=repayment, facility=facility, settings=settings
		)
		if bank_dim and _je_account_has_field("bank_dimension"):
			out["bank_dimension"] = bank_dim
		if pdc_party:
			pt = (pdc_party.get("party_type") or "").strip()
			p = (pdc_party.get("party") or "").strip()
			if pt and p:
				out["party_type"] = pt
				out["party"] = p
		return out
	if row_role in ("loan", "loan_profit", "deferred_credit"):
		return facility_dimension_on_row(facility_name)
	if row_role in ("penalty", "interest_expense"):
		out: dict[str, Any] = {}
		cc = resolve_repayment_cost_center(repayment=repayment, facility=facility, settings=settings)
		if cc and _je_account_has_field("cost_center"):
			out["cost_center"] = cc
		dept = resolve_dimension("department", repayment=repayment, facility=facility, settings=settings)
		if dept and _je_account_has_field("department"):
			out["department"] = dept
		out.update(facility_dimension_on_row(facility_name))
		return out
	return {}


def _je_amount_pair(amount: Decimal, *, debit: bool) -> dict[str, str]:
	s = format(amount, "f")
	if debit:
		return {"debit_in_account_currency": s, "debit": s}
	return {"credit_in_account_currency": s, "credit": s}


def _merge_row_dims(facility_name: str, extra: dict[str, Any]) -> dict[str, Any]:
	"""Deprecated helper — prefer receipt_je_row_dimensions / repayment_je_row_dimensions."""
	row = dict(facility_dimension_on_row(facility_name))
	row.update(extra or {})
	return row


def _append_je_row(
	je,
	*,
	account: str,
	amount: Decimal,
	debit: bool,
	row_dims: dict[str, Any],
	user_remark: str | None = None,
):
	if amount <= 0:
		return
	payload = {"account": account, **_je_amount_pair(amount, debit=debit), **row_dims}
	if user_remark and frappe.get_meta("Journal Entry Account").has_field("user_remark"):
		payload["user_remark"] = user_remark
	je.append("accounts", payload)


def _enforce_receipt_je_row_dims_from_plan(je, plan: list[dict[str, Any]]) -> None:
	_enforce_je_row_dims_from_plan(je, plan)


def _enforce_je_row_dims_from_plan(je, plan: list[dict[str, Any]]) -> None:
	"""Strip ERPNext default dimensions so submitted rows match the JE plan."""
	fields = _je_row_dim_fieldnames()
	for row, spec in zip(je.accounts, plan, strict=True):
		dims = spec.get("dims") or {}
		for fn in fields:
			row.set(fn, dims.get(fn))
		# Party only when the plan explicitly sets it (DPIC settlement credit).
		for fn in ("party_type", "party"):
			if fn in dims:
				row.set(fn, dims.get(fn))
			elif getattr(row, fn, None) not in (None, ""):
				row.set(fn, None)


def _planned_receipt_rows(principal: Decimal, profit: Decimal) -> list[tuple[Decimal, str]]:
	rows: list[tuple[Decimal, str]] = [(principal, "debit")]
	if profit > 0:
		rows.append((profit, "debit"))
	rows.append((principal, "credit"))
	if profit > 0:
		rows.append((profit, "credit"))
	return rows


def _planned_repayment_rows(
	principal: Decimal, profit: Decimal, penalty: Decimal
) -> list[tuple[Decimal, str]]:
	"""Mirror build_repayment_je_plan row order for exact amount SQL sync."""
	total = principal + profit + penalty
	rows: list[tuple[Decimal, str]] = [(total, "credit")]
	if principal > 0:
		rows.append((principal, "debit"))
	if profit > 0:
		rows.append((profit, "debit"))
	if penalty > 0:
		rows.append((penalty, "debit"))
	if profit > 0:
		rows.append((profit, "credit"))
		rows.append((profit, "debit"))
	return rows


def _apply_exact_je_planned(je_name: str, planned: list[tuple[Decimal, str]]) -> None:
	rows = frappe.db.sql(
		"""
		SELECT name FROM `tabJournal Entry Account`
		WHERE parent = %s
		ORDER BY idx ASC
		""",
		(je_name,),
	)
	if len(rows) != len(planned):
		frappe.throw(
			frappe._("Journal Entry {0} row count {1} does not match planned lines {2}").format(
				je_name, len(rows), len(planned)
			)
		)
	total_debit = Decimal("0")
	total_credit = Decimal("0")
	for (row_name,), (amount, kind) in zip(rows, planned, strict=True):
		amount_str = format(amount, "f")
		if kind == "debit":
			total_debit += amount
			frappe.db.sql(
				"""
				UPDATE `tabJournal Entry Account`
				SET debit_in_account_currency = %s, debit = %s,
					credit_in_account_currency = 0, credit = 0
				WHERE name = %s
				""",
				(amount_str, amount_str, row_name),
			)
		else:
			total_credit += amount
			frappe.db.sql(
				"""
				UPDATE `tabJournal Entry Account`
				SET credit_in_account_currency = %s, credit = %s,
					debit_in_account_currency = 0, debit = 0
				WHERE name = %s
				""",
				(amount_str, amount_str, row_name),
			)
	total_str = format(max(total_debit, total_credit), "f")
	frappe.db.sql(
		"""
		UPDATE `tabJournal Entry`
		SET total_debit = %s, total_credit = %s, difference = 0
		WHERE name = %s
		""",
		(total_str, total_str, je_name),
	)


def _apply_exact_gl_planned(je_name: str, planned: list[tuple[Decimal, str]]) -> None:
	gl_rows = frappe.db.sql(
		"""
		SELECT name FROM `tabGL Entry`
		WHERE voucher_type = 'Journal Entry' AND voucher_no = %s AND is_cancelled = 0
		ORDER BY creation ASC, name ASC
		""",
		(je_name,),
	)
	if len(gl_rows) != len(planned):
		return
	for (row_name,), (amount, kind) in zip(gl_rows, planned, strict=True):
		amount_str = format(amount, "f")
		if kind == "debit":
			frappe.db.sql(
				"""
				UPDATE `tabGL Entry`
				SET debit = %s, credit = 0, debit_in_account_currency = %s, credit_in_account_currency = 0
				WHERE name = %s
				""",
				(amount_str, amount_str, row_name),
			)
		else:
			frappe.db.sql(
				"""
				UPDATE `tabGL Entry`
				SET credit = %s, debit = 0, credit_in_account_currency = %s, debit_in_account_currency = 0
				WHERE name = %s
				""",
				(amount_str, amount_str, row_name),
			)


def _sync_je_credit_to_debit_sum(je_name: str) -> None:
	total = frappe.db.sql(
		"""
		SELECT COALESCE(SUM(debit_in_account_currency), 0)
		FROM `tabJournal Entry Account`
		WHERE parent = %s
		""",
		(je_name,),
	)[0][0]
	total_str = format(Decimal(str(total)), "f")
	for (row_name,) in frappe.db.sql(
		"""
		SELECT name FROM `tabJournal Entry Account`
		WHERE parent = %s AND credit_in_account_currency > 0
		""",
		(je_name,),
	):
		frappe.db.sql(
			"""
			UPDATE `tabJournal Entry Account`
			SET credit_in_account_currency = %s, credit = %s
			WHERE name = %s
			""",
			(total_str, total_str, row_name),
		)
	frappe.db.sql(
		"""
		UPDATE `tabJournal Entry`
		SET total_debit = %s, total_credit = %s, difference = 0
		WHERE name = %s
		""",
		(total_str, total_str, je_name),
	)


def _facility_receipt_amounts(facility) -> tuple[Decimal, Decimal]:
	if getattr(facility, "name", None) and frappe.db.exists("Facility", facility.name):
		principal = get_exact_currency_decimal("Facility", facility.name, "principal_amount")
		profit = get_exact_currency_decimal("Facility", facility.name, "profit_amount")
	else:
		principal = parse_facility_amount(facility.get("principal_amount"))
		profit = parse_facility_amount(facility.get("profit_amount"))
	return principal, profit


def _receipt_row_templates(facility, settings) -> dict[str, str]:
	return {
		"remark": template_chain(
			facility_key="receipt_remarks_template",
			settings_key="default_receipt_remarks_template",
			facility=facility,
			settings=settings,
			default=DEFAULT_RECEIPT_REMARKS,
		),
		"bank": template_chain(
			facility_key="",
			settings_key="default_receipt_bank_row_description_template",
			facility=None,
			settings=settings,
			default=DEFAULT_RECEIPT_BANK_ROW,
		),
		"deferred": template_chain(
			facility_key="",
			settings_key="default_receipt_deferred_interest_row_description_template",
			facility=None,
			settings=settings,
			default=DEFAULT_RECEIPT_DEFERRED_ROW,
		),
		"loan": template_chain(
			facility_key="",
			settings_key="default_receipt_loan_payable_row_description_template",
			facility=None,
			settings=settings,
			default=DEFAULT_RECEIPT_LOAN_ROW,
		),
		"loan_principal": template_chain(
			facility_key="",
			settings_key="default_receipt_loan_principal_row_description_template",
			facility=None,
			settings=settings,
			default=DEFAULT_RECEIPT_LOAN_PRINCIPAL_ROW,
		),
		"loan_profit": template_chain(
			facility_key="",
			settings_key="default_receipt_loan_profit_row_description_template",
			facility=None,
			settings=settings,
			default=DEFAULT_RECEIPT_LOAN_PROFIT_ROW,
		),
	}


def _validate_receipt_je_prerequisites(facility, *, principal: Decimal, profit: Decimal) -> None:
	if cint(facility.is_opening_facility):
		frappe.throw(
			frappe._("Opening / migrated facilities must not create a Receipt Journal Entry."),
			title=frappe._("Facility Receipt"),
		)
	if principal < 0 or profit < 0:
		frappe.throw(frappe._("Principal and profit amounts cannot be negative."))
	settings = get_facility_settings_doc(facility.company)
	if principal <= 0:
		frappe.throw(frappe._("Principal amount must be positive to receive facility."))
	if profit > 0:
		resolve_account(
			"deferred_loan_interest_account",
			facility=facility,
			settings=settings,
			required=True,
			required_label="Deferred Loan Interest Account",
		)
	resolve_account(
		"bank_account",
		facility=facility,
		settings=settings,
		required=True,
		required_label="Bank Account",
	)
	resolve_account(
		"loan_payable_account",
		facility=facility,
		settings=settings,
		required=True,
		required_label="Loan Payable Account",
	)


def build_receipt_je_plan(facility) -> list[dict[str, Any]]:
	"""Finance Excel receipt template — shared by preview and submit."""
	principal, profit = _facility_receipt_amounts(facility)
	_validate_receipt_je_prerequisites(facility, principal=principal, profit=profit)
	settings = get_facility_settings_doc(facility.company)
	ctx = build_template_context(facility)
	tpl = _receipt_row_templates(facility, settings)

	bank_acc = resolve_account(
		"bank_account",
		facility=facility,
		settings=settings,
		required=True,
		required_label="Bank Account",
	)
	loan_acc = resolve_account(
		"loan_payable_account",
		facility=facility,
		settings=settings,
		required=True,
		required_label="Loan Payable Account",
	)
	deferred_acc = resolve_account(
		"deferred_loan_interest_account",
		facility=facility,
		settings=settings,
		required=profit > 0,
		required_label="Deferred Loan Interest Account",
	)

	plan: list[dict[str, Any]] = []

	def add(role, account, amount, debit, tpl_key, label):
		if amount <= 0:
			return
		plan.append(
			{
				"role": role,
				"row_label": label,
				"account": account,
				"amount": amount,
				"debit": debit,
				"dims": receipt_je_row_dimensions(role, facility.name, facility=facility, settings=settings),
				"user_remark": render_facility_template(tpl[tpl_key], ctx),
			}
		)

	add("bank", bank_acc, principal, True, "bank", "Bank")
	if profit > 0:
		add("deferred", deferred_acc, profit, True, "deferred", "Deferred Loan Interest")
	add("loan_principal", loan_acc, principal, False, "loan_principal", "Loan Payable — Principal")
	if profit > 0:
		add("loan_profit", loan_acc, profit, False, "loan_profit", "Loan Payable — Profit")
	_assert_receipt_plan_balanced(plan, principal=principal, profit=profit)
	return plan


def _assert_receipt_plan_balanced(plan: list[dict[str, Any]], *, principal: Decimal, profit: Decimal) -> None:
	total_debit = sum(p["amount"] for p in plan if p["debit"])
	total_credit = sum(p["amount"] for p in plan if not p["debit"])
	expected = principal + profit
	if total_debit != expected or total_credit != expected:
		frappe.throw(
			frappe._("Receipt JE plan is not balanced: debit {0}, credit {1}, expected {2}").format(
				total_debit, total_credit, expected
			)
		)


def _je_preview_payload_from_plan(
	plan: list[dict[str, Any]],
	*,
	voucher_type: str,
	posting_date,
	remarks: str,
) -> dict[str, Any]:
	dim_fn = get_facility_dimension_fieldname()
	rows = []
	total_debit = Decimal("0")
	total_credit = Decimal("0")
	for spec in plan:
		amount = spec["amount"]
		if spec["debit"]:
			total_debit += amount
		else:
			total_credit += amount
		dims = spec.get("dims") or {}
		rows.append(
			{
				"row_label": spec.get("row_label") or "",
				"account": spec["account"],
				"debit": float(amount) if spec["debit"] else 0,
				"credit": float(amount) if not spec["debit"] else 0,
				"user_remark": spec.get("user_remark") or "",
				"facility": dims.get(dim_fn) if dim_fn else None,
				"department": dims.get("department"),
				"cost_center": dims.get("cost_center"),
				"bank_dimension": dims.get("bank_dimension"),
				"bank_account_dimension": dims.get("bank_account_dimension"),
			}
		)
	return {
		"rows": rows,
		"total_debit": float(total_debit),
		"total_credit": float(total_credit),
		"balanced": total_debit == total_credit,
		"voucher_type": voucher_type,
		"posting_date": str(posting_date) if posting_date else "",
		"remarks": remarks or "",
	}


def _active_receipt_journal_entry(facility) -> str | None:
	"""Return receipt JE name only when it exists and is submitted (docstatus=1)."""
	je_name = (getattr(facility, "receipt_journal_entry", None) or "").strip() or None
	if not je_name:
		return None
	ds = frappe.db.get_value("Journal Entry", je_name, "docstatus")
	if ds is None:
		return None
	if cint(ds) == 1:
		return je_name
	return None


def _assert_no_duplicate_receipt(facility) -> None:
	active = _active_receipt_journal_entry(facility)
	if active:
		frappe.throw(
			frappe._("Receipt Journal Entry already exists: {0}").format(active),
			title=frappe._("Facility Receipt"),
		)
	# Stale cancelled/missing pointer must be cleared before creating a replacement
	stale = (getattr(facility, "receipt_journal_entry", None) or "").strip()
	if stale and not active:
		frappe.throw(
			frappe._(
				"Facility still references cancelled or missing Receipt Journal Entry {0}. "
				"Run Facility JE reconciliation (or cancel-path restore) before creating a replacement."
			).format(stale),
			title=frappe._("Facility Receipt"),
		)
	# Conflicting submitted receipt-like JEs (cheque_no = facility)
	from erpnext_extensions.facility_management.facility_je_reconciliation import (
		_candidate_replacement_receipt_jes,
	)

	candidates = _candidate_replacement_receipt_jes(facility.name)
	if len(candidates) > 1:
		frappe.throw(
			frappe._(
				"Multiple submitted receipt Journal Entries found for Facility {0}: {1}. "
				"Manual review required."
			).format(facility.name, ", ".join(candidates)),
			title=frappe._("Facility Receipt"),
		)
	if len(candidates) == 1:
		frappe.throw(
			frappe._(
				"A submitted receipt Journal Entry already exists for Facility {0}: {1}. "
				"Link it via reconciliation instead of creating a duplicate."
			).format(facility.name, candidates[0]),
			title=frappe._("Facility Receipt"),
		)


def preview_receipt_journal_entry(facility) -> dict[str, Any]:
	_assert_no_duplicate_receipt(facility)
	plan = build_receipt_je_plan(facility)
	settings = get_facility_settings_doc(facility.company)
	ctx = build_template_context(facility)
	tpl = _receipt_row_templates(facility, settings)
	posting_date = facility.receive_date or facility.contract_date or frappe.utils.today()
	remark = render_facility_template(tpl["remark"], ctx)
	return _je_preview_payload_from_plan(
		plan,
		voucher_type="Bank Entry",
		posting_date=posting_date,
		remarks=remark,
	)


def create_and_submit_receipt_je(facility) -> str:
	_assert_no_duplicate_receipt(facility)
	principal, profit = _facility_receipt_amounts(facility)
	plan = build_receipt_je_plan(facility)
	settings = get_facility_settings_doc(facility.company)
	ctx = build_template_context(facility)
	tpl = _receipt_row_templates(facility, settings)
	remark = render_facility_template(tpl["remark"], ctx)

	je = frappe.new_doc("Journal Entry")
	je.voucher_type = "Bank Entry"
	je.company = facility.company
	je.posting_date = facility.receive_date or facility.contract_date or frappe.utils.today()
	je.cheque_no = facility.name
	je.cheque_date = je.posting_date
	je.user_remark = remark
	je.remark = remark

	for spec in plan:
		_append_je_row(
			je,
			account=spec["account"],
			amount=spec["amount"],
			debit=spec["debit"],
			row_dims=spec["dims"],
			user_remark=spec.get("user_remark"),
		)

	planned = _planned_receipt_rows(principal, profit)
	je.insert(ignore_permissions=True)
	je.reload()
	_enforce_receipt_je_row_dims_from_plan(je, plan)
	je.submit()
	_apply_exact_je_planned(je.name, planned)
	_apply_exact_gl_planned(je.name, planned)
	_validate_receipt_je_dimensions(je.name, facility)
	return je.name


def _repayment_line_amounts(repayment) -> tuple[Decimal, Decimal, Decimal]:
	return (
		get_exact_currency_decimal("Facility Repayment", repayment.name, "principal_amount"),
		get_exact_currency_decimal("Facility Repayment", repayment.name, "profit_amount"),
		get_exact_currency_decimal("Facility Repayment", repayment.name, "penalty_amount"),
	)


def _repayment_amounts(repayment) -> tuple[Decimal, Decimal, Decimal]:
	if getattr(repayment, "name", None) and frappe.db.exists("Facility Repayment", repayment.name):
		return _repayment_line_amounts(repayment)
	return (
		parse_facility_amount(repayment.get("principal_amount")),
		parse_facility_amount(repayment.get("profit_amount")),
		parse_facility_amount(repayment.get("penalty_amount")),
	)


def _repayment_row_templates(facility, repayment, settings, ctx):
	remark_tpl = (
		repayment.repayment_remarks_template
		if getattr(repayment, "repayment_remarks_template", None)
		else template_chain(
			facility_key="repayment_remarks_template",
			settings_key="default_repayment_remarks_template",
			facility=facility,
			settings=settings,
			default=DEFAULT_REPAYMENT_REMARKS,
		)
	)
	return {
		"remark": remark_tpl,
		"bank": template_chain(
			facility_key="",
			settings_key="default_repayment_bank_row_description_template",
			facility=None,
			settings=settings,
			default=DEFAULT_REPAYMENT_BANK_ROW,
		),
		"principal": template_chain(
			facility_key="",
			settings_key="default_repayment_principal_row_description_template",
			facility=None,
			settings=settings,
			default=DEFAULT_REPAYMENT_PRINCIPAL_ROW,
		),
		"profit": template_chain(
			facility_key="",
			settings_key="default_repayment_profit_row_description_template",
			facility=None,
			settings=settings,
			default=DEFAULT_REPAYMENT_PROFIT_ROW,
		),
		"penalty": template_chain(
			facility_key="",
			settings_key="default_repayment_penalty_row_description_template",
			facility=None,
			settings=settings,
			default=DEFAULT_REPAYMENT_PENALTY_ROW,
		),
	}


def build_repayment_je_plan(repayment, facility=None) -> list[dict[str, Any]]:
	"""Finance Excel repayment template — shared by preview and submit."""
	if facility is None:
		facility = frappe.get_doc("Facility", repayment.facility)
	principal, profit, penalty = _repayment_amounts(repayment)
	settings = get_facility_settings_doc(facility.company)
	from erpnext_extensions.facility_management.facility_debt_purchase import (
		REPAYMENT_METHOD_DEBT_PURCHASE,
		is_debt_purchase_cheque_method,
		normalize_repayment_method,
		resolve_debt_purchase_in_collection_account,
		validate_debt_purchase_cheque_repayment,
	)

	method = normalize_repayment_method(getattr(repayment, "repayment_method", None))
	dp_ctx = None
	if method == REPAYMENT_METHOD_DEBT_PURCHASE:
		dp_ctx = validate_debt_purchase_cheque_repayment(repayment, facility=facility)
	# Shared prerequisites (interest/deferred/cost center). Bank Account is only required for bank method
	# via resolve_account(required=True) below / validate_repayment path for bank.
	validate_repayment_je_prerequisites(
		repayment, facility, settings, principal=principal, profit=profit, penalty=penalty
	)
	ctx = build_template_context(facility, repayment)
	tpl = _repayment_row_templates(facility, repayment, settings, ctx)

	if is_debt_purchase_cheque_method(repayment):
		# Settlement credit = debt_purchase_in_collection (not bank).
		settlement_acc = dp_ctx["dpic_account"]
		settlement_role = "debt_purchase_in_collection"
		settlement_tpl_key = "bank"  # reuse bank row narration template family for settlement credit
		settlement_label = "Debt Purchase In Collection"
		# Cheque mode: credit amount is principal + profit only (penalty forbidden at validate).
		settlement_total = principal + profit
	else:
		settlement_acc = resolve_account(
			"bank_account", repayment=repayment, facility=facility, settings=settings, required=True
		)
		settlement_role = "bank"
		settlement_tpl_key = "bank"
		settlement_label = "Bank"
		settlement_total = principal + profit + penalty

	loan_acc = resolve_account(
		"loan_payable_account",
		repayment=repayment,
		facility=facility,
		settings=settings,
		required=principal > 0 or profit > 0,
	)
	deferred_acc = resolve_account(
		"deferred_loan_interest_account",
		repayment=repayment,
		facility=facility,
		settings=settings,
		required=profit > 0,
	)
	interest_acc = resolve_account(
		"interest_expense_account",
		repayment=repayment,
		facility=facility,
		settings=settings,
		required=profit > 0,
	)
	penalty_acc = resolve_account(
		"penalty_expense_account",
		repayment=repayment,
		facility=facility,
		settings=settings,
		required=penalty > 0,
	)

	plan: list[dict[str, Any]] = []
	pdc_party: dict[str, str] | None = None
	if dp_ctx and dp_ctx.get("pdc") is not None:
		pdc = dp_ctx["pdc"]
		pt = (getattr(pdc, "party_type", None) or "").strip()
		party = (getattr(pdc, "party", None) or "").strip()
		if pt and party:
			pdc_party = {"party_type": pt, "party": party}

	def add(role, account, amount, debit, tpl_key, label):
		if amount <= 0:
			return
		# DPIC settlement uses its own role (bank_dimension + PDC Party); bank stays Party-free.
		dim_role = role if role in ("bank", "debt_purchase_in_collection") else role
		plan.append(
			{
				"role": role,
				"row_label": label,
				"account": account,
				"amount": amount,
				"debit": debit,
				"dims": repayment_je_row_dimensions(
					dim_role,
					facility.name,
					repayment=repayment,
					facility=facility,
					settings=settings,
					pdc_party=pdc_party if role == "debt_purchase_in_collection" else None,
				),
				"user_remark": render_facility_template(tpl[tpl_key], ctx),
			}
		)

	add(settlement_role, settlement_acc, settlement_total, False, settlement_tpl_key, settlement_label)
	add("loan", loan_acc, principal, True, "principal", "Principal — Loan Payable")
	add("loan_profit", loan_acc, profit, True, "profit", "Profit — Loan Payable")
	add("penalty", penalty_acc, penalty, True, "penalty", "Penalty Expense")
	add("deferred_credit", deferred_acc, profit, False, "profit", "Deferred Loan Interest")
	add("interest_expense", interest_acc, profit, True, "profit", "Interest Expense")
	return plan


def preview_repayment_journal_entry(repayment) -> dict[str, Any]:
	facility = frappe.get_doc("Facility", repayment.facility)
	plan = build_repayment_je_plan(repayment, facility=facility)
	ctx = build_template_context(facility, repayment)
	settings = get_facility_settings_doc(facility.company)
	tpl = _repayment_row_templates(facility, repayment, settings, ctx)
	remark_tpl = tpl["remark"]
	remark = render_facility_template(remark_tpl, ctx) if "{" in remark_tpl else remark_tpl
	posting_date = repayment.posting_date or frappe.utils.today()
	return _je_preview_payload_from_plan(
		plan,
		voucher_type="Bank Entry",
		posting_date=posting_date,
		remarks=remark,
	)


def create_and_submit_repayment_je(repayment) -> str:
	facility = frappe.get_doc("Facility", repayment.facility)
	principal, profit, penalty = _repayment_amounts(repayment)
	settings = get_facility_settings_doc(facility.company)
	from erpnext_extensions.facility_management.facility_debt_purchase import (
		is_debt_purchase_cheque_method,
	)

	if not is_debt_purchase_cheque_method(repayment):
		validate_repayment_je_prerequisites(
			repayment, facility, settings, principal=principal, profit=profit, penalty=penalty
		)
	plan = build_repayment_je_plan(repayment, facility=facility)
	ctx = build_template_context(facility, repayment)
	tpl = _repayment_row_templates(facility, repayment, settings, ctx)
	remark_tpl = tpl["remark"]
	remark = render_facility_template(remark_tpl, ctx) if "{" in remark_tpl else remark_tpl

	je = frappe.new_doc("Journal Entry")
	je.voucher_type = "Bank Entry"
	je.company = repayment.company or facility.company
	je.posting_date = repayment.posting_date
	je.cheque_no = repayment.name or facility.name
	je.cheque_date = repayment.posting_date
	je.user_remark = remark
	je.remark = remark

	for spec in plan:
		_append_je_row(
			je,
			account=spec["account"],
			amount=spec["amount"],
			debit=spec["debit"],
			row_dims=spec["dims"],
			user_remark=spec.get("user_remark"),
		)

	# Planned row amounts: for DP cheque, settlement credit is P+profit (penalty=0).
	planned = _planned_repayment_rows(principal, profit, penalty)
	je.insert(ignore_permissions=True)
	je.reload()
	_enforce_je_row_dims_from_plan(je, plan)
	je.submit()
	_apply_exact_je_planned(je.name, planned)
	_apply_exact_gl_planned(je.name, planned)
	_validate_repayment_je_dimensions(je.name, facility, repayment)
	return je.name


def cancel_journal_entry(je_name: str | None) -> None:
	if not je_name or not frappe.db.exists("Journal Entry", je_name):
		return
	je = frappe.get_doc("Journal Entry", je_name)
	if je.docstatus == 1:
		je.cancel()


def refresh_facility_paid_fields(facility_name: str) -> None:
	bal = get_facility_balance_row(facility_name)
	updates = {
		"paid_principal_amount": bal["paid_principal"],
		"paid_profit_amount": bal["paid_profit"],
		"paid_penalty_amount": bal["paid_penalty"],
		"remaining_principal_amount": bal["remaining_principal"],
		"remaining_profit_amount": bal["remaining_profit"],
		"remaining_total_amount": bal["remaining_total"],
	}
	# Only treat submitted receipt JEs as valid for received_amount
	receipt_je = frappe.db.get_value("Facility", facility_name, "receipt_journal_entry")
	if receipt_je:
		ds = frappe.db.get_value("Journal Entry", receipt_je, "docstatus")
		if cint(ds) == 1:
			updates["received_amount"] = flt(bal["principal_amount"])
	frappe.db.set_value("Facility", facility_name, updates, update_modified=False)


def _apply_exact_repayment_je_amounts(
	je_name: str, principal: Decimal, profit: Decimal, penalty: Decimal
) -> None:
	_apply_exact_je_planned(je_name, _planned_repayment_rows(principal, profit, penalty))


def _apply_exact_repayment_gl_amounts(
	je_name: str, principal: Decimal, profit: Decimal, penalty: Decimal
) -> None:
	_apply_exact_gl_planned(je_name, _planned_repayment_rows(principal, profit, penalty))

# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Identify monthly control-account credit sources with line-level attribution."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable

import frappe
from frappe.utils import flt, getdate

from erpnext_extensions.iran_accounting.domain.currency import get_currency_precision
from erpnext_extensions.iran_accounting.fg_cost_allocation.control_credit.accounts import (
	ControlAccountPair,
	control_kind_for_account,
	resolve_control_accounts,
)
from erpnext_extensions.iran_accounting.fg_cost_allocation.period import resolve_persian_month

_PA_REF_RE = re.compile(r"\bPA\s+([0-9a-f]{8,})\b", re.IGNORECASE)


@dataclass
class CreditSourceLine:
	gl_entry: str
	posting_date: str
	account: str
	account_kind: str  # labor | overhead
	voucher_type: str
	voucher_no: str
	cost_center: str
	department: str
	debit: Decimal
	credit: Decimal
	landed_cost_row: str
	landed_cost_amount: Decimal
	eligible_amount: Decimal
	previously_allocated_amount: Decimal
	remaining_amount: Decimal
	eligibility_status: str  # eligible | consumed_by_product_allocation | unresolved | excluded
	exclusion_reason: str
	product_allocation: str
	description: str


@dataclass
class CreditIdentificationResult:
	company: str
	persian_year: int
	persian_month: int
	period_start: str
	period_end: str
	accounts: ControlAccountPair
	sources: list[CreditSourceLine] = field(default_factory=list)
	labor_period_credit: Decimal = Decimal("0")
	overhead_period_credit: Decimal = Decimal("0")
	labor_eligible: Decimal = Decimal("0")
	overhead_eligible: Decimal = Decimal("0")
	labor_consumed_by_pa: Decimal = Decimal("0")
	overhead_consumed_by_pa: Decimal = Decimal("0")
	labor_unresolved: Decimal = Decimal("0")
	overhead_unresolved: Decimal = Decimal("0")
	gl_entry_count: int = 0
	voucher_count: int = 0
	unresolved_count: int = 0


def _money(value, currency: str) -> Decimal:
	precision = get_currency_precision(currency)
	q = Decimal("1") if precision <= 0 else Decimal("1").scaleb(-precision)
	return Decimal(str(value or 0)).quantize(q)


def _extract_pa_name(description: str | None) -> str:
	if not description:
		return ""
	m = _PA_REF_RE.search(description)
	return m.group(1) if m else ""


def _prior_credit_consumption(
	*,
	gl_entry: str,
	landed_cost_row: str,
	exclude_allocation: str | None,
) -> Decimal:
	"""Sum eligible amounts already consumed by other active credit allocations."""
	filters = {
		"parenttype": "MFG Cost Control Credit Allocation",
		"eligibility_status": "eligible",
	}
	# Match by landed_cost_row when present, else gl_entry
	rows = frappe.db.sql(
		"""
		SELECT s.eligible_amount, s.current_allocated_amount, p.name AS parent, p.status, p.docstatus
		FROM `tabMFG Cost Control Credit Source` s
		INNER JOIN `tabMFG Cost Control Credit Allocation` p ON p.name = s.parent
		WHERE p.docstatus < 2
		  AND p.status IN ('Calculated', 'Submitted')
		  AND s.eligibility_status = 'eligible'
		  AND (
		        (%(lc)s != '' AND s.landed_cost_row = %(lc)s)
		     OR (%(lc)s = '' AND s.gl_entry = %(gle)s AND IFNULL(s.landed_cost_row, '') = '')
		  )
		  AND (%(excl)s IS NULL OR p.name != %(excl)s)
		""",
		{"lc": landed_cost_row or "", "gle": gl_entry, "excl": exclude_allocation},
		as_dict=True,
	)
	total = Decimal("0")
	for r in rows:
		total += Decimal(str(flt(r.current_allocated_amount if r.current_allocated_amount is not None else r.eligible_amount)))
	return total


def _classify_landed_line(
	*,
	description: str,
	has_operating_cost: int,
	operation_id: str,
	operating_component: str,
	mca: str,
) -> tuple[str, str, str]:
	"""Return (status, reason, product_allocation)."""
	pa = _extract_pa_name(description)
	if pa and frappe.db.exists("MFG Cost Product Allocation", pa):
		return (
			"consumed_by_product_allocation",
			"Linked to MFG Cost Product Allocation via Landed Cost description PA reference",
			pa,
		)
	if pa:
		# PA token present but document missing — unresolved, do not silent-exclude whole voucher
		return ("unresolved", f"PA reference {pa} not found as Product Allocation", "")

	if cint_true(has_operating_cost) or (operation_id or "").strip() or (operating_component or "").strip():
		return ("eligible", "", "")

	if (mca or "").strip():
		return (
			"excluded",
			"Linked Manufacturing Cost Adjustment additional cost — not credit-control source",
			"",
		)

	return ("unresolved", "Landed Cost line could not be reliably classified", "")


def cint_true(v) -> bool:
	try:
		return int(v or 0) == 1
	except Exception:
		return bool(v)


def identify_control_credits(
	*,
	company: str,
	persian_year: int,
	persian_month: int,
	currency: str | None = None,
	exclude_allocation: str | None = None,
) -> CreditIdentificationResult:
	period = resolve_persian_month(company, int(persian_year), int(persian_month))
	accounts = resolve_control_accounts(company)
	ccy = currency or frappe.get_cached_value("Company", company, "default_currency") or "IRR"
	start = str(period.start_date)
	end = str(period.end_date)

	account_list = [accounts.labor_account, accounts.overhead_account]
	gl_rows = frappe.db.sql(
		"""
		SELECT name, posting_date, account, voucher_type, voucher_no, cost_center,
		       debit, credit, is_cancelled, company
		FROM `tabGL Entry`
		WHERE company = %(company)s
		  AND account IN %(accounts)s
		  AND posting_date BETWEEN %(start)s AND %(end)s
		  AND IFNULL(is_cancelled, 0) = 0
		  AND credit > 0
		ORDER BY posting_date, account, name
		""",
		{"company": company, "accounts": tuple(account_list), "start": start, "end": end},
		as_dict=True,
	)

	# Period credit totals from GL (informational / reconciliation) — credits only
	labor_period = Decimal("0")
	oh_period = Decimal("0")
	for g in gl_rows:
		amt = _money(g.credit, ccy)
		kind = control_kind_for_account(g.account)
		if kind == "labor":
			labor_period += amt
		elif kind == "overhead":
			oh_period += amt

	voucher_nos = sorted({g.voucher_no for g in gl_rows if g.voucher_no})
	landed_by_voucher: dict[str, list[dict]] = {v: [] for v in voucher_nos}
	if voucher_nos:
		lc_rows = frappe.db.sql(
			"""
			SELECT name, parent, expense_account, description, amount, base_amount,
			       cost_center, department, has_operating_cost, operation_id,
			       operating_component, custom_manufacturing_cost_adjustment
			FROM `tabLanded Cost Taxes and Charges`
			WHERE parenttype = 'Stock Entry'
			  AND parent IN %(parents)s
			  AND expense_account IN %(accounts)s
			ORDER BY parent, idx, name
			""",
			{"parents": tuple(voucher_nos), "accounts": tuple(account_list)},
			as_dict=True,
		)
		for r in lc_rows:
			landed_by_voucher.setdefault(r.parent, []).append(r)

	sources: list[CreditSourceLine] = []
	for g in gl_rows:
		kind = control_kind_for_account(g.account)
		if kind not in ("labor", "overhead"):
			sources.append(
				CreditSourceLine(
					gl_entry=g.name,
					posting_date=str(g.posting_date),
					account=g.account,
					account_kind="unresolved",
					voucher_type=g.voucher_type or "",
					voucher_no=g.voucher_no or "",
					cost_center=g.cost_center or "",
					department="",
					debit=_money(g.debit, ccy),
					credit=_money(g.credit, ccy),
					landed_cost_row="",
					landed_cost_amount=Decimal("0"),
					eligible_amount=Decimal("0"),
					previously_allocated_amount=Decimal("0"),
					remaining_amount=Decimal("0"),
					eligibility_status="unresolved",
					exclusion_reason="Account is not classified as labor or overhead control",
					product_allocation="",
					description="",
				)
			)
			continue

		lines = [
			r
			for r in landed_by_voucher.get(g.voucher_no or "", [])
			if r.expense_account == g.account
		]
		gl_credit = _money(g.credit, ccy)

		if not lines:
			# No line-level attribution possible
			sources.append(
				CreditSourceLine(
					gl_entry=g.name,
					posting_date=str(g.posting_date),
					account=g.account,
					account_kind=kind,
					voucher_type=g.voucher_type or "",
					voucher_no=g.voucher_no or "",
					cost_center=g.cost_center or "",
					department="",
					debit=_money(g.debit, ccy),
					credit=gl_credit,
					landed_cost_row="",
					landed_cost_amount=Decimal("0"),
					eligible_amount=Decimal("0"),
					previously_allocated_amount=Decimal("0"),
					remaining_amount=Decimal("0"),
					eligibility_status="unresolved",
					exclusion_reason="No Landed Cost Taxes and Charges line matched this GL credit",
					product_allocation="",
					description="",
				)
			)
			continue

		line_sum = _money(sum(flt(r.base_amount or r.amount) for r in lines), ccy)
		# Attribute each landed line independently (do not drop whole voucher)
		for r in lines:
			line_amt = _money(r.base_amount if r.base_amount is not None else r.amount, ccy)
			status, reason, pa = _classify_landed_line(
				description=r.description or "",
				has_operating_cost=r.has_operating_cost,
				operation_id=r.operation_id or "",
				operating_component=r.operating_component or "",
				mca=r.custom_manufacturing_cost_adjustment or "",
			)
			eligible = line_amt if status == "eligible" else Decimal("0")
			prev = Decimal("0")
			if status == "eligible":
				prev = _money(
					_prior_credit_consumption(
						gl_entry=g.name,
						landed_cost_row=r.name,
						exclude_allocation=exclude_allocation,
					),
					ccy,
				)
			remaining = _money(eligible - prev, ccy) if status == "eligible" else Decimal("0")
			if status == "eligible" and remaining < 0:
				# Negative remaining is a reconciliation exception — keep visible
				pass

			sources.append(
				CreditSourceLine(
					gl_entry=g.name,
					posting_date=str(g.posting_date),
					account=g.account,
					account_kind=kind,
					voucher_type=g.voucher_type or "",
					voucher_no=g.voucher_no or "",
					cost_center=(r.cost_center or g.cost_center or ""),
					department=r.department or "",
					debit=_money(g.debit, ccy),
					credit=gl_credit,
					landed_cost_row=r.name,
					landed_cost_amount=line_amt,
					eligible_amount=eligible,
					previously_allocated_amount=prev,
					remaining_amount=remaining,
					eligibility_status=status,
					exclusion_reason=reason,
					product_allocation=pa,
					description=(r.description or "")[:140],
				)
			)

		# If landed lines do not reconcile to GL credit, flag residual as unresolved
		if line_sum != gl_credit:
			diff = _money(gl_credit - line_sum, ccy)
			if diff != 0:
				sources.append(
					CreditSourceLine(
						gl_entry=g.name,
						posting_date=str(g.posting_date),
						account=g.account,
						account_kind=kind,
						voucher_type=g.voucher_type or "",
						voucher_no=g.voucher_no or "",
						cost_center=g.cost_center or "",
						department="",
						debit=_money(g.debit, ccy),
						credit=gl_credit,
						landed_cost_row="",
						landed_cost_amount=diff,
						eligible_amount=Decimal("0"),
						previously_allocated_amount=Decimal("0"),
						remaining_amount=Decimal("0"),
						eligibility_status="unresolved",
						exclusion_reason=(
							f"GL credit {gl_credit} differs from landed-cost line sum {line_sum}"
						),
						product_allocation="",
						description="",
					)
				)

	result = CreditIdentificationResult(
		company=company,
		persian_year=int(persian_year),
		persian_month=int(persian_month),
		period_start=start,
		period_end=end,
		accounts=accounts,
		sources=sources,
		labor_period_credit=labor_period,
		overhead_period_credit=oh_period,
		gl_entry_count=len(gl_rows),
		voucher_count=len(voucher_nos),
	)
	for s in sources:
		if s.account_kind == "labor":
			if s.eligibility_status == "eligible":
				result.labor_eligible += s.remaining_amount
			elif s.eligibility_status == "consumed_by_product_allocation":
				result.labor_consumed_by_pa += s.landed_cost_amount
			elif s.eligibility_status == "unresolved":
				result.labor_unresolved += abs(s.landed_cost_amount or s.credit)
				result.unresolved_count += 1
		elif s.account_kind == "overhead":
			if s.eligibility_status == "eligible":
				result.overhead_eligible += s.remaining_amount
			elif s.eligibility_status == "consumed_by_product_allocation":
				result.overhead_consumed_by_pa += s.landed_cost_amount
			elif s.eligibility_status == "unresolved":
				result.overhead_unresolved += abs(s.landed_cost_amount or s.credit)
				result.unresolved_count += 1
	return result

# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""GL extraction for configured Account × Cost Center × Department rules."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.fg_cost_allocation.period import ResolvedPeriod


@dataclass
class RuleSpec:
	name: str
	account: str
	cost_center: str
	department: str
	enabled: int = 1
	description: str = ""


@dataclass
class GLLineSnapshot:
	gl_entry: str
	voucher_type: str
	voucher_no: str
	posting_date: Any
	account: str
	cost_center: str
	department: str
	debit: Decimal
	credit: Decimal
	net_amount: Decimal
	remarks: str = ""


@dataclass
class ExpensePoolSnapshot:
	settings_rule: str
	account: str
	cost_center: str
	department: str
	monthly_debit: Decimal = Decimal("0")
	monthly_credit: Decimal = Decimal("0")
	monthly_net_expense: Decimal = Decimal("0")
	gl_entry_count: int = 0
	lines: list[GLLineSnapshot] = field(default_factory=list)


def load_enabled_rules(settings_name: str) -> list[RuleSpec]:
	rows = frappe.get_all(
		"Expense Account Dimension Rule",
		filters={"parent": settings_name, "enabled": 1},
		fields=["name", "account", "cost_center", "department", "enabled", "description"],
		order_by="idx asc",
	)
	return [
		RuleSpec(
			name=r.name,
			account=r.account,
			cost_center=r.cost_center or "",
			department=r.department or "",
			enabled=1,
			description=r.description or "",
		)
		for r in rows
		if r.account
	]


def _blank(value) -> str:
	return (value or "").strip()


def extract_pools_for_rules(
	company: str, period: ResolvedPeriod, rules: list[RuleSpec]
) -> tuple[list[ExpensePoolSnapshot], Decimal]:
	"""Return (pools, excluded_gl_amount for accounts appearing in rules)."""
	pools: list[ExpensePoolSnapshot] = []
	accounts = sorted({r.account for r in rules})

	for rule in rules:
		lines = _fetch_gl_lines(
			company=company,
			account=rule.account,
			cost_center=rule.cost_center,
			department=rule.department,
			start_date=period.start_date,
			end_date=period.end_date,
		)
		pool = ExpensePoolSnapshot(
			settings_rule=rule.name,
			account=rule.account,
			cost_center=rule.cost_center,
			department=rule.department,
			lines=lines,
			gl_entry_count=len(lines),
		)
		for line in lines:
			pool.monthly_debit += line.debit
			pool.monthly_credit += line.credit
			pool.monthly_net_expense += line.net_amount
		pools.append(pool)

	excluded = _compute_excluded_amount(company, period, accounts, rules)
	return pools, excluded


def _fetch_gl_lines(
	*,
	company: str,
	account: str,
	cost_center: str,
	department: str,
	start_date,
	end_date,
) -> list[GLLineSnapshot]:
	"""Exact dimension match: empty rule field matches blank GL dimension only."""
	rows = frappe.db.sql(
		"""
		SELECT
			g.name AS gl_entry,
			g.voucher_type,
			g.voucher_no,
			g.posting_date,
			g.account,
			IFNULL(g.cost_center, '') AS cost_center,
			IFNULL(g.department, '') AS department,
			g.debit,
			g.credit,
			IFNULL(g.remarks, '') AS remarks
		FROM `tabGL Entry` g
		WHERE g.company = %(company)s
		  AND g.account = %(account)s
		  AND g.posting_date BETWEEN %(start)s AND %(end)s
		  AND IFNULL(g.is_cancelled, 0) = 0
		  AND IFNULL(g.is_opening, 'No') != 'Yes'
		  AND IFNULL(g.cost_center, '') = %(cost_center)s
		  AND IFNULL(g.department, '') = %(department)s
		ORDER BY g.posting_date, g.creation, g.name
		""",
		{
			"company": company,
			"account": account,
			"start": str(start_date),
			"end": str(end_date),
			"cost_center": _blank(cost_center),
			"department": _blank(department),
		},
		as_dict=True,
	)
	out: list[GLLineSnapshot] = []
	for r in rows:
		debit = Decimal(str(flt(r.debit)))
		credit = Decimal(str(flt(r.credit)))
		out.append(
			GLLineSnapshot(
				gl_entry=r.gl_entry,
				voucher_type=r.voucher_type or "",
				voucher_no=r.voucher_no or "",
				posting_date=r.posting_date,
				account=r.account,
				cost_center=_blank(r.cost_center),
				department=_blank(r.department),
				debit=debit,
				credit=credit,
				net_amount=debit - credit,
				remarks=r.remarks or "",
			)
		)
	return out


def _compute_excluded_amount(
	company: str, period: ResolvedPeriod, accounts: list[str], rules: list[RuleSpec]
) -> Decimal:
	"""GL net for configured accounts that did not match any enabled rule."""
	if not accounts:
		return Decimal("0")

	total = frappe.db.sql(
		"""
		SELECT COALESCE(SUM(g.debit - g.credit), 0)
		FROM `tabGL Entry` g
		WHERE g.company = %(company)s
		  AND g.account IN %(accounts)s
		  AND g.posting_date BETWEEN %(start)s AND %(end)s
		  AND IFNULL(g.is_cancelled, 0) = 0
		  AND IFNULL(g.is_opening, 'No') != 'Yes'
		""",
		{
			"company": company,
			"accounts": tuple(accounts),
			"start": str(period.start_date),
			"end": str(period.end_date),
		},
	)[0][0]
	matched = Decimal("0")
	for rule in rules:
		rows = frappe.db.sql(
			"""
			SELECT COALESCE(SUM(g.debit - g.credit), 0)
			FROM `tabGL Entry` g
			WHERE g.company = %(company)s
			  AND g.account = %(account)s
			  AND g.posting_date BETWEEN %(start)s AND %(end)s
			  AND IFNULL(g.is_cancelled, 0) = 0
			  AND IFNULL(g.is_opening, 'No') != 'Yes'
			  AND IFNULL(g.cost_center, '') = %(cost_center)s
			  AND IFNULL(g.department, '') = %(department)s
			""",
			{
				"company": company,
				"account": rule.account,
				"start": str(period.start_date),
				"end": str(period.end_date),
				"cost_center": _blank(rule.cost_center),
				"department": _blank(rule.department),
			},
		)[0][0]
		matched += Decimal(str(flt(rows)))
	return Decimal(str(flt(total))) - matched

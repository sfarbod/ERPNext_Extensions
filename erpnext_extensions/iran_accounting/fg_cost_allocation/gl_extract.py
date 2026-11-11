# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""GL extraction for configured Account × Cost Center × Department rules."""

from __future__ import annotations

from collections import defaultdict
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


def _key(account, cost_center, department) -> tuple[str, str, str]:
	return (_blank(account), _blank(cost_center), _blank(department))


def extract_pools_for_rules(
	company: str, period: ResolvedPeriod, rules: list[RuleSpec]
) -> tuple[list[ExpensePoolSnapshot], Decimal]:
	"""Bulk-fetch GL once, then assign lines to exact rule keys."""
	if not rules:
		return [], Decimal("0")

	accounts = sorted({r.account for r in rules})
	rule_by_key: dict[tuple[str, str, str], list[RuleSpec]] = defaultdict(list)
	for rule in rules:
		rule_by_key[_key(rule.account, rule.cost_center, rule.department)].append(rule)

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
		  AND g.account IN %(accounts)s
		  AND g.posting_date BETWEEN %(start)s AND %(end)s
		  AND IFNULL(g.is_cancelled, 0) = 0
		  AND IFNULL(g.is_opening, 'No') != 'Yes'
		ORDER BY g.posting_date, g.creation, g.name
		""",
		{
			"company": company,
			"accounts": tuple(accounts),
			"start": str(period.start_date),
			"end": str(period.end_date),
		},
		as_dict=True,
	)

	lines_by_key: dict[tuple[str, str, str], list[GLLineSnapshot]] = defaultdict(list)
	total_account_net = Decimal("0")
	matched_net = Decimal("0")

	for r in rows:
		debit = Decimal(str(flt(r.debit)))
		credit = Decimal(str(flt(r.credit)))
		net = debit - credit
		total_account_net += net
		key = _key(r.account, r.cost_center, r.department)
		snap = GLLineSnapshot(
			gl_entry=r.gl_entry,
			voucher_type=r.voucher_type or "",
			voucher_no=r.voucher_no or "",
			posting_date=r.posting_date,
			account=r.account,
			cost_center=_blank(r.cost_center),
			department=_blank(r.department),
			debit=debit,
			credit=credit,
			net_amount=net,
			remarks=r.remarks or "",
		)
		if key in rule_by_key:
			lines_by_key[key].append(snap)
			matched_net += net

	pools: list[ExpensePoolSnapshot] = []
	for rule in rules:
		key = _key(rule.account, rule.cost_center, rule.department)
		lines = lines_by_key.get(key, [])
		# Persist only pools with matched GL activity; zero-activity rules stay configured
		# but do not explode detail cardinality.
		if not lines:
			continue
		pool = ExpensePoolSnapshot(
			settings_rule=rule.name,
			account=rule.account,
			cost_center=rule.cost_center,
			department=rule.department,
			lines=list(lines),
			gl_entry_count=len(lines),
		)
		for line in lines:
			pool.monthly_debit += line.debit
			pool.monthly_credit += line.credit
			pool.monthly_net_expense += line.net_amount
		pools.append(pool)

	excluded = total_account_net - matched_net
	return pools, excluded

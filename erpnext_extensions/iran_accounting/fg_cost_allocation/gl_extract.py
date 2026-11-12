# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""GL extraction for configured Account × (optional Cost Center) × (optional Department) rules.

Matching semantics (critical):
  - Account is always mandatory and must match exactly.
  - Blank Department on a rule means DO NOT FILTER on Department (any GL Department matches).
  - Blank Cost Center on a rule means DO NOT FILTER on Cost Center (any GL Cost Center matches).
  - Blank is NOT an exact-empty match against GL Entry dimensions.

Overlap policy:
  - Active rules whose match-sets can intersect are overlapping.
  - A GL Entry that matches multiple enabled rules is NOT allocated to any pool
    (no silent double-counting). Conflicts are reported; calculation must not be consumable.
"""

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


@dataclass
class ExtractResult:
	pools: list[ExpensePoolSnapshot]
	excluded_net: Decimal
	matched_net: Decimal
	total_account_net: Decimal
	overlapping_rule_pairs: list[dict] = field(default_factory=list)
	overlapping_gl_entries: list[dict] = field(default_factory=list)
	has_overlap_conflicts: bool = False


def _norm(value) -> str:
	return (value or "").strip()


def dimension_filter_matches(rule_value: str | None, gl_value: str | None) -> bool:
	"""Optional dimension filter: blank rule value → no filter; else exact match on GL value."""
	rule_dim = _norm(rule_value)
	if not rule_dim:
		return True
	return rule_dim == _norm(gl_value)


def rule_matches_gl(
	rule: RuleSpec,
	*,
	account: str,
	cost_center: str | None = "",
	department: str | None = "",
) -> bool:
	"""Return True when GL dimensions satisfy the rule's optional filters."""
	if not rule.account or _norm(rule.account) != _norm(account):
		return False
	if not dimension_filter_matches(rule.department, department):
		return False
	if not dimension_filter_matches(rule.cost_center, cost_center):
		return False
	return True


def rules_can_overlap(a: RuleSpec, b: RuleSpec) -> bool:
	"""True when some concrete GL Entry could match both rules.

	Same Account required. On each optional dimension, either values are equal
	or at least one side is blank (unrestricted).
	"""
	if _norm(a.account) != _norm(b.account):
		return False
	a_cc, b_cc = _norm(a.cost_center), _norm(b.cost_center)
	a_dept, b_dept = _norm(a.department), _norm(b.department)
	cc_ok = (not a_cc) or (not b_cc) or a_cc == b_cc
	dept_ok = (not a_dept) or (not b_dept) or a_dept == b_dept
	# Identical configuration is a duplicate, also treated as overlap/conflict.
	return cc_ok and dept_ok


def detect_overlapping_rules(rules: list[RuleSpec]) -> list[dict]:
	"""Overlap detection among enabled rules (deterministic).

	Groups by Account first so Account×Department grids (thousands of rows) stay O(n)
	per account rather than O(N²) across the full rule set.
	"""
	enabled = [r for r in rules if cint_enabled(r)]
	by_account: dict[str, list[RuleSpec]] = defaultdict(list)
	for r in enabled:
		by_account[_norm(r.account)].append(r)

	pairs: list[dict] = []
	for account, group in by_account.items():
		for i, a in enumerate(group):
			for b in group[i + 1 :]:
				if rules_can_overlap(a, b):
					pairs.append(
						{
							"rule_a": a.name,
							"rule_b": b.name,
							"account": a.account,
							"rule_a_cost_center": a.cost_center or "",
							"rule_a_department": a.department or "",
							"rule_b_cost_center": b.cost_center or "",
							"rule_b_department": b.department or "",
							"reason": "Match sets intersect under optional-dimension semantics",
						}
					)
	return pairs


def cint_enabled(rule: RuleSpec) -> bool:
	return bool(int(rule.enabled or 0))


def assign_gl_lines_to_rules(
	rules: list[RuleSpec], lines: list[GLLineSnapshot]
) -> tuple[dict[str, list[GLLineSnapshot]], list[dict], Decimal, Decimal]:
	"""Assign each GL line to at most one rule.

	Returns:
	  lines_by_rule_name, overlapping_gl_entries, matched_net, unmatched_net
	"""
	enabled = [r for r in rules if cint_enabled(r) and r.account]
	lines_by_rule: dict[str, list[GLLineSnapshot]] = defaultdict(list)
	overlaps: list[dict] = []
	matched_net = Decimal("0")
	unmatched_net = Decimal("0")

	for line in lines:
		hits = [
			r
			for r in enabled
			if rule_matches_gl(
				r,
				account=line.account,
				cost_center=line.cost_center,
				department=line.department,
			)
		]
		if not hits:
			unmatched_net += line.net_amount
			continue
		if len(hits) == 1:
			lines_by_rule[hits[0].name].append(line)
			matched_net += line.net_amount
			continue
		# Ambiguous: do not allocate to any pool (prevent duplicate amounts).
		overlaps.append(
			{
				"gl_entry": line.gl_entry,
				"account": line.account,
				"cost_center": line.cost_center,
				"department": line.department,
				"net_amount": str(line.net_amount),
				"matching_rules": [
					{
						"name": r.name,
						"cost_center": r.cost_center or "",
						"department": r.department or "",
					}
					for r in hits
				],
				"reason": "GL Entry matches multiple overlapping rules; amount withheld from all pools",
			}
		)
		unmatched_net += line.net_amount

	return lines_by_rule, overlaps, matched_net, unmatched_net


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


def extract_pools_for_rules(
	company: str, period: ResolvedPeriod, rules: list[RuleSpec]
) -> ExtractResult:
	"""Bulk-fetch GL once, then assign lines using optional dimension filters."""
	if not rules:
		return ExtractResult(
			pools=[],
			excluded_net=Decimal("0"),
			matched_net=Decimal("0"),
			total_account_net=Decimal("0"),
		)

	overlap_pairs = detect_overlapping_rules(rules)
	accounts = sorted({r.account for r in rules if r.account})
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

	snapshots: list[GLLineSnapshot] = []
	total_account_net = Decimal("0")
	for r in rows:
		debit = Decimal(str(flt(r.debit)))
		credit = Decimal(str(flt(r.credit)))
		net = debit - credit
		total_account_net += net
		snapshots.append(
			GLLineSnapshot(
				gl_entry=r.gl_entry,
				voucher_type=r.voucher_type or "",
				voucher_no=r.voucher_no or "",
				posting_date=r.posting_date,
				account=r.account,
				cost_center=_norm(r.cost_center),
				department=_norm(r.department),
				debit=debit,
				credit=credit,
				net_amount=net,
				remarks=r.remarks or "",
			)
		)

	lines_by_rule, overlapping_gl, matched_net, unmatched_net = assign_gl_lines_to_rules(
		rules, snapshots
	)

	pools: list[ExpensePoolSnapshot] = []
	for rule in rules:
		if not cint_enabled(rule):
			continue
		lines = lines_by_rule.get(rule.name, [])
		if not lines:
			continue
		pool = ExpensePoolSnapshot(
			settings_rule=rule.name,
			account=rule.account,
			cost_center=rule.cost_center or "",
			department=rule.department or "",
			lines=list(lines),
			gl_entry_count=len(lines),
		)
		for line in lines:
			pool.monthly_debit += line.debit
			pool.monthly_credit += line.credit
			pool.monthly_net_expense += line.net_amount
		pools.append(pool)

	has_conflicts = bool(overlap_pairs) or bool(overlapping_gl)
	return ExtractResult(
		pools=pools,
		excluded_net=unmatched_net,
		matched_net=matched_net,
		total_account_net=total_account_net,
		overlapping_rule_pairs=overlap_pairs,
		overlapping_gl_entries=overlapping_gl,
		has_overlap_conflicts=has_conflicts,
	)

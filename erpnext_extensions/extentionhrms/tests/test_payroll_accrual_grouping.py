# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Unit tests for the framework-free payroll accrual grouping logic.

These tests import **no Frappe** and run under plain ``pytest``::

    pytest erpnext_extensions/extentionhrms/tests/test_payroll_accrual_grouping.py

The fixtures use fictional accounts/components/departments but reproduce the
data *shapes* the module must handle: a zero-decimal currency, employer-
contribution "expense" earnings, deductions mapped both to liability payables
*and* to expense accounts (a deduction sharing an earning's expense account, and
a penalty expense account), per-employee cost-centre percentage splits, and a
P&L round-off account.
"""

from __future__ import annotations

import random
from decimal import Decimal

import pytest

from erpnext_extensions.extentionhrms.payroll_accrual_grouping import (
	AccrualConfig,
	ComponentAmount,
	CostCenterSplit,
	SalarySlip,
	build_accrual_journal_accounts,
	split_amount_by_cost_centers,
)

# ---------------------------------------------------------------------------
# Fixtures (fictional identifiers; structure mirrors a real payroll)
# ---------------------------------------------------------------------------

# Accounts
SALARY_EXP = "EXP-SALARY"  # base salary / overtime (and shared by a deduction)
EMPLOYER_INS_EXP = "EXP-EMPLOYER-INS"  # employer contribution booked as expense
PENALTY_EXP = "EXP-PENALTY"  # absence penalty (deduction -> expense)
TAX_PAYABLE = "LIA-TAX"
INS_PAYABLE = "LIA-INS"
PAYROLL_PAYABLE = "LIA-PAYABLE"
ADVANCE = "AST-ADVANCE"
LOAN = "AST-LOAN"
ROUND_OFF = "EXP-ROUNDOFF"  # round-off account is P&L!

# Salary component -> GL account
COMPONENT_ACCOUNTS = {
	# earnings (Expense)
	"Basic Salary": SALARY_EXP,
	"Overtime": SALARY_EXP,
	"Employer Insurance (Expense)": EMPLOYER_INS_EXP,
	# deductions -> Liability payables
	"Income Tax": TAX_PAYABLE,
	"Employee Insurance": INS_PAYABLE,
	# deductions -> Expense accounts (the ones the old script silently dropped)
	"Time Deduction": SALARY_EXP,  # shares the base-salary expense account
	"Absence": PENALTY_EXP,
	# deductions -> Asset (advances/loans)
	"Advance": ADVANCE,
	"Loan": LOAN,
}

ACCOUNT_ROOT_TYPE = {
	SALARY_EXP: "Expense",
	EMPLOYER_INS_EXP: "Expense",
	PENALTY_EXP: "Expense",
	TAX_PAYABLE: "Liability",
	INS_PAYABLE: "Liability",
	PAYROLL_PAYABLE: "Liability",
	ADVANCE: "Asset",
	LOAN: "Asset",
	ROUND_OFF: "Expense",
}

ROUND_OFF_DEPARTMENT = "Adjustments"
ROUND_OFF_COST_CENTER = "CC-ADMIN"


def make_config(precision: int = 0, per_employee=(), account_parties=None) -> AccrualConfig:
	return AccrualConfig(
		component_accounts=dict(COMPONENT_ACCOUNTS),
		account_root_type=dict(ACCOUNT_ROOT_TYPE),
		payable_account=PAYROLL_PAYABLE,
		round_off_account=ROUND_OFF,
		round_off_cost_center=ROUND_OFF_COST_CENTER,
		round_off_department=ROUND_OFF_DEPARTMENT,
		per_employee_components=frozenset(per_employee),
		account_parties=dict(account_parties or {}),
		precision=precision,
	)


def slip(
	name,
	department,
	splits,
	earnings,
	deductions=(),
	employee=None,
):
	return SalarySlip.make(
		name=name,
		employee=employee or name,
		department=department,
		cost_center_splits=[CostCenterSplit.make(cc, pct) for cc, pct in splits],
		earnings=[ComponentAmount.make(c, a) for c, a in earnings],
		deductions=[ComponentAmount.make(c, a) for c, a in deductions],
	)


# ---------------------------------------------------------------------------
# Balance / reconciliation
# ---------------------------------------------------------------------------


def test_single_cost_center_is_exact_and_balanced():
	config = make_config()
	slips = [
		slip(
			"SS-1",
			"Finance",
			splits=[("CC-ADMIN", 100)],
			earnings=[("Basic Salary", 10_000_000), ("Overtime", 2_500_000)],
			deductions=[("Income Tax", 1_200_000), ("Employee Insurance", 875_000)],
		)
	]
	result = build_accrual_journal_accounts(slips, config)

	assert result.is_balanced()
	# no rounding needed with a single 100% split
	assert result.round_off_amount == Decimal(0)
	assert not result.rows_for(ROUND_OFF)
	# expense debit = gross earnings
	debit_salary = sum(r.debit for r in result.rows_for(SALARY_EXP))
	assert debit_salary == Decimal(12_500_000)
	# payable credit = net (earnings - deductions)
	payable = sum(r.credit for r in result.rows_for(PAYROLL_PAYABLE))
	assert payable == Decimal(12_500_000 - 1_200_000 - 875_000)


# A 3-way 33.33% split where the earnings side and the payable side round in
# different directions leaves a residue. Values are small on purpose to isolate
# the rounding behaviour; the magnitude is irrelevant to the mechanism.
THREE_WAY = [("CC-P1", Decimal("33.34")), ("CC-P2", Decimal("33.33")), ("CC-P3", Decimal("33.33"))]


def test_debits_equal_credits_after_round_off_three_way_split():
	"""A 3-way split whose earnings/payable rounding diverges forces a residual
	that must land on the round-off account, and the entry must still balance."""
	config = make_config()
	slips = [
		slip(
			"SS-2",
			"Production",
			splits=THREE_WAY,
			earnings=[("Basic Salary", 5)],
			deductions=[("Income Tax", 1)],
		)
	]
	result = build_accrual_journal_accounts(slips, config)

	# a residual actually occurred and was pushed to the round-off account
	assert result.round_off_amount != Decimal(0)
	assert result.rows_for(ROUND_OFF)
	# and, with the round-off line, the entry balances exactly
	assert result.is_balanced()
	# the round-off line equals the pre-round-off imbalance
	non_round = [r for r in result.rows if r.account != ROUND_OFF]
	pre_debit = sum(r.debit for r in non_round)
	pre_credit = sum(r.credit for r in non_round)
	assert result.round_off_amount == pre_debit - pre_credit


def test_round_off_line_carries_cost_center_and_department():
	config = make_config()
	slips = [
		slip(
			"SS-3",
			"Management",
			splits=THREE_WAY,
			earnings=[("Basic Salary", 5)],
			deductions=[("Income Tax", 1)],
		)
	]
	result = build_accrual_journal_accounts(slips, config)
	assert result.is_balanced()
	round_rows = result.rows_for(ROUND_OFF)
	assert len(round_rows) == 1
	assert round_rows[0].cost_center == ROUND_OFF_COST_CENTER
	assert round_rows[0].department == ROUND_OFF_DEPARTMENT
	# the round-off line is on exactly one side
	assert bool(round_rows[0].debit) != bool(round_rows[0].credit)


# ---------------------------------------------------------------------------
# Multi-cost-center fractional splits
# ---------------------------------------------------------------------------


def test_sixty_forty_split_allocates_proportionally():
	config = make_config()
	slips = [
		slip(
			"SS-4",
			"Security",
			splits=[("CC-PROD", 60), ("CC-ADMIN", 40)],
			earnings=[("Basic Salary", 10_000_000)],
		)
	]
	result = build_accrual_journal_accounts(slips, config)

	by_cc = {r.cost_center: r.debit for r in result.rows_for(SALARY_EXP)}
	assert by_cc["CC-PROD"] == Decimal(6_000_000)
	assert by_cc["CC-ADMIN"] == Decimal(4_000_000)
	assert result.is_balanced()


def test_split_helper_matches_hrms_naive_rounding():
	splits = [CostCenterSplit.make("A", 60), CostCenterSplit.make("B", 40)]
	parts = dict(split_amount_by_cost_centers(101, splits, precision=0))
	# 101*0.6 = 60.6 -> 61 ; 101*0.4 = 40.4 -> 40
	assert parts == {"A": Decimal(61), "B": Decimal(40)}


# ---------------------------------------------------------------------------
# Deductions mapped to Expense accounts must NOT be lost
# ---------------------------------------------------------------------------


def test_deduction_mapped_to_expense_is_kept_as_credit_with_department():
	config = make_config()
	dept = "Maintenance"
	slips = [
		slip(
			"SS-5",
			dept,
			splits=[("CC-PROD", 100)],
			earnings=[("Basic Salary", 8_000_000)],
			deductions=[("Time Deduction", 500_000), ("Absence", 300_000)],
		)
	]
	result = build_accrual_journal_accounts(slips, config)

	# credit rows on the expense accounts exist (the old script deleted these)
	salary_rows = result.rows_for(SALARY_EXP)
	credit_on_salary = sum(r.credit for r in salary_rows)
	assert credit_on_salary == Decimal(500_000)

	penalty = result.rows_for(PENALTY_EXP)
	assert len(penalty) == 1
	assert penalty[0].credit == Decimal(300_000)
	# and both P&L credit rows carry the slip department
	assert penalty[0].department == dept
	assert all(r.department == dept for r in salary_rows)

	assert result.is_balanced()


# ---------------------------------------------------------------------------
# Every P&L row carries a Department
# ---------------------------------------------------------------------------


def test_all_pl_rows_have_department_and_bs_rows_do_not():
	config = make_config()
	slips = [
		slip(
			"SS-6",
			"Finance",
			splits=[("CC-ADMIN", 100)],
			earnings=[
				("Basic Salary", 9_000_000),
				("Employer Insurance (Expense)", 1_800_000),
			],
			deductions=[
				("Income Tax", 700_000),
				("Employee Insurance", 630_000),
				("Time Deduction", 200_000),
				("Advance", 1_000_000),
			],
		)
	]
	result = build_accrual_journal_accounts(slips, config)

	assert result.pl_rows_missing_department(config) == []
	# Balance-Sheet rows (payable/liability/asset) must NOT get a department
	for r in result.rows:
		if not config.is_pl_account(r.account):
			assert r.department is None
	assert result.is_balanced()


def test_same_cost_center_different_departments_produce_distinct_rows():
	"""A shared cost centre used by several departments must NOT collapse rows."""
	config = make_config()
	slips = [
		slip("SS-A", "Micro Lab", [("CC-PROD", 100)], [("Basic Salary", 5_000_000)]),
		slip("SS-B", "Warehouse", [("CC-PROD", 100)], [("Basic Salary", 4_000_000)]),
	]
	result = build_accrual_journal_accounts(slips, config)

	rows = result.rows_for(SALARY_EXP)
	depts = {r.department for r in rows}
	assert depts == {"Micro Lab", "Warehouse"}
	assert len(rows) == 2  # not merged into one aggregate row
	assert result.is_balanced()


# ---------------------------------------------------------------------------
# Aggregation across many slips
# ---------------------------------------------------------------------------


def test_aggregates_same_account_cost_center_department():
	config = make_config()
	dept = "Admin"
	slips = [
		slip("SS-C", dept, [("CC-ADMIN", 100)], [("Basic Salary", 3_000_000)]),
		slip("SS-D", dept, [("CC-ADMIN", 100)], [("Basic Salary", 2_000_000)]),
	]
	result = build_accrual_journal_accounts(slips, config)
	rows = result.rows_for(SALARY_EXP)
	assert len(rows) == 1  # merged: same (account, cc, dept)
	assert rows[0].debit == Decimal(5_000_000)


# ---------------------------------------------------------------------------
# "Process Based on Employee" — loans / advances bypass the group-by
# ---------------------------------------------------------------------------


def _loan_slips():
	# two employees in the SAME department + cost centre, each with a Loan
	return [
		slip(
			"SS-L1",
			"Finance",
			[("CC-ADMIN", 100)],
			earnings=[("Basic Salary", 5_000_000)],
			deductions=[("Loan", 1_000_000), ("Income Tax", 400_000)],
			employee="EMP-1",
		),
		slip(
			"SS-L2",
			"Finance",
			[("CC-ADMIN", 100)],
			earnings=[("Basic Salary", 6_000_000)],
			deductions=[("Loan", 1_500_000), ("Income Tax", 500_000)],
			employee="EMP-2",
		),
	]


def test_per_employee_component_produces_a_row_per_employee_with_party():
	config = make_config(per_employee=["Loan"])
	result = build_accrual_journal_accounts(_loan_slips(), config)

	loan_rows = result.rows_for(LOAN)
	# one row per employee — NOT aggregated into a single CC/dept row
	assert len(loan_rows) == 2
	by_emp = {r.party: r.credit for r in loan_rows}
	assert by_emp == {"EMP-1": Decimal(1_000_000), "EMP-2": Decimal(1_500_000)}
	assert all(r.party_type == "Employee" for r in loan_rows)

	# a NON-flagged deduction in the same slips is still aggregated
	tax_rows = result.rows_for(TAX_PAYABLE)
	assert len(tax_rows) == 1
	assert tax_rows[0].credit == Decimal(900_000)
	assert tax_rows[0].party is None

	assert result.is_balanced()


def test_per_employee_flag_off_aggregates_as_before():
	config = make_config()  # Loan NOT flagged
	result = build_accrual_journal_accounts(_loan_slips(), config)

	loan_rows = result.rows_for(LOAN)
	assert len(loan_rows) == 1  # merged: same account + cost centre
	assert loan_rows[0].credit == Decimal(2_500_000)
	assert loan_rows[0].party is None
	assert result.is_balanced()


def test_per_employee_pl_component_keeps_department_and_party():
	# flag a P&L component: row is per-employee AND still carries the department
	config = make_config(per_employee=["Absence"])
	slips = [
		slip(
			"SS-P",
			"Maintenance",
			[("CC-PROD", 100)],
			earnings=[("Basic Salary", 4_000_000)],
			deductions=[("Absence", 250_000)],
			employee="EMP-9",
		),
	]
	result = build_accrual_journal_accounts(slips, config)
	row = result.rows_for(PENALTY_EXP)[0]
	assert row.party == "EMP-9" and row.party_type == "Employee"
	assert row.department == "Maintenance"  # P&L dimension still stamped
	assert result.pl_rows_missing_department(config) == []
	assert result.is_balanced()


# ---------------------------------------------------------------------------
# Account-level Party mapping (replaces the SSO/tax party Server Script)
# ---------------------------------------------------------------------------


def test_account_party_is_stamped_on_matching_rows():
	config = make_config(account_parties={TAX_PAYABLE: ("Supplier", "SUP-TAX")})
	slips = [
		slip(
			"SS-T1",
			"Finance",
			[("CC-ADMIN", 100)],
			earnings=[("Basic Salary", 5_000_000)],
			deductions=[("Income Tax", 400_000)],
			employee="E1",
		),
		slip(
			"SS-T2",
			"Micro Lab",
			[("CC-PROD", 100)],
			earnings=[("Basic Salary", 6_000_000)],
			deductions=[("Income Tax", 500_000)],
			employee="E2",
		),
	]
	result = build_accrual_journal_accounts(slips, config)

	tax_rows = result.rows_for(TAX_PAYABLE)
	assert tax_rows, "expected tax payable rows"
	for r in tax_rows:
		assert r.party_type == "Supplier"
		assert r.party == "SUP-TAX"
	# an account with no configured party stays party-less
	assert all(not r.party for r in result.rows_for(SALARY_EXP))
	assert result.is_balanced()


def test_account_party_does_not_override_employee_party():
	# Loan is per-employee (Employee party). Even if someone misconfigured a
	# fixed party on the loan account, the employee party must win.
	config = make_config(
		per_employee=["Loan"],
		account_parties={LOAN: ("Supplier", "SHOULD-NOT-WIN")},
	)
	slips = [
		slip(
			"SS-L",
			"Finance",
			[("CC-ADMIN", 100)],
			earnings=[("Basic Salary", 5_000_000)],
			deductions=[("Loan", 1_000_000)],
			employee="EMP-7",
		),
	]
	result = build_accrual_journal_accounts(slips, config)
	loan = result.rows_for(LOAN)[0]
	assert loan.party_type == "Employee"
	assert loan.party == "EMP-7"
	assert result.is_balanced()


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


def test_unmapped_component_is_skipped_not_booked():
	"""Statistical / calculation-base components with no GL account (e.g. a
	'minimum daily wage' figure) are skipped — not booked and not counted toward
	the payable — so the entry still balances."""
	config = make_config()
	slips = [
		slip(
			"SS-E",
			"Finance",
			[("CC-ADMIN", 100)],
			earnings=[("Basic Salary", 5_000_000), ("Min Daily Wage Base", 9_999_999)],
		)
	]
	result = build_accrual_journal_accounts(slips, config)

	assert "Min Daily Wage Base" in result.skipped_components
	# only the accounted earning is booked
	assert sum(r.debit for r in result.rows_for(SALARY_EXP)) == Decimal(5_000_000)
	# payable = booked earnings only (the unbooked base does NOT inflate it)
	assert sum(r.credit for r in result.rows_for(PAYROLL_PAYABLE)) == Decimal(5_000_000)
	assert result.round_off_amount == Decimal(0)
	assert result.is_balanced()


# ---------------------------------------------------------------------------
# Randomised property test: whatever the data, the entry always balances and
# every P&L row has a department.
# ---------------------------------------------------------------------------

DEPARTMENTS = ["Finance", "Micro Lab", "Warehouse", "Production", "Security"]
COST_CENTERS = ["CC-PROD", "CC-ADMIN", "CC-SALES", "CC-QC", "CC-P2"]
EARNING_COMPONENTS = ["Basic Salary", "Overtime", "Employer Insurance (Expense)"]
DEDUCTION_COMPONENTS = ["Income Tax", "Employee Insurance", "Time Deduction", "Absence", "Advance", "Loan"]


def _random_splits(rng: random.Random) -> list[tuple[str, Decimal]]:
	n = rng.choice([1, 1, 2, 3])  # bias toward single, but exercise multi
	ccs = rng.sample(COST_CENTERS, n)
	if n == 1:
		return [(ccs[0], Decimal(100))]
	# integer percentages that sum to exactly 100
	cuts = sorted(rng.sample(range(1, 100), n - 1))
	bounds = [0, *cuts, 100]
	pcts = [Decimal(bounds[i + 1] - bounds[i]) for i in range(n)]
	return list(zip(ccs, pcts, strict=True))


def _random_slip(rng: random.Random, idx: int) -> SalarySlip:
	earnings = [
		(c, rng.randint(1, 50) * 100_000)
		for c in rng.sample(EARNING_COMPONENTS, rng.randint(1, len(EARNING_COMPONENTS)))
	]
	deductions = [
		(c, rng.randint(1, 20) * 50_000)
		for c in rng.sample(DEDUCTION_COMPONENTS, rng.randint(0, len(DEDUCTION_COMPONENTS)))
	]
	return slip(
		f"SS-R{idx}",
		rng.choice(DEPARTMENTS),
		_random_splits(rng),
		earnings,
		deductions,
	)


@pytest.mark.parametrize("seed", range(40))
def test_randomised_entries_always_balance_and_are_dimension_complete(seed):
	rng = random.Random(seed)
	config = make_config()
	slips = [_random_slip(rng, i) for i in range(rng.randint(1, 25))]

	result = build_accrual_journal_accounts(slips, config)

	# 1) always balances (round-off absorbs any residue)
	assert result.is_balanced(), f"seed={seed} unbalanced: {result.round_off_amount}"

	# 2) every P&L row has a department
	assert result.pl_rows_missing_department(config) == [], f"seed={seed} missing dept"

	# 3) round-off residue is tiny relative to the entry (a few currency units)
	total = result.total_debit
	assert (
		abs(result.round_off_amount) <= len(slips) * 5
	), f"seed={seed} residue too large: {result.round_off_amount} on total {total}"

	# 4) no row has both a debit and a credit
	for r in result.rows:
		assert not (r.debit and r.credit)


# ---------------------------------------------------------------------------
# IRR / per-employee years accrual (regression for 18 IRR imbalance)
# ---------------------------------------------------------------------------

YEARS_EXP = "EXP-YEARS"
YEARS_LIAB = "LIA-YEARS"


def _years_config(precision: int) -> AccrualConfig:
	accounts = dict(COMPONENT_ACCOUNTS)
	accounts["Years Expense"] = YEARS_EXP
	accounts["Years Liability"] = YEARS_LIAB
	roots = dict(ACCOUNT_ROOT_TYPE)
	roots[YEARS_EXP] = "Expense"
	roots[YEARS_LIAB] = "Liability"
	return AccrualConfig(
		component_accounts=accounts,
		account_root_type=roots,
		payable_account=PAYROLL_PAYABLE,
		round_off_account=ROUND_OFF,
		round_off_cost_center=ROUND_OFF_COST_CENTER,
		round_off_department=ROUND_OFF_DEPARTMENT,
		per_employee_components=frozenset({"Years Liability"}),
		precision=precision,
	)


def test_fractional_years_per_employee_liability_balances_at_irr_precision():
	"""Aggregated expense + per-employee liability must balance at precision 0 (IRR)."""
	config = _years_config(precision=0)
	# Reproduce the two edge fractions that previously broke under precision-2→0 re-round
	amounts = [
		Decimal("75278633.49811949"),
		Decimal("22851855.49942466"),
		Decimal("102382012.43835616"),
		Decimal("23756501.588770803"),
	]
	slips = []
	for i, amt in enumerate(amounts):
		slips.append(
			slip(
				f"SS-Y{i}",
				f"Dept-{i % 2}",
				[("CC-PROD" if i % 2 == 0 else "CC-ADMIN", 100)],
				earnings=[("Basic Salary", 1_000_000), ("Years Expense", amt)],
				deductions=[("Years Liability", amt)],
				employee=f"E{i}",
			)
		)
	result = build_accrual_journal_accounts(slips, config)
	assert result.is_balanced()
	assert result.round_off_amount == 0
	exp_total = sum(r.debit for r in result.rows_for(YEARS_EXP))
	liab_total = sum(r.credit for r in result.rows_for(YEARS_LIAB))
	assert exp_total == liab_total
	# liability is one row per employee
	assert len(result.rows_for(YEARS_LIAB)) == len(amounts)
	assert all(r.party_type == "Employee" for r in result.rows_for(YEARS_LIAB))


def test_fractional_years_also_balances_at_two_decimal_precision():
	config = _years_config(precision=2)
	amounts = [Decimal("100.333"), Decimal("200.666"), Decimal("50.001")]
	slips = [
		slip(
			f"SS-U{i}",
			"Finance",
			[("CC-ADMIN", 100)],
			earnings=[("Basic Salary", 1000), ("Years Expense", a)],
			deductions=[("Years Liability", a)],
			employee=f"U{i}",
		)
		for i, a in enumerate(amounts)
	]
	result = build_accrual_journal_accounts(slips, config)
	assert result.is_balanced()
	assert sum(r.debit for r in result.rows_for(YEARS_EXP)) == sum(
		r.credit for r in result.rows_for(YEARS_LIAB)
	)


def test_multi_cost_center_fractional_accrual_balances_with_round_off():
	config = _years_config(precision=0)
	slips = [
		slip(
			"SS-M0",
			"Production",
			[("CC-PROD", 60), ("CC-ADMIN", 40)],
			earnings=[("Basic Salary", 10_000), ("Years Expense", Decimal("100.4"))],
			deductions=[("Years Liability", Decimal("100.4"))],
			employee="M0",
		),
		slip(
			"SS-M1",
			"Finance",
			[("CC-ADMIN", 33), ("CC-PROD", 67)],
			earnings=[("Basic Salary", 20_000), ("Years Expense", Decimal("99.6"))],
			deductions=[("Years Liability", Decimal("99.6"))],
			employee="M1",
		),
	]
	result = build_accrual_journal_accounts(slips, config)
	assert result.is_balanced()
	assert sum(r.debit for r in result.rows_for(YEARS_EXP)) == sum(
		r.credit for r in result.rows_for(YEARS_LIAB)
	)


def test_eidi_style_integer_accrual_with_asymmetric_per_employee_flag():
	"""عیدی-style: integer amounts, expense aggregated, liability per-employee."""
	config = _years_config(precision=0)
	slips = [
		slip(
			f"SS-EIDI{i}",
			"Finance",
			[("CC-ADMIN", 100)],
			earnings=[("Basic Salary", 5_000_000), ("Years Expense", 42_360_990)],
			deductions=[("Years Liability", 42_360_990)],
			employee=f"EE{i}",
		)
		for i in range(5)
	]
	result = build_accrual_journal_accounts(slips, config)
	assert result.is_balanced()
	assert result.round_off_amount == 0
	assert len(result.rows_for(YEARS_LIAB)) == 5
	assert len(result.rows_for(YEARS_EXP)) == 1  # same dept+cc aggregated


def test_employer_insurance_style_both_aggregated_balances():
	config = make_config(precision=0)
	slips = [
		slip(
			f"SS-INS{i}",
			"Finance",
			[("CC-ADMIN", 100)],
			earnings=[
				("Basic Salary", 10_000_000),
				("Employer Insurance (Expense)", 2_000_000),
			],
			deductions=[("Employee Insurance", 700_000)],
			employee=f"I{i}",
		)
		for i in range(3)
	]
	result = build_accrual_journal_accounts(slips, config)
	assert result.is_balanced()
	assert sum(r.debit for r in result.rows_for(EMPLOYER_INS_EXP)) == Decimal(6_000_000)

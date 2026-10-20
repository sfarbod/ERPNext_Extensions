# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Framework-free logic for building the payroll accrual Journal Entry grouped by
``(Account, Cost Center, Department)``.

Why this module exists
----------------------
When *Department* (or any other Accounting Dimension) is configured as
**mandatory for P&L (Income/Expense) accounts**, the stock HRMS payroll accrual
entry fails validation: HRMS keys the accrual by ``(account, cost_center)`` and
copies dimensions from the *Payroll Entry* itself (a single value), so the
aggregated expense rows come out with a blank Department.

A common work-around — a ``Journal Entry`` ``before_validate`` script that
**deletes every Expense row and rebuilds it from earnings only** — silently
drops the credit rows of deductions that are mapped to expense accounts (a
deduction sharing an earning's expense account, or a penalty expense), which
unbalances the entry, and it ignores per-employee cost-centre percentage splits.

This module replaces that with a single, loss-less *group-by* pass:

* every earning and deduction row is split across the employee's payroll cost
  centres by percentage (mirroring HRMS), then aggregated by
  ``(account, cost_center, department)``;
* the *Department* is taken **directly from each Salary Slip** (not the Employee
  master);
* Department is stamped on **P&L rows only** (Income/Expense) — it is left empty
  on Balance-Sheet accounts (payables, advances, loans) where the dimension is
  not mandatory;
* any residue left by per-split rounding is absorbed by a single balancing line
  posted to the company Round-Off account, which is itself a P&L account and so
  also carries a (configurable) Department.

The module deliberately imports **nothing from Frappe** so it can be unit-tested
with plain Python structures that mirror the shapes observed in production. The
thin runtime shim that reads these structures out of the database and overrides
``PayrollEntry.make_accrual_jv_entry`` lives in ``payroll_entry_override.py``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

# ---------------------------------------------------------------------------
# Value types (plain data — mirror the production doctype shapes)
# ---------------------------------------------------------------------------

Number = Decimal | int | float | str

PL_ROOT_TYPES = frozenset({"Income", "Expense"})


@dataclass(frozen=True)
class CostCenterSplit:
	"""One row of ``Salary Structure Assignment.payroll_cost_centers``."""

	cost_center: str
	percentage: Decimal

	@staticmethod
	def make(cost_center: str, percentage: Number) -> CostCenterSplit:
		return CostCenterSplit(cost_center=cost_center, percentage=_dec(percentage))


@dataclass(frozen=True)
class ComponentAmount:
	"""One ``Salary Detail`` row (an earning or a deduction) on a Salary Slip."""

	component: str
	amount: Decimal

	@staticmethod
	def make(component: str, amount: Number) -> ComponentAmount:
		return ComponentAmount(component=component, amount=_dec(amount))


@dataclass(frozen=True)
class SalarySlip:
	"""The subset of a Salary Slip needed to build the accrual entry."""

	name: str
	employee: str
	department: str | None
	cost_center_splits: tuple[CostCenterSplit, ...]
	earnings: tuple[ComponentAmount, ...]
	deductions: tuple[ComponentAmount, ...]

	@staticmethod
	def make(
		name: str,
		employee: str,
		department: str | None,
		cost_center_splits: Iterable[CostCenterSplit],
		earnings: Iterable[ComponentAmount],
		deductions: Iterable[ComponentAmount],
	) -> SalarySlip:
		return SalarySlip(
			name=name,
			employee=employee,
			department=department,
			cost_center_splits=tuple(cost_center_splits),
			earnings=tuple(earnings),
			deductions=tuple(deductions),
		)


@dataclass(frozen=True)
class AccrualConfig:
	"""Company-level configuration for the accrual entry.

	``component_accounts``   : ``{salary_component: gl_account}`` (per company).
	``account_root_type``    : ``{gl_account: root_type}`` — used to decide which
	                           rows are P&L (and therefore need a Department).
	``payable_account``      : ``Company.default_payroll_payable_account``.
	``round_off_account``    : ``Company.round_off_account``.
	``round_off_cost_center``: ``Company.round_off_cost_center``.
	``round_off_department`` : Department stamped on the round-off line — needed
	                           when the round-off account is a P&L account. Set
	                           by the shim from a Company custom field.
	``per_employee_components``: components flagged "Process Based on Employee" —
	                           they bypass the Cost Center / Department group-by
	                           and are booked as a separate row per employee, with
	                           the Employee as Party (e.g. loans, advances).
	``account_parties``      : ``{gl_account: (party_type, party)}`` — a fixed
	                           Party stamped on the generated rows for that account
	                           (e.g. the Supplier for the SSO / tax payable). Set
	                           by the shim from the Salary Component Account child
	                           table, replacing the old party-assignment Server
	                           Script.
	``precision``            : currency precision (``0`` for a zero-decimal
	                           currency).
	"""

	component_accounts: dict[str, str]
	account_root_type: dict[str, str]
	payable_account: str
	round_off_account: str
	round_off_cost_center: str
	round_off_department: str | None
	per_employee_components: frozenset[str] = frozenset()
	account_parties: dict[str, tuple[str, str]] = field(default_factory=dict)
	precision: int = 0

	def account_for(self, component: str) -> str | None:
		return self.component_accounts.get(component)

	def party_for(self, account: str | None) -> tuple[str, str] | None:
		"""``(party_type, party)`` fixed for this account, or ``None``."""
		return self.account_parties.get(account) if account else None

	def is_pl_account(self, account: str | None) -> bool:
		if not account:
			return False
		return self.account_root_type.get(account) in PL_ROOT_TYPES

	def is_per_employee(self, component: str) -> bool:
		return component in self.per_employee_components


@dataclass
class AccountRow:
	"""One row of the resulting ``Journal Entry.accounts`` table."""

	account: str
	cost_center: str | None
	department: str | None
	debit: Decimal = Decimal(0)
	credit: Decimal = Decimal(0)
	party_type: str | None = None
	party: str | None = None

	def net(self) -> Decimal:
		return self.debit - self.credit


@dataclass
class AccrualResult:
	rows: list[AccountRow] = field(default_factory=list)
	round_off_amount: Decimal = Decimal(0)
	# Components encountered with no GL account mapping (skipped, not booked).
	skipped_components: list[str] = field(default_factory=list)

	@property
	def total_debit(self) -> Decimal:
		return sum((r.debit for r in self.rows), Decimal(0))

	@property
	def total_credit(self) -> Decimal:
		return sum((r.credit for r in self.rows), Decimal(0))

	def is_balanced(self) -> bool:
		return self.total_debit == self.total_credit

	def rows_for(self, account: str) -> list[AccountRow]:
		return [r for r in self.rows if r.account == account]

	def pl_rows_missing_department(self, config: AccrualConfig) -> list[AccountRow]:
		"""Every Income/Expense row must carry a Department; return the offenders."""
		return [r for r in self.rows if config.is_pl_account(r.account) and not r.department]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _dec(value: Number) -> Decimal:
	"""Coerce ints/floats/strings to ``Decimal`` without float artefacts."""
	if isinstance(value, Decimal):
		return value
	if isinstance(value, float):
		return Decimal(str(value))
	return Decimal(value)


def _quantize(amount: Decimal, precision: int) -> Decimal:
	exp = Decimal(1).scaleb(-precision)  # 10**-precision, e.g. Decimal("1") for 0
	return amount.quantize(exp, rounding=ROUND_HALF_UP)


def split_amount_by_cost_centers(
	amount: Number, splits: Iterable[CostCenterSplit], precision: int
) -> list[tuple[str, Decimal]]:
	"""Apportion ``amount`` across cost centres by percentage.

	Mirrors HRMS (``flt(amount * percentage / 100)`` per split, rounded to the
	currency precision). Rounding residue is **not** forced back here — it is
	absorbed once, at the entry level, by the Round-Off line. With a single
	100%% split this is an exact pass-through.
	"""
	amount = _dec(amount)
	out: list[tuple[str, Decimal]] = []
	for split in splits:
		part = _quantize(amount * split.percentage / Decimal(100), precision)
		out.append((split.cost_center, part))
	return out


def _accumulate(bucket: dict[tuple, Decimal], key: tuple, amount: Decimal) -> None:
	bucket[key] = bucket.get(key, Decimal(0)) + amount


# ---------------------------------------------------------------------------
# Core builder
# ---------------------------------------------------------------------------


def build_accrual_journal_accounts(slips: Iterable[SalarySlip], config: AccrualConfig) -> AccrualResult:
	"""Build the accrual ``Journal Entry.accounts`` rows, grouped by
	``(account, cost_center, department)`` and balanced via the round-off line.

	Structure produced (matching the HRMS accrual, aggregated / employee flag OFF):

	* **Debit**  — every *earning* component, at its expense account.
	* **Credit** — every *deduction* component, at its mapped account (this is
	  where deductions mapped to Expense accounts are preserved as contra-expense
	  credits rather than being dropped).
	* **Credit** — the net payable (``earnings`` minus ``deductions`` per slip) at
	  the payroll payable account.
	* **Round-off** — a single balancing line for the accumulated rounding
	  residue.

	Department is stamped on P&L rows only; Balance-Sheet rows (payable, and any
	deduction mapped to an asset/liability account) are left with ``department =
	None`` because the dimension is not mandatory there.
	"""
	precision = config.precision

	# key -> amount, keyed by (account, cost_center, department)
	debit_bucket: dict[tuple, Decimal] = {}
	credit_bucket: dict[tuple, Decimal] = {}
	# payable is a Balance-Sheet account -> keyed by (account, cost_center, None)
	payable_bucket: dict[tuple, Decimal] = {}

	skipped_components: set[str] = set()

	for slip in slips:
		splits = _normalise_splits(slip.cost_center_splits)
		slip_department = slip.department

		# Components without a GL account (statistical / calculation-base rows such
		# as a "minimum daily wage" figure, or ``do_not_include_in_total`` bases)
		# are not booked — exactly as stock HRMS does. Crucially they are also
		# excluded from the net-payable total below, so the payable equals
		# booked earnings minus booked deductions (= net pay) and the entry stays
		# balanced instead of dumping the unbooked base into the round-off line.

		# --- earnings -> debit ------------------------------------------------
		earnings_total = Decimal(0)
		for item in slip.earnings:
			account = config.account_for(item.component)
			if not account:
				skipped_components.add(item.component)
				continue
			earnings_total += _dec(item.amount)
			department = slip_department if config.is_pl_account(account) else None
			# Components flagged "Process Based on Employee" keep employee-level
			# granularity (a distinct row per employee, with Party = Employee)
			# instead of being aggregated by cost centre / department.
			employee = slip.employee if config.is_per_employee(item.component) else None
			for cost_center, part in split_amount_by_cost_centers(item.amount, splits, precision):
				_accumulate(debit_bucket, (account, cost_center, department, employee), part)

		# --- deductions -> credit --------------------------------------------
		deductions_total = Decimal(0)
		for item in slip.deductions:
			account = config.account_for(item.component)
			if not account:
				skipped_components.add(item.component)
				continue
			deductions_total += _dec(item.amount)
			department = slip_department if config.is_pl_account(account) else None
			employee = slip.employee if config.is_per_employee(item.component) else None
			for cost_center, part in split_amount_by_cost_centers(item.amount, splits, precision):
				_accumulate(credit_bucket, (account, cost_center, department, employee), part)

		# --- net payable -> credit (Balance-Sheet, no department) ------------
		net_payable = earnings_total - deductions_total
		for cost_center, part in split_amount_by_cost_centers(net_payable, splits, precision):
			_accumulate(payable_bucket, (config.payable_account, cost_center, None, None), part)

	result = AccrualResult()
	result.skipped_components = sorted(skipped_components)
	_emit_rows(result, debit_bucket, is_debit=True)
	_emit_rows(result, credit_bucket, is_debit=False)
	_emit_rows(result, payable_bucket, is_debit=False)

	_apply_account_parties(result, config)
	# Quantize emitted rows to the configured precision before measuring residual
	# so round-off matches the amounts Journal Entry will store after currency
	# rounding (critical for zero-decimal currencies such as IRR).
	_quantize_emitted_rows(result, precision)
	_append_round_off(result, config)
	return result


def _quantize_emitted_rows(result: AccrualResult, precision: int) -> None:
	"""Re-apply currency precision to every debit/credit before round-off."""
	for row in result.rows:
		if row.debit:
			row.debit = _quantize(row.debit, precision)
		if row.credit:
			row.credit = _quantize(row.credit, precision)


def _apply_account_parties(result: AccrualResult, config: AccrualConfig) -> None:
	"""Stamp the fixed Party (e.g. SSO / tax Supplier) on rows for the configured
	accounts — but never override a Party already set (e.g. an employee-keyed
	loan row). Replaces the old party-assignment Server Script."""
	if not config.account_parties:
		return
	for row in result.rows:
		if row.party:
			continue
		party = config.party_for(row.account)
		if party:
			row.party_type, row.party = party


def _emit_rows(result: AccrualResult, bucket: dict[tuple, Decimal], *, is_debit: bool) -> None:
	# Deterministic ordering keeps output stable and diff-friendly for review.
	for (account, cost_center, department, employee), amount in sorted(
		bucket.items(),
		key=lambda kv: (kv[0][0], kv[0][1] or "", kv[0][2] or "", kv[0][3] or ""),
	):
		if amount == 0:
			continue
		# A negative bucket total (e.g. a net-negative payable) flips the side.
		debit = amount if is_debit else Decimal(0)
		credit = Decimal(0) if is_debit else amount
		if amount < 0:
			debit, credit = -credit, -debit
		row = AccountRow(
			account=account,
			cost_center=cost_center,
			department=department,
			debit=debit,
			credit=credit,
		)
		# Employee-keyed rows (flagged components) carry the Employee as Party.
		if employee:
			row.party_type = "Employee"
			row.party = employee
		result.rows.append(row)


def _append_round_off(result: AccrualResult, config: AccrualConfig) -> None:
	residual = result.total_debit - result.total_credit
	result.round_off_amount = residual
	if residual == 0:
		return
	row = AccountRow(
		account=config.round_off_account,
		cost_center=config.round_off_cost_center,
		department=config.round_off_department,
	)
	if residual > 0:
		# debits exceed credits -> add a credit to balance
		row.credit = residual
	else:
		row.debit = -residual
	result.rows.append(row)


def _normalise_splits(splits: tuple[CostCenterSplit, ...]) -> tuple[CostCenterSplit, ...]:
	"""A slip with no explicit split is treated as 100%% on a single implicit
	cost centre only if one is provided; otherwise the caller must supply at
	least one split. HRMS guarantees at least one entry, so we simply validate."""
	if not splits:
		raise MissingCostCenterError()
	return splits


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class PayrollAccrualError(Exception):
	"""Base error for accrual grouping."""


class UnmappedSalaryComponentError(PayrollAccrualError):
	def __init__(self, components: list[str]):
		self.components = components
		super().__init__(
			"Salary components have no account mapping for this company: " + ", ".join(components)
		)


class MissingCostCenterError(PayrollAccrualError):
	def __init__(self):
		super().__init__("Salary Slip has no payroll cost centre to allocate against.")

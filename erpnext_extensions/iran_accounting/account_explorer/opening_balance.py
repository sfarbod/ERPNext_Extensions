# Copyright (c) 2026, Farbod Siyahpoosh and contributors

from __future__ import annotations

from contextlib import contextmanager

import frappe
from erpnext.accounts.report.financial_statements import apply_additional_conditions, set_gl_entries_by_account
from erpnext.accounts.report.trial_balance.trial_balance import get_opening_balances
from frappe.query_builder.functions import Sum
from frappe.utils import flt
from pypika import Bracket
from pypika.terms import LiteralValue

from erpnext_extensions.iran_accounting.account_explorer import e1_gl_scope
from erpnext_extensions.iran_accounting.account_explorer.filters import spec_to_trial_balance_filters
from erpnext_extensions.iran_accounting.account_explorer.gle_filters import (
	apply_period_turnover_filters,
	apply_scoped_gle_filters,
)
from erpnext_extensions.iran_accounting.account_explorer.measures import measures_from_opening_period
from erpnext_extensions.iran_accounting.account_explorer.opening_entry_policy import (
	AccountAxisEngine,
	OpeningEntryPolicyMode,
	adjust_tb_opening_for_policy,
	aggregate_opening_flagged_by_account,
	aggregate_opening_flagged_pre_in_by_account,
	apply_policy_opening_filters,
	apply_policy_turnover_filters,
	policy_from_spec,
	select_account_axis_engine,
	site_ignore_is_opening,
)
from erpnext_extensions.iran_accounting.account_explorer.party_opening import _toggle_debit_credit
from erpnext_extensions.iran_accounting.account_explorer.schemas import AccountExplorerQuerySpec


def get_account_wise_measures(
	spec: AccountExplorerQuerySpec, account_names: list[str] | None = None
) -> dict[str, dict]:
	"""Return per-account opening / period / closing measures.

	Engine selection (v5.1.1 asymmetric contract):

	- **sle_scoped_stock** (Case A) — Item / Item Group / Warehouse filters:
	  scoped SLE → warehouse → inventory account. Same population as Item /
	  Item Group. Item|IG → Account EQUAL (Δ=0). Not E3 / voucher GL amounts.
	- **E3 / E1 / E2** (Case B / other GL filters) — posted tabGL when inventory
	  filters are absent (or after Case A short-circuit is skipped).
	"""
	from erpnext_extensions.iran_accounting.account_explorer.sle_scoped_account import (
		aggregate_sle_scoped_account_measures,
		select_sle_scoped_account_engine,
	)

	# Case A short-circuit: must run before select_account_axis_engine (E3).
	if select_sle_scoped_account_engine(spec):
		return aggregate_sle_scoped_account_measures(spec, account_names)

	engine = select_account_axis_engine(spec)
	if engine == AccountAxisEngine.E3_SCOPED_GL:
		return _get_account_wise_measures_scoped(spec, account_names)
	if engine == AccountAxisEngine.E2_TB_GAP_SUPPLEMENT:
		return _get_account_wise_measures_e2(spec, account_names)
	return _get_account_wise_measures_e1(spec, account_names)


def _get_account_wise_measures_e1(
	spec: AccountExplorerQuerySpec, account_names: list[str] | None = None
) -> dict[str, dict]:
	"""E1 — TB baseline + pre-period opening-flagged delta."""
	filters = spec_to_trial_balance_filters(spec)
	policy = policy_from_spec(spec)
	ignore_is_opening = site_ignore_is_opening()
	# v4.6.1: drill/filter scope → narrow TB + policy-aux GL SQL; root stays company-wide.
	restrict_accounts = e1_gl_scope.resolve_narrowed_gl_accounts(spec)
	opening_balances = _get_opening_balances_for_e1(filters, ignore_is_opening, restrict_accounts)
	aux_pre, aux_in = aggregate_opening_flagged_pre_in_by_account(spec)

	gl_entries_by_account: dict[str, list] = {}
	# Always batch-fetch period GL (group_by_account=True). Policy OFF excludes
	# is_opening='Yes' turnover via aux_in subtraction — not ERPNext
	# ignore_opening_entries=True, which triggers per-account query explosion.
	_set_period_gl_entries_for_e1(
		spec,
		filters,
		gl_entries_by_account,
		restrict_accounts=restrict_accounts,
	)

	target_accounts = account_names
	if target_accounts is None:
		target_accounts = list({*opening_balances.keys(), *gl_entries_by_account.keys(), *aux_pre.keys()})

	result: dict[str, dict] = {}
	for account in target_accounts:
		opening = opening_balances.get(account, {})
		aux_debit, aux_credit = aux_pre.get(account, (0.0, 0.0))
		in_debit, in_credit = aux_in.get(account, (0.0, 0.0)) if aux_in else (0.0, 0.0)
		opening_debit, opening_credit = adjust_tb_opening_for_policy(
			flt(opening.get("opening_debit")),
			flt(opening.get("opening_credit")),
			aux_debit,
			aux_credit,
			policy,
		)
		if aux_in:
			opening_debit = flt(opening_debit) - flt(in_debit)
			opening_credit = flt(opening_credit) - flt(in_credit)
		period_debit = 0.0
		period_credit = 0.0
		for entry in gl_entries_by_account.get(account, []):
			period_debit += flt(entry.debit)
			period_credit += flt(entry.credit)
		if policy == OpeningEntryPolicyMode.EXCLUDE_OPENING_FLAGGED:
			period_debit = flt(period_debit) - flt(in_debit)
			period_credit = flt(period_credit) - flt(in_credit)
		result[account] = measures_from_opening_period(
			opening_debit, opening_credit, period_debit, period_credit
		)
	return result


def _get_account_wise_measures_e2(
	spec: AccountExplorerQuerySpec, account_names: list[str] | None = None
) -> dict[str, dict]:
	"""E2 — E1 + gap-window opening-flagged supplement to opening bucket."""
	result = _get_account_wise_measures_e1(spec, account_names)
	aux_gap = aggregate_opening_flagged_by_account(spec, bucket="gap")
	if not aux_gap:
		return result

	target_accounts = account_names if account_names is not None else list(result.keys())
	for account in target_accounts:
		if account not in result:
			continue
		gap_debit, gap_credit = aux_gap.get(account, (0.0, 0.0))
		if not gap_debit and not gap_credit:
			continue
		row = result[account]
		result[account] = measures_from_opening_period(
			flt(row["opening_debit"]) + flt(gap_debit),
			flt(row["opening_credit"]) + flt(gap_credit),
			flt(row["period_debit"]),
			flt(row["period_credit"]),
		)
	return result


@contextmanager
def _force_report_type_account_names(account_names: list[str]):
	"""Make ERPNext opening Account lookups return exactly ``account_names``."""
	forced = list(account_names)
	original = frappe.db.get_all

	def wrapped(doctype, *args, **kwargs):
		filters = kwargs.get("filters")
		if filters is None and args and isinstance(args[0], dict):
			filters = args[0]
		if (
			doctype == "Account"
			and isinstance(filters, dict)
			and "report_type" in filters
			and kwargs.get("pluck") == "name"
		):
			return list(forced)
		return original(doctype, *args, **kwargs)

	frappe.db.get_all = wrapped  # type: ignore[method-assign]
	try:
		yield
	finally:
		frappe.db.get_all = original  # type: ignore[method-assign]


def _get_opening_balances_for_e1(filters, ignore_is_opening, restrict_accounts: list[str] | None):
	"""ERPNext Trial Balance opening, optionally restricted to a drill account tree."""
	if not restrict_accounts:
		return get_opening_balances(filters, ignore_is_opening)

	from erpnext.accounts.report.trial_balance import trial_balance as tb_module

	allow = set(restrict_accounts)
	original = tb_module.get_opening_balance

	def scoped_get_opening_balance(doctype, filters, report_type, accounting_dimensions, *args, **kwargs):
		type_accounts = frappe.db.get_all("Account", filters={"report_type": report_type}, pluck="name")
		narrowed = [name for name in type_accounts if name in allow]
		if not narrowed:
			return []
		with _force_report_type_account_names(narrowed):
			return original(doctype, filters, report_type, accounting_dimensions, *args, **kwargs)

	tb_module.get_opening_balance = scoped_get_opening_balance  # type: ignore[assignment]
	try:
		return get_opening_balances(filters, ignore_is_opening)
	finally:
		tb_module.get_opening_balance = original  # type: ignore[assignment]


def _set_period_gl_entries_for_e1(
	spec: AccountExplorerQuerySpec,
	filters,
	gl_entries_by_account: dict,
	*,
	restrict_accounts: list[str] | None,
) -> None:
	"""Period GL grouped by account — company-wide or drill-scoped."""
	ignore_closing = not spec.include_period_closing_vouchers
	if not restrict_accounts:
		set_gl_entries_by_account(
			spec.company,
			spec.from_date,
			spec.to_date,
			filters,
			gl_entries_by_account,
			ignore_closing_entries=ignore_closing,
			ignore_opening_entries=False,
			group_by_account=True,
		)
		return

	gl_entry = frappe.qb.DocType("GL Entry")
	query = (
		frappe.qb.from_(gl_entry)
		.select(
			gl_entry.account,
			Sum(gl_entry.debit).as_("debit"),
			Sum(gl_entry.credit).as_("credit"),
			Sum(gl_entry.debit_in_account_currency).as_("debit_in_account_currency"),
			Sum(gl_entry.credit_in_account_currency).as_("credit_in_account_currency"),
			gl_entry.account_currency,
			gl_entry.posting_date,
			gl_entry.is_opening,
			gl_entry.fiscal_year,
		)
		.where(gl_entry.company == filters.company)
		.where(gl_entry.is_cancelled == 0)
		.where(gl_entry.posting_date <= spec.to_date)
		.where(gl_entry.account.isin(restrict_accounts))
	)
	# v4.6.2: never FORCE INDEX(posting_date_company_index) when account IN (...) is
	# present — that index defeats account selectivity (~1.5s vs ~1ms on leaf drills).
	# Root / company-wide path above still uses ERPNext set_gl_entries_by_account
	# (unchanged date/company force index).
	query = apply_additional_conditions(
		"GL Entry", query, spec.from_date, ignore_closing, filters
	)
	query = query.groupby(gl_entry.account)

	from frappe.desk.reportview import build_match_conditions

	if match_conditions := build_match_conditions("GL Entry"):
		query = query.where(Bracket(LiteralValue(match_conditions)))

	for entry in query.run(as_dict=True):
		gl_entries_by_account.setdefault(entry.account, []).append(entry)


def get_account_opening_balances(spec: AccountExplorerQuerySpec) -> dict[str, tuple[float, float]]:
	"""Filtered opening balances via scoped policy GL WHERE."""
	gle = frappe.qb.DocType("GL Entry")
	query = frappe.qb.from_(gle).select(
		gle.account,
		Sum(gle.debit).as_("opening_debit"),
		Sum(gle.credit).as_("opening_credit"),
	)
	query = apply_scoped_gle_filters(query, gle, spec)
	query = apply_policy_opening_filters(query, gle, spec)
	query = query.groupby(gle.account)

	return {
		row.account: _toggle_debit_credit(row.opening_debit, row.opening_credit)
		for row in query.run(as_dict=True)
		if row.account
	}


def get_account_period_balances(spec: AccountExplorerQuerySpec) -> dict[str, tuple[float, float]]:
	"""Filtered period turnover via scoped policy GL WHERE."""
	gle = frappe.qb.DocType("GL Entry")
	query = frappe.qb.from_(gle).select(
		gle.account,
		Sum(gle.debit).as_("period_debit"),
		Sum(gle.credit).as_("period_credit"),
	)
	query = apply_scoped_gle_filters(query, gle, spec)
	query = apply_policy_turnover_filters(query, gle, spec)
	query = query.groupby(gle.account)

	return {
		row.account: (flt(row.period_debit), flt(row.period_credit))
		for row in query.run(as_dict=True)
		if row.account
	}


def _get_account_wise_measures_scoped(
	spec: AccountExplorerQuerySpec, account_names: list[str] | None = None
) -> dict[str, dict]:
	opening = get_account_opening_balances(spec)
	period = get_account_period_balances(spec)

	if account_names is None:
		target_accounts = list({*opening.keys(), *period.keys()})
	else:
		target_accounts = list(account_names)

	result: dict[str, dict] = {}
	for account in target_accounts:
		opening_debit, opening_credit = opening.get(account, (0.0, 0.0))
		period_debit, period_credit = period.get(account, (0.0, 0.0))
		result[account] = measures_from_opening_period(
			opening_debit, opening_credit, period_debit, period_credit
		)
	return result


def get_accounts_with_direct_gl_postings(
	spec: AccountExplorerQuerySpec, group_account_names: set[str]
) -> set[str]:
	if not group_account_names:
		return set()

	from erpnext_extensions.iran_accounting.account_explorer.sle_scoped_account import (
		aggregate_sle_scoped_account_measures,
		select_sle_scoped_account_engine,
	)

	if select_sle_scoped_account_engine(spec):
		measures = aggregate_sle_scoped_account_measures(spec, list(group_account_names))
		return {name for name in measures if name in group_account_names}

	gle = frappe.qb.DocType("GL Entry")
	query = frappe.qb.from_(gle).select(gle.account).distinct()
	query = apply_scoped_gle_filters(query, gle, spec)
	query = apply_period_turnover_filters(query, gle, spec)
	query = query.where(gle.account.isin(list(group_account_names)))

	return {row.account for row in query.run(as_dict=True) if row.account}

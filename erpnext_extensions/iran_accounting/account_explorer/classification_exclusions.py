# Copyright (c) 2026, Farbod Siyahpoosh and contributors
"""Account hierarchy exclusion enrichment and reconciliation helpers."""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt

from erpnext_extensions.iran_accounting.account_explorer.measures import (
	finalize_measures,
	zero_measures,
)


REASON_LABELS = {
	"non_numeric_or_missing_account_number": _(
		"Account Number is missing or not a pure numeric hierarchy code"
	),
	"empty_code_prefix": _("Account Number prefix is empty for the selected level"),
}


def enrich_excluded_account_rows(company: str, excluded: list[dict]) -> list[dict]:
	"""Batch-enrich excluded residual rows with Account master fields."""
	if not excluded:
		return []
	names = list(dict.fromkeys([row.get("account") for row in excluded if row.get("account")]))
	if not names:
		return list(excluded)
	meta_rows = frappe.get_all(
		"Account",
		filters={"name": ("in", names), "company": company} if company else {"name": ("in", names)},
		fields=[
			"name",
			"account_number",
			"account_name",
			"parent_account",
			"company",
			"is_group",
			"account_type",
			"root_type",
		],
		limit=len(names),
	)
	by_name = {row.name: row for row in meta_rows}
	out: list[dict] = []
	for item in excluded:
		account = item.get("account")
		meta = by_name.get(account) or {}
		reason = item.get("reason") or "non_numeric_or_missing_account_number"
		measures = {
			"opening_debit": flt(item.get("opening_debit")),
			"opening_credit": flt(item.get("opening_credit")),
			"period_debit": flt(item.get("period_debit")),
			"period_credit": flt(item.get("period_credit")),
			"closing_debit": flt(item.get("closing_debit")),
			"closing_credit": flt(item.get("closing_credit")),
			"net_balance": flt(item.get("net_balance")),
			"debit_balance": flt(item.get("debit_balance")),
			"credit_balance": flt(item.get("credit_balance")),
		}
		if not measures.get("debit_balance") and not measures.get("credit_balance"):
			finalize_measures(measures)
		out.append(
			{
				"account": account,
				"account_number": meta.get("account_number")
				if meta
				else item.get("account_number"),
				"account_name": meta.get("account_name") or account,
				"parent_account": meta.get("parent_account"),
				"company": meta.get("company") or company,
				"is_group": int(meta.get("is_group") or 0),
				"account_type": meta.get("account_type"),
				"root_type": meta.get("root_type"),
				"reason": reason,
				"reason_label": REASON_LABELS.get(reason) or reason,
				"opening_debit": measures["opening_debit"],
				"opening_credit": measures["opening_credit"],
				"period_debit": measures["period_debit"],
				"period_credit": measures["period_credit"],
				"closing_debit": measures["closing_debit"],
				"closing_credit": measures["closing_credit"],
				"net_balance": measures["net_balance"],
				"debit_balance": measures["debit_balance"],
				"credit_balance": measures["credit_balance"],
			}
		)
	return out


def build_classification_reconciliation(
	*,
	included_totals: dict | None,
	excluded_residual: dict | None,
) -> dict:
	"""Additive measure reconciliation: Included + Excluded = Full Scope.

	Balance columns are presentation of signed closing; do not sum debit_balance
	and credit_balance as independent additive measures.
	"""
	included = included_totals or zero_measures()
	excluded = excluded_residual or zero_measures()
	full = zero_measures()
	for key in (
		"opening_debit",
		"opening_credit",
		"period_debit",
		"period_credit",
		"closing_debit",
		"closing_credit",
		"net_balance",
	):
		full[key] = flt(included.get(key)) + flt(excluded.get(key))
	finalize_measures(full)
	ok = True
	for key in ("period_debit", "period_credit", "net_balance"):
		expected = flt(included.get(key)) + flt(excluded.get(key))
		if abs(flt(full.get(key)) - expected) > 0.01:
			ok = False
	return {
		"ok": ok,
		"included": {
			"period_debit": flt(included.get("period_debit")),
			"period_credit": flt(included.get("period_credit")),
			"net_balance": flt(included.get("net_balance")),
			"debit_balance": flt(included.get("debit_balance")),
			"credit_balance": flt(included.get("credit_balance")),
		},
		"excluded": {
			"period_debit": flt(excluded.get("period_debit")),
			"period_credit": flt(excluded.get("period_credit")),
			"net_balance": flt(excluded.get("net_balance")),
			"debit_balance": flt(excluded.get("debit_balance")),
			"credit_balance": flt(excluded.get("credit_balance")),
		},
		"full_scope": {
			"period_debit": flt(full.get("period_debit")),
			"period_credit": flt(full.get("period_credit")),
			"net_balance": flt(full.get("net_balance")),
			"debit_balance": flt(full.get("debit_balance")),
			"credit_balance": flt(full.get("credit_balance")),
		},
		"balance_policy": (
			"period_debit/period_credit/net_balance are additive. "
			"debit_balance/credit_balance are presentation of signed closing "
			"and must not be summed across buckets as independent addends."
		),
	}


def persian_exclusion_message(count: int) -> str:
	return _(
		"{0} حساب به دلیل نامشخص بودن کد یا عدم انطباق با ساختار سطح‌بندی حساب‌ها، "
		"در جدول سلسله‌مراتب نمایش داده نشده‌اند."
	).format(count)

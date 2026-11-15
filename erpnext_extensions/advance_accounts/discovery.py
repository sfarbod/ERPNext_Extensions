# Copyright (c) 2026, ERPNext Extensions contributors
"""Widen PE advance discovery to all eligible advance accounts."""

from __future__ import annotations

import frappe
from frappe.query_builder import Criterion
from frappe.query_builder.custom import ConstantColumn

from erpnext_extensions.advance_accounts.eligibility import (
	direction_for_party_type,
	get_eligible_advance_accounts,
)

_PATCHED = False
_original_get_common_query = None


def _resolve_company(party_account, condition, default_advance_account) -> str | None:
	if condition and condition.get("company"):
		return condition["company"]
	for acc in list(party_account or []) + ([default_advance_account] if default_advance_account else []):
		if not acc:
			continue
		co = frappe.db.get_value("Account", acc, "company")
		if co:
			return co
	return None


def _patched_get_common_query(
	party_type,
	party,
	party_account,
	default_advance_account,
	limit,
	condition,
):
	"""Like ERPNext get_common_query but matches any eligible advance account."""
	from erpnext.controllers.accounts_controller import get_common_query as _unused  # noqa: F401

	account_type = frappe.db.get_value("Party Type", party_type, "account_type")
	payment_type = "Receive" if account_type == "Receivable" else "Pay"
	payment_entry = frappe.qb.DocType("Payment Entry")

	q = (
		frappe.qb.from_(payment_entry)
		.select(
			ConstantColumn("Payment Entry").as_("reference_type"),
			(payment_entry.name).as_("reference_name"),
			payment_entry.posting_date,
			(payment_entry.remarks).as_("remarks"),
			(payment_entry.book_advance_payments_in_separate_party_account),
		)
		.where(payment_entry.payment_type == payment_type)
		.where(payment_entry.party_type == party_type)
		.where(payment_entry.party == party)
		.where(payment_entry.docstatus == 1)
	)

	field = "paid_from" if payment_type == "Receive" else "paid_to"

	q = q.select((payment_entry[f"{field}_account_currency"]).as_("currency"))
	q = q.select(payment_entry[field])
	account_condition = payment_entry[field].isin(party_account) if party_account else None

	company = _resolve_company(party_account, condition, default_advance_account)
	direction = direction_for_party_type(party_type)
	eligible = (
		get_eligible_advance_accounts(company, direction, party_type=party_type, party=party)
		if company and direction
		else []
	)
	# Always include explicitly passed default_advance_account (PR form selection)
	if default_advance_account and default_advance_account not in eligible:
		eligible = list(eligible) + [default_advance_account]

	if eligible:
		advance_condition = (payment_entry[field].isin(eligible)) & (
			payment_entry.book_advance_payments_in_separate_party_account == 1
		)
		if account_condition is not None:
			q = q.where(account_condition | advance_condition)
		else:
			q = q.where(advance_condition)
	elif account_condition is not None:
		q = q.where(account_condition)
	else:
		# No party accounts and no advances — empty result
		q = q.where(payment_entry.name.isnull())

	if payment_type == "Receive":
		q = q.select((payment_entry.source_exchange_rate).as_("exchange_rate"))
	else:
		q = q.select((payment_entry.target_exchange_rate).as_("exchange_rate"))

	if condition:
		from frappe.query_builder.functions import IfNull

		common_filter_conditions = []
		common_filter_conditions.append(payment_entry.company == condition["company"])
		if condition.get("name", None):
			common_filter_conditions.append(payment_entry.name.like(f"%{condition.get('name')}%"))
		if condition.get("from_payment_date"):
			common_filter_conditions.append(payment_entry.posting_date.gte(condition["from_payment_date"]))
		if condition.get("to_payment_date"):
			common_filter_conditions.append(payment_entry.posting_date.lte(condition["to_payment_date"]))
		if condition.get("get_payments") is True:
			if condition.get("cost_center"):
				common_filter_conditions.append(payment_entry.cost_center == condition["cost_center"])
			if condition.get("accounting_dimensions"):
				apply_strict_user_permissions = frappe.get_system_settings("apply_strict_user_permissions")
				for field, val in (condition.get("accounting_dimensions") or {}).items():
					if isinstance(val, list | tuple | set):
						value_condition = payment_entry[field].isin(val)
						if apply_strict_user_permissions:
							common_filter_conditions.append(value_condition)
						else:
							common_filter_conditions.append(
								(IfNull(payment_entry[field], "") == "") | value_condition
							)
					else:
						common_filter_conditions.append(payment_entry[field] == val)
			if condition.get("minimum_payment_amount"):
				common_filter_conditions.append(
					payment_entry.unallocated_amount.gte(condition["minimum_payment_amount"])
				)
			if condition.get("maximum_payment_amount"):
				common_filter_conditions.append(
					payment_entry.unallocated_amount.lte(condition["maximum_payment_amount"])
				)
		q = q.where(Criterion.all(common_filter_conditions))

	q = q.orderby(payment_entry.posting_date)
	q = q.limit(limit) if limit else q
	return q


def apply_discovery_patch() -> None:
	global _PATCHED, _original_get_common_query
	import erpnext.controllers.accounts_controller as ac

	if _PATCHED and getattr(ac, "get_common_query", None) is _patched_get_common_query:
		return

	if _original_get_common_query is None:
		_original_get_common_query = ac.get_common_query

	ac.get_common_query = _patched_get_common_query
	_PATCHED = True

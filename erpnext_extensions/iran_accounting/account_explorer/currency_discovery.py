# Copyright (c) 2026, Farbod Siyahpoosh and contributors

from __future__ import annotations

import frappe


def discover_company_currencies(company: str) -> list[str]:
	"""Distinct currencies used on GL Entry for a company.

	Cached per company so metadata / toolbar enrichment does not repeatedly
	scan the full GL table on every Account Explorer open.
	"""
	if not company:
		return []
	cache_key = f"ae:company_currencies:{company}"
	cached = frappe.cache.get_value(cache_key)
	if cached is not None:
		return list(cached)
	rows = frappe.db.sql(
		"""
		select distinct account_currency as currency
		from `tabGL Entry`
		where company = %s and ifnull(account_currency, '') != '' and is_cancelled = 0
		union
		select distinct transaction_currency as currency
		from `tabGL Entry`
		where company = %s and ifnull(transaction_currency, '') != '' and is_cancelled = 0
		order by currency
		""",
		(company, company),
	)
	currencies = [row[0] for row in rows if row[0]]
	frappe.cache.set_value(cache_key, currencies, expires_in_sec=3600)
	return currencies


def clear_company_currency_cache(company: str | None = None) -> None:
	if company:
		frappe.cache.delete_value(f"ae:company_currencies:{company}")
		return
	# Best-effort: version bump handled via metadata_cache_version elsewhere.

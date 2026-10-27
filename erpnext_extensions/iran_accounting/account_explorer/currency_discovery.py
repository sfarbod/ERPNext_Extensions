# Copyright (c) 2026, Farbod Siyahpoosh and contributors

from __future__ import annotations

import frappe


def discover_company_currencies(company: str) -> list[str]:
	"""Distinct currencies available for Account Explorer currency filters.

	Uses Company default currency + ``tabAccount.account_currency`` for the
	company. This avoids a full ``tabGL Entry`` DISTINCT scan (measured ~3s on
	~750k GL rows when Redis cache is cold).

	Account masters are a safe superset for filter dropdowns: any GL currency
	that can post must exist on an Account. Unused Account currencies may
	appear in the list; that is acceptable.

	Results are cached per company for one hour.
	"""
	if not company:
		return []
	cache_key = f"ae:company_currencies:{company}"
	cached = frappe.cache.get_value(cache_key)
	if cached is not None:
		return list(cached)

	currencies: set[str] = set()
	default_currency = frappe.db.get_value("Company", company, "default_currency")
	if default_currency:
		currencies.add(default_currency)

	rows = frappe.db.sql(
		"""
		select distinct account_currency as currency
		from `tabAccount`
		where company = %s and ifnull(account_currency, '') != ''
		order by currency
		""",
		(company,),
	)
	for row in rows:
		if row[0]:
			currencies.add(row[0])

	ordered = sorted(currencies)
	frappe.cache.set_value(cache_key, ordered, expires_in_sec=3600)
	return list(ordered)


def clear_company_currency_cache(company: str | None = None) -> None:
	if company:
		frappe.cache.delete_value(f"ae:company_currencies:{company}")
		return
	# Best-effort: version bump handled via metadata_cache_version elsewhere.

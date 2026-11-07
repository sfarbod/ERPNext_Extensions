# Copyright (c) 2026, Farbod Siyahpoosh and contributors
"""Server-side column filters for Account Explorer grids (v5.7.7)."""

from __future__ import annotations

from datetime import date, datetime

from frappe.utils import cint, cstr, flt, getdate


# Fields that accept numeric / range filters (min,max or comparison prefixes).
NUMERIC_COLUMN_FIELDS = frozenset(
	{
		"period_debit",
		"period_credit",
		"debit_balance",
		"credit_balance",
		"opening_debit",
		"opening_credit",
		"closing_debit",
		"closing_credit",
		"net_balance",
		"debit",
		"credit",
		"running_balance",
		"scoped_debit",
		"scoped_credit",
		"scoped_net",
		"inward_value",
		"outward_value",
		"company_period_debit",
		"company_period_credit",
		"company_net_balance",
		"company_debit_balance",
		"company_credit_balance",
		"sales_base_net_amount",
		"purchase_base_net_amount",
	}
)

DATE_COLUMN_FIELDS = frozenset({"posting_date"})


def normalize_column_filters(raw) -> dict[str, str]:
	"""Return cleaned ``{field: query}`` map (empty values dropped)."""
	if not isinstance(raw, dict):
		return {}
	out: dict[str, str] = {}
	for key, value in raw.items():
		field = cstr(key or "").strip()
		if not field or field.startswith("__"):
			continue
		# Migrate legacy running balance filter keys.
		if field in {"running_debit_balance", "running_credit_balance"}:
			field = "running_balance"
		text = cstr(value).strip()
		if text:
			out[field] = text
	return out


def apply_column_filters_to_rows(rows: list[dict], column_filters: dict | None) -> list[dict]:
	"""Filter in-memory summary/detail rows before pagination."""
	filters = normalize_column_filters(column_filters or {})
	if not filters or not rows:
		return rows
	kept: list[dict] = []
	for row in rows:
		if _row_matches_filters(row, filters):
			kept.append(row)
	return kept


def _row_matches_filters(row: dict, filters: dict[str, str]) -> bool:
	for field, query in filters.items():
		if not _value_matches(row.get(field), query, field):
			return False
	return True


def _value_matches(value, query: str, field: str) -> bool:
	if field in DATE_COLUMN_FIELDS:
		return _date_matches(value, query)
	if field in NUMERIC_COLUMN_FIELDS:
		return _numeric_matches(value, query)
	return _text_matches(value, query)


def _text_matches(value, query: str) -> bool:
	hay = cstr(value).casefold()
	needle = cstr(query).casefold()
	if not needle:
		return True
	# Prefix "=" for exact match.
	if needle.startswith("="):
		return hay == needle[1:].strip()
	return needle in hay


def _numeric_matches(value, query: str) -> bool:
	text = cstr(query).strip()
	if not text:
		return True
	try:
		amount = flt(value)
	except Exception:
		amount = 0.0
	# Range: min..max or min,max
	for sep in ("..", ",", ":"):
		if sep in text:
			left, _, right = text.partition(sep)
			lo = flt(left) if left.strip() else None
			hi = flt(right) if right.strip() else None
			if lo is not None and amount < lo:
				return False
			if hi is not None and amount > hi:
				return False
			return True
	if text.startswith(">="):
		return amount >= flt(text[2:])
	if text.startswith("<="):
		return amount <= flt(text[2:])
	if text.startswith(">"):
		return amount > flt(text[1:])
	if text.startswith("<"):
		return amount < flt(text[1:])
	if text.startswith("="):
		return abs(amount - flt(text[1:])) < 0.0000001
	# Partial digit match against formatted string OR exact equality.
	if abs(amount - flt(text)) < 0.0000001:
		return True
	return text in cstr(value)


def _date_matches(value, query: str) -> bool:
	text = cstr(query).strip()
	if not text:
		return True
	try:
		row_date = getdate(value) if value else None
	except Exception:
		row_date = None
	if row_date is None:
		return False
	# Range start..end
	if ".." in text:
		left, _, right = text.partition("..")
		lo = _parse_date(left.strip()) if left.strip() else None
		hi = _parse_date(right.strip()) if right.strip() else None
		if lo and row_date < lo:
			return False
		if hi and row_date > hi:
			return False
		return True
	target = _parse_date(text)
	if target:
		return row_date == target
	return text in cstr(value)


def _parse_date(text: str):
	try:
		return getdate(text)
	except Exception:
		return None


def format_running_balance_display(running_net) -> str:
	"""Signed net → ``100 Dr`` / ``20 Cr`` / ``0``."""
	net = flt(running_net)
	if abs(net) < 0.0000001:
		return "0"
	if net > 0:
		return f"{flt(net)} Dr"
	return f"{abs(flt(net))} Cr"


def apply_column_filters_to_gle_query(query, gle, acc, spec) -> object:
	"""Push voucher-axis column filters into the GL Entry query where possible."""
	filters = normalize_column_filters(getattr(spec, "column_filters", None) or {})
	if not filters:
		return query

	text_map = {
		"voucher_type": gle.voucher_type,
		"voucher_no": gle.voucher_no,
		"gl_entry": gle.name,
		"account": gle.account,
		"party_type": gle.party_type,
		"party": gle.party,
		"remarks": gle.remarks,
		"description": gle.remarks,
		"cost_center": gle.cost_center,
		"finance_book": gle.finance_book,
		"against_account": gle.against,
		"against_voucher_type": gle.against_voucher_type,
		"against_voucher": gle.against_voucher,
		"account_currency": gle.account_currency,
		"is_opening": gle.is_opening,
	}
	acc_text_map = {
		"account_code": acc.account_number,
		"account_name": acc.account_name,
	}
	numeric_map = {
		"debit": gle.debit,
		"credit": gle.credit,
		"debit_in_account_currency": gle.debit_in_account_currency,
		"credit_in_account_currency": gle.credit_in_account_currency,
	}

	for field, raw in filters.items():
		if field == "posting_date":
			query = _apply_date_sql_filter(query, gle.posting_date, raw)
			continue
		if field == "party_name":
			# Party name is enriched post-query; keep as post-filter marker.
			continue
		if field in numeric_map:
			query = _apply_numeric_sql_filter(query, numeric_map[field], raw)
			continue
		if field in text_map:
			query = _apply_text_sql_filter(query, text_map[field], raw)
			continue
		if field in acc_text_map:
			query = _apply_text_sql_filter(query, acc_text_map[field], raw)
			continue
		# Dimension columns: dim:cost_center or bare field on gle
		dim_field = field[4:] if field.startswith("dim:") else field
		if hasattr(gle, dim_field):
			query = _apply_text_sql_filter(query, getattr(gle, dim_field), raw)
	return query


def post_filter_voucher_rows(rows: list[dict], spec) -> list[dict]:
	"""Apply filters that cannot be pushed to SQL (e.g. party_name display)."""
	filters = normalize_column_filters(getattr(spec, "column_filters", None) or {})
	if not filters:
		return rows
	# running_balance is filtered in the windowed SQL before LIMIT (see voucher_gl_lines).
	pending = {k: v for k, v in filters.items() if k in {"party_name"}}
	if not pending:
		return rows
	return apply_column_filters_to_rows(rows, pending)


def running_balance_sql_predicate(raw: str, column_alias: str = "running_net") -> str | None:
	"""Return a SQL boolean predicate for numeric running-balance column filters."""
	text = cstr(raw).strip()
	if not text:
		return None
	col = column_alias
	for sep in ("..", ",", ":"):
		if sep in text:
			left, _, right = text.partition(sep)
			parts = []
			if left.strip():
				parts.append(f"{col} >= {flt(left)}")
			if right.strip():
				parts.append(f"{col} <= {flt(right)}")
			return " and ".join(parts) if parts else None
	if text.startswith(">="):
		return f"{col} >= {flt(text[2:])}"
	if text.startswith("<="):
		return f"{col} <= {flt(text[2:])}"
	if text.startswith(">"):
		return f"{col} > {flt(text[1:])}"
	if text.startswith("<"):
		return f"{col} < {flt(text[1:])}"
	if text.startswith("="):
		return f"abs({col} - {flt(text[1:])}) < 0.0000001"
	return f"abs({col} - {flt(text)}) < 0.0000001"


def _apply_text_sql_filter(query, column, raw: str):
	text = cstr(raw).strip()
	if not text:
		return query
	if text.startswith("="):
		return query.where(column == text[1:].strip())
	return query.where(column.like(f"%{text}%"))


def _apply_numeric_sql_filter(query, column, raw: str):
	text = cstr(raw).strip()
	if not text:
		return query
	for sep in ("..", ",", ":"):
		if sep in text:
			left, _, right = text.partition(sep)
			if left.strip():
				query = query.where(column >= flt(left))
			if right.strip():
				query = query.where(column <= flt(right))
			return query
	if text.startswith(">="):
		return query.where(column >= flt(text[2:]))
	if text.startswith("<="):
		return query.where(column <= flt(text[2:]))
	if text.startswith(">"):
		return query.where(column > flt(text[1:]))
	if text.startswith("<"):
		return query.where(column < flt(text[1:]))
	if text.startswith("="):
		return query.where(column == flt(text[1:]))
	return query.where(column == flt(text))


def _apply_date_sql_filter(query, column, raw: str):
	text = cstr(raw).strip()
	if not text:
		return query
	if ".." in text:
		left, _, right = text.partition("..")
		if left.strip():
			query = query.where(column >= getdate(left.strip()))
		if right.strip():
			query = query.where(column <= getdate(right.strip()))
		return query
	return query.where(column == getdate(text))

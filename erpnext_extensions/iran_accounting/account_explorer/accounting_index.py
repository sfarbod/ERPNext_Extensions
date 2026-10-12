# Copyright (c) 2026, Farbod Siyahpoosh and contributors
"""Redis accounting index prototype. Disabled unless site config enables it.

GL remains the source of truth. This module is not called from
``get_account_summary``. Daily leaf turnover is stored so a posting-date
change invalidates that account-date only; parent balances and later dates
are derived at read time.

Enable for a development experiment with ``ae_accounting_index = 1`` in
site config. The default is off.
"""

from __future__ import annotations

import hashlib
from decimal import Decimal

import frappe
import redis
from frappe.utils import cint, getdate

SCHEMA_VERSION = "1"
KEY_PREFIX = "ae:idx:v1"
CONFIG_KEY = "ae_accounting_index"
# Secondary safety only. A missing key is a miss, never a stale balance.
INDEX_TTL_SECONDS = 7 * 24 * 3600
PCV_VOUCHER_TYPE = "Period Closing Voucher"

_PUBLISH_LUA = """
local current = redis.call('HGET', KEYS[2], ARGV[1])
if current == false then
  current = '0'
end
if current == ARGV[2] then
  redis.call('HSET', KEYS[1], ARGV[1], ARGV[3])
  redis.call('HDEL', KEYS[2], ARGV[1])
  return 1
end
return 0
"""


def index_enabled() -> bool:
	return bool(cint(getattr(frappe.conf, CONFIG_KEY, 0)))


def _token(company: str) -> str:
	return hashlib.sha1(company.encode()).hexdigest()[:10]


def _keys(company: str) -> dict[str, str]:
	base = f"{frappe.conf.db_name}|{KEY_PREFIX}:{_token(company)}"
	return {
		"data": f"{base}:data",
		"stamp": f"{base}:stamp",
		"meta": f"{base}:meta",
		"lock": f"{base}:lock",
	}


def _client():
	return redis.Redis.from_url(frappe.conf.redis_cache, decode_responses=True)


def segment_field(account: str, finance_book: str | None, posting_date) -> str:
	book = finance_book or ""
	return f"{account}\x1f{book}\x1f{getdate(posting_date).isoformat()}"


def parse_segment_field(field: str) -> tuple[str, str, str]:
	account, book, day = field.split("\x1f")
	return account, book, day


def segments_for_gl_rows(rows: list[dict]) -> list[str]:
	"""Leaf account-date fields touched by GL rows. Parents are not fields."""
	fields = []
	seen = set()
	for row in rows or []:
		if not row.get("account") or not row.get("posting_date"):
			continue
		field = segment_field(row.get("account"), row.get("finance_book"), row.get("posting_date"))
		if field not in seen:
			seen.add(field)
			fields.append(field)
	return fields


def _status(status: str, **extra) -> dict:
	payload = {"status": status, "enabled": index_enabled()}
	payload.update(extra)
	return payload


def get_turnover(
	company: str,
	accounts: list[str],
	from_date,
	to_date,
	*,
	finance_books: list[str] | None = None,
	include_period_closing: bool = False,
	include_cancelled: bool = False,
	dimensions: dict | None = None,
) -> dict:
	"""Read daily turnover. ``ready`` is the only status that carries balances."""
	if not index_enabled():
		return _status("disabled")
	if include_period_closing or include_cancelled or dimensions:
		return _status("unsupported")
	try:
		cache = _client()
		keys = _keys(company)
		meta = cache.hgetall(keys["meta"]) or {}
		schema = meta.get("schema")
		if schema != SCHEMA_VERSION:
			return _status("incompatible" if schema else "missing")
		allowed = set(finance_books if finance_books is not None else [""])
		account_set = set(accounts)
		start = getdate(from_date)
		end = getdate(to_date)
		for field in cache.hkeys(keys["stamp"]) or []:
			account, book, day = parse_segment_field(field)
			if account not in account_set or book not in allowed:
				continue
			if getdate(day) <= end:
				return _status("incomplete", reason="segment_rebuilding")
		opening_debit = Decimal("0")
		opening_credit = Decimal("0")
		period_debit = Decimal("0")
		period_credit = Decimal("0")
		for field, raw_value in (cache.hgetall(keys["data"]) or {}).items():
			account, book, day = parse_segment_field(field)
			if account not in account_set or book not in allowed:
				continue
			debit_s, credit_s = raw_value.split("|")
			debit = Decimal(debit_s)
			credit = Decimal(credit_s)
			posted = getdate(day)
			if posted < start:
				opening_debit += debit
				opening_credit += credit
			elif posted <= end:
				period_debit += debit
				period_credit += credit
		net = (opening_debit - opening_credit) + (period_debit - period_credit)
		return _status(
			"ready",
			opening_debit=opening_debit,
			opening_credit=opening_credit,
			period_debit=period_debit,
			period_credit=period_credit,
			debit_balance=max(net, Decimal("0")),
			credit_balance=abs(min(net, Decimal("0"))),
		)
	except redis.exceptions.RedisError:
		return _status("unavailable")


def rebuild_company(company: str) -> dict:
	"""Replace this company's index from posted GL. Other companies are untouched."""
	if not index_enabled():
		return _status("disabled")
	cache = _client()
	keys = _keys(company)
	if not cache.set(keys["lock"], "1", nx=True, ex=120):
		return _status("busy")
	try:
		cache.hset(keys["meta"], mapping={"schema": "rebuilding", "company_token": _token(company)})
		rows = frappe.db.sql(
			"""
			select account, ifnull(finance_book, '') as finance_book, posting_date,
				sum(debit) as debit, sum(credit) as credit
			from `tabGL Entry`
			where company = %s and is_cancelled = 0 and voucher_type != %s
			group by account, ifnull(finance_book, ''), posting_date
			""",
			(company, PCV_VOUCHER_TYPE),
			as_dict=True,
		)
		cache.delete(keys["data"], keys["stamp"])
		mapping = {}
		for row in rows:
			debit = Decimal(str(row.debit or 0))
			credit = Decimal(str(row.credit or 0))
			if debit == 0 and credit == 0:
				continue
			field = segment_field(row.account, row.finance_book, row.posting_date)
			mapping[field] = f"{format(debit, 'f')}|{format(credit, 'f')}"
		pipe = cache.pipeline()
		if mapping:
			pipe.hset(keys["data"], mapping=mapping)
		pipe.hset(
			keys["meta"],
			mapping={"schema": SCHEMA_VERSION, "company_token": _token(company)},
		)
		pipe.expire(keys["data"], INDEX_TTL_SECONDS)
		pipe.expire(keys["stamp"], INDEX_TTL_SECONDS)
		pipe.expire(keys["meta"], INDEX_TTL_SECONDS)
		pipe.execute()
		memory = cache.memory_usage(keys["data"]) or 0
		return _status("ready", fields=len(mapping), memory_bytes=int(memory))
	except redis.exceptions.RedisError:
		return _status("unavailable")
	finally:
		try:
			cache.delete(keys["lock"])
		except redis.exceptions.RedisError:
			pass


def invalidate_segments(company: str, fields: list[str]) -> dict:
	"""Drop only the given account-date fields. Unrelated fields stay."""
	if not index_enabled():
		return _status("disabled")
	if not fields:
		return _status("ready", invalidated=0)
	try:
		cache = _client()
		keys = _keys(company)
		pipe = cache.pipeline()
		for field in fields:
			pipe.hincrby(keys["stamp"], field, 1)
			pipe.hdel(keys["data"], field)
		pipe.expire(keys["stamp"], INDEX_TTL_SECONDS)
		results = pipe.execute()
		stamps = {field: int(results[index * 2]) for index, field in enumerate(fields)}
		return _status("incomplete", invalidated=len(fields), stamps=stamps)
	except redis.exceptions.RedisError:
		return _status("unavailable")


def publish_segment(company: str, field: str, debit, credit, expected_stamp: int) -> bool:
	"""Write one segment only if its stamp is unchanged. Stale writers lose."""
	if not index_enabled():
		return False
	cache = _client()
	keys = _keys(company)
	value = f"{Decimal(str(debit))}|{Decimal(str(credit))}"
	written = cache.eval(
		_PUBLISH_LUA,
		2,
		keys["data"],
		keys["stamp"],
		field,
		str(expected_stamp),
		value,
	)
	return bool(int(written or 0))


def drop_company_index(company: str) -> None:
	"""Remove only this company's index keys."""
	cache = _client()
	keys = _keys(company)
	cache.delete(keys["data"], keys["stamp"], keys["meta"], keys["lock"])

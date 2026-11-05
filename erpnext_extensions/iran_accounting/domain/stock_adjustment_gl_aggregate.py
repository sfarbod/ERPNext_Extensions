# Copyright (c) 2026, ERPNext Extensions contributors
"""Adjustment-account-only same-side GL aggregation.

Iran Accounting may collapse multiple same-side legs **only** for configured
adjustment accounts:

* ``Company.stock_adjustment_account`` (e.g. 621301)
* ``Company.round_off_account`` (financial / rate rounding)

All other accounts (WIP, FG, inventory, scrap, expense, revenue, tax, party,
…) are left exactly as ERPNext Core produced them. This module never merges
Debit against Credit and never rewrites non-allowlisted rows.
"""

from __future__ import annotations

import json
from typing import Any

import frappe
from frappe.utils import flt

from erpnext.accounts.doctype.accounting_dimension.accounting_dimension import (
	get_accounting_dimensions,
)
from erpnext_extensions.iran_accounting.domain.currency import (
	get_company_currency,
	is_irr_company,
	round_currency,
)

_AGGREGATION_FIELDS = (
	"company",
	"voucher_type",
	"voucher_no",
	"account",
	"account_currency",
	"cost_center",
	"project",
	"finance_book",
	"party_type",
	"party",
	"posting_date",
	"transaction_currency",
)


def _g(entry: Any, key: str, default=None):
	if hasattr(entry, "get"):
		val = entry.get(key, default)
	else:
		val = getattr(entry, key, default)
	if val is None:
		return default if default is not None else ""
	return val


def _s(entry: Any, key: str, val) -> None:
	if hasattr(entry, "__setitem__"):
		entry[key] = val
	else:
		setattr(entry, key, val)


def _copy_entry(entry: Any) -> Any:
	if hasattr(entry, "items"):
		return frappe._dict(dict(entry))
	if hasattr(entry, "copy"):
		return entry.copy()
	return frappe._dict(entry)


def get_adjustable_gl_accounts(company: str) -> set[str]:
	"""Explicit allowlist of accounts eligible for Iran same-side aggregation."""
	if not company:
		return set()
	accounts: set[str] = set()
	sa = frappe.get_cached_value("Company", company, "stock_adjustment_account")
	if sa:
		accounts.add(sa)
	ro = frappe.get_cached_value("Company", company, "round_off_account")
	if ro:
		accounts.add(ro)
	return accounts


def is_adjustable_gl_account(company: str, account: str | None) -> bool:
	if not account:
		return False
	return account in get_adjustable_gl_accounts(company)


def entry_accounting_side(entry: Any) -> str | None:
	"""Return 'debit' / 'credit', or None when mixed / zero."""
	debit = flt(_g(entry, "debit"))
	credit = flt(_g(entry, "credit"))
	if abs(debit) > 0 and abs(credit) > 0:
		return None
	if abs(debit) > 0:
		return "debit"
	if abs(credit) > 0:
		return "credit"
	dac = flt(_g(entry, "debit_in_account_currency"))
	cac = flt(_g(entry, "credit_in_account_currency"))
	if abs(dac) > 0 and abs(cac) > 0:
		return None
	if abs(dac) > 0:
		return "debit"
	if abs(cac) > 0:
		return "credit"
	return None


def is_one_sided_gl_entry(entry: Any, *, require_non_negative: bool = False) -> bool:
	debit = flt(_g(entry, "debit"))
	credit = flt(_g(entry, "credit"))
	if abs(debit) > 0 and abs(credit) > 0:
		return False
	dac = flt(_g(entry, "debit_in_account_currency"))
	cac = flt(_g(entry, "credit_in_account_currency"))
	if abs(dac) > 0 and abs(cac) > 0:
		return False
	dtx = flt(_g(entry, "debit_in_transaction_currency"))
	ctx = flt(_g(entry, "credit_in_transaction_currency"))
	if abs(dtx) > 0 and abs(ctx) > 0:
		return False
	if require_non_negative:
		if debit < 0 or credit < 0 or dac < 0 or cac < 0 or dtx < 0 or ctx < 0:
			return False
	return True


def _norm_key_val(entry: Any, field: str) -> str:
	val = _g(entry, field, "")
	if val is None or val == "":
		if field == "transaction_currency":
			val = _g(entry, "account_currency", "") or ""
		else:
			val = ""
	return str(val)


def sa_aggregation_key(entry: Any, dimension_fields: list[str], side: str | None = None) -> tuple:
	parts = [_norm_key_val(entry, field) for field in _AGGREGATION_FIELDS]
	for field in dimension_fields:
		if field == "cost_center":
			continue
		parts.append(_norm_key_val(entry, field))
	if side is None:
		side = entry_accounting_side(entry) or ""
	parts.append(str(side))
	return tuple(parts)


def _classify_adjustment_source(entry: Any) -> dict[str, Any]:
	remarks = str(_g(entry, "remarks", "") or "")
	debit = flt(_g(entry, "debit"))
	credit = flt(_g(entry, "credit"))
	signed = credit - debit
	if _g(entry, "_irr_sabe_localization") or "IRR_SABE_A2|" in remarks:
		source = "iran_a2"
	elif "IRR stock-valuation currency-precision residual" in remarks:
		source = "iran_precision_residual"
	elif "IRR_RATE_ROUNDING" in remarks or "rate rounding" in remarks.lower():
		source = "iran_rate_rounding"
	elif _g(entry, "_irr_inventory_valuation_residual") and "IRR" in remarks:
		source = "iran_residual"
	elif "Round Off" in remarks or "round off" in remarks.lower():
		source = "core_round_off"
	else:
		source = "core_stock_adjustment"
	return {
		"source": source,
		"debit": debit,
		"credit": credit,
		"signed_credit_minus_debit": signed,
		"side": entry_accounting_side(entry),
		"remarks": remarks[:240],
		"idempotency": (
			remarks[remarks.find("IRR_SABE_A2|") :][:180]
			if "IRR_SABE_A2|" in remarks
			else None
		),
	}


def _aggregate_allowlisted_account(
	gl_map: list,
	*,
	account: str,
	ccy: str,
	dimensions: list[str],
) -> tuple[list, list]:
	"""Same-side aggregate rows for one allowlisted account. Returns (map, audits)."""
	indices = [i for i, e in enumerate(gl_map) if _g(e, "account") == account]
	if len(indices) <= 1:
		return gl_map, []

	groups: dict[tuple, list[tuple[int, Any]]] = {}
	for i in indices:
		side = entry_accounting_side(gl_map[i])
		if not side:
			# Mixed adjustment row: leave untouched rather than inventing Core edits.
			continue
		key = sa_aggregation_key(gl_map[i], dimensions, side=side)
		groups.setdefault(key, []).append((i, gl_map[i]))

	if all(len(members) == 1 for members in groups.values()):
		return gl_map, []

	drop: set[int] = set()
	replacements: list[tuple[int, Any]] = []
	audits: list[dict] = []

	for key, members in groups.items():
		if len(members) == 1:
			continue
		side = key[-1]
		sources = []
		debit = credit = 0.0
		dac = cac = 0.0
		dtx = ctx = 0.0
		anchor_idx, anchor = members[0]
		for idx, entry in members:
			drop.add(idx)
			src = _classify_adjustment_source(entry)
			sources.append(src)
			debit += flt(_g(entry, "debit"))
			credit += flt(_g(entry, "credit"))
			dac += flt(_g(entry, "debit_in_account_currency"))
			cac += flt(_g(entry, "credit_in_account_currency"))
			dtx += flt(_g(entry, "debit_in_transaction_currency"))
			ctx += flt(_g(entry, "credit_in_transaction_currency"))

		if side == "debit":
			agg_debit = float(round_currency(debit, ccy))
			agg_credit = 0.0
			agg_dac = float(round_currency(dac, ccy))
			agg_cac = 0.0
			agg_dtx = float(round_currency(dtx, ccy))
			agg_ctx = 0.0
		else:
			agg_debit = 0.0
			agg_credit = float(round_currency(credit, ccy))
			agg_dac = 0.0
			agg_cac = float(round_currency(cac, ccy))
			agg_dtx = 0.0
			agg_ctx = float(round_currency(ctx, ccy))

		audits.append(
			{
				"account": account,
				"side": side,
				"aggregation_key": list(key),
				"components": sources,
				"gross_debit": float(round_currency(debit, ccy)),
				"gross_credit": float(round_currency(credit, ccy)),
				"agg_debit": agg_debit,
				"agg_credit": agg_credit,
			}
		)

		if abs(agg_debit) < 0.5 and abs(agg_credit) < 0.5:
			continue

		agg = _copy_entry(anchor)
		_s(agg, "debit", agg_debit)
		_s(agg, "credit", agg_credit)
		_s(agg, "debit_in_account_currency", agg_dac)
		_s(agg, "credit_in_account_currency", agg_cac)
		_s(agg, "debit_in_transaction_currency", agg_dtx)
		_s(agg, "credit_in_transaction_currency", agg_ctx)
		_s(agg, "account_currency", _g(agg, "account_currency") or ccy)
		_s(agg, "transaction_currency", _g(agg, "transaction_currency") or ccy)
		_s(agg, "voucher_detail_no", None)
		_s(agg, "_irr_adjustment_aggregated", 1)
		_s(agg, "_irr_sa_aggregated", 1)  # backward-compatible marker
		_s(agg, "_irr_sa_source_components", sources)
		if hasattr(agg, "pop"):
			agg.pop("post_net_value", None)
		summary_bits = [
			f"{s['source']}={s['signed_credit_minus_debit']:+.0f}" for s in sources
		]
		payload = {
			"iran_adjustment_aggregate": True,
			"account": account,
			"side": side,
			"agg_credit": agg_credit,
			"agg_debit": agg_debit,
			"components": sources,
		}
		_s(
			agg,
			"remarks",
			(
				f"IRR aggregated adjustment account={account} side={side} "
				f"agg_credit={agg_credit} agg_debit={agg_debit} "
				f"[{', '.join(summary_bits)}] "
				f"IRR_SA_AGG|{json.dumps(payload, ensure_ascii=False, default=str)[:1500]}"
			),
		)
		if hasattr(agg, "pop"):
			agg.pop("_irr_sabe_localization", None)
		replacements.append((anchor_idx, agg))

	if not drop and not replacements:
		return gl_map, audits

	new_map: list = []
	replaced_at = {idx: entry for idx, entry in replacements}
	for i, entry in enumerate(gl_map):
		if i in replaced_at:
			new_map.append(replaced_at[i])
			continue
		if i in drop:
			continue
		new_map.append(entry)
	return new_map, audits


def aggregate_adjustment_gl_entries(doc, gl_map: list | None) -> list | None:
	"""Same-side aggregate **only** allowlisted adjustment accounts.

	Non-adjustment accounts are returned unchanged — no merge, net, split, or
	dimension rewrite.
	"""
	if not gl_map:
		return gl_map
	company = None
	if doc is not None:
		company = getattr(doc, "company", None)
	if not company:
		company = _g(gl_map[0], "company")
	if not company or not is_irr_company(company):
		return gl_map

	allowlist = get_adjustable_gl_accounts(company)
	if not allowlist:
		return gl_map

	ccy = get_company_currency(company)
	dimensions = [d for d in get_accounting_dimensions() if d != "cost_center"]
	all_audits: list[dict] = []
	active = 0
	out = gl_map
	for account in sorted(allowlist):
		out, audits = _aggregate_allowlisted_account(
			out, account=account, ccy=ccy, dimensions=dimensions
		)
		all_audits.extend(audits)
		active += sum(1 for a in audits if abs(a.get("agg_debit") or 0) >= 0.5 or abs(a.get("agg_credit") or 0) >= 0.5)

	if doc is not None:
		doc.flags = getattr(doc, "flags", None) or frappe._dict()
		doc.flags.iran_sa_aggregate = {
			"allowlist": sorted(allowlist),
			"groups": all_audits,
			"active_aggregated_rows": active,
		}
	return out


# Backward-compatible alias used by existing call sites / tests.
aggregate_stock_adjustment_gl_entries = aggregate_adjustment_gl_entries


def align_sabe_a2_sa_dimensions_with_in_map(doc, gl_map: list, sa_account: str) -> dict:
	"""Prefer existing in-map SA dimensions so A2 can aggregate with Core SA."""
	dims = {}
	for e in gl_map or []:
		if _g(e, "account") != sa_account:
			continue
		if _g(e, "_irr_sabe_localization"):
			continue
		dims["cost_center"] = _g(e, "cost_center") or dims.get("cost_center")
		for d in get_accounting_dimensions():
			if d == "cost_center":
				continue
			val = _g(e, d)
			if val:
				dims[d] = val
		if dims.get("cost_center"):
			break
	if not dims.get("cost_center") and doc.get("doctype") == "Stock Entry":
		for row in doc.get("items") or []:
			if row.get("cost_center"):
				dims["cost_center"] = row.get("cost_center")
				break
	return dims


# Retained for unit tests that assert split helpers; never applied to full GL maps.
def split_mixed_side_gl_entries(gl_map: list | None) -> list | None:
	"""Test/helper only — not wired into Core merge for non-adjustment accounts."""
	if not gl_map:
		return gl_map
	out: list = []
	for entry in gl_map:
		debit = flt(_g(entry, "debit"))
		credit = flt(_g(entry, "credit"))
		dac = flt(_g(entry, "debit_in_account_currency"))
		cac = flt(_g(entry, "credit_in_account_currency"))
		dtx = flt(_g(entry, "debit_in_transaction_currency"))
		ctx = flt(_g(entry, "credit_in_transaction_currency"))
		if not (
			(abs(debit) > 0 and abs(credit) > 0)
			or (abs(dac) > 0 and abs(cac) > 0)
			or (abs(dtx) > 0 and abs(ctx) > 0)
		):
			out.append(entry)
			continue
		base = dict(entry) if hasattr(entry, "items") else dict(_copy_entry(entry))
		debit_amt = debit if abs(debit) > 0 else dac
		credit_amt = credit if abs(credit) > 0 else cac
		if abs(debit_amt) > 0:
			row = frappe._dict(base)
			row.update(
				{
					"debit": debit_amt,
					"credit": 0.0,
					"debit_in_account_currency": dac if abs(dac) > 0 else debit_amt,
					"credit_in_account_currency": 0.0,
					"debit_in_transaction_currency": dtx if abs(dtx) > 0 else debit_amt,
					"credit_in_transaction_currency": 0.0,
				}
			)
			out.append(row)
		if abs(credit_amt) > 0:
			row = frappe._dict(base)
			row.update(
				{
					"debit": 0.0,
					"credit": credit_amt,
					"debit_in_account_currency": 0.0,
					"credit_in_account_currency": cac if abs(cac) > 0 else credit_amt,
					"debit_in_transaction_currency": 0.0,
					"credit_in_transaction_currency": ctx if abs(ctx) > 0 else credit_amt,
				}
			)
			out.append(row)
	return out


def ensure_one_sided_gl_entries(gl_map: list | None, *, require_non_negative: bool = False) -> list | None:
	"""Test/helper only — not applied to the full voucher GL map."""
	gl_map = split_mixed_side_gl_entries(gl_map)
	for entry in gl_map or []:
		if not is_one_sided_gl_entry(entry, require_non_negative=require_non_negative):
			frappe.throw(
				(
					f"GL Entry for {_g(entry, 'account')} has both Debit and Credit amounts. "
					"Each adjustment GL row must be one-sided."
				),
				title="Mixed Debit/Credit GL Entry",
			)
	return gl_map

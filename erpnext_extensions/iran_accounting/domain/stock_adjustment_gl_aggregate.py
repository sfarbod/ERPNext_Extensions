# Copyright (c) 2026, ERPNext Extensions contributors
"""Same-side Stock Adjustment GL aggregation + one-sided row enforcement.

Multiple sources (Core Manufacture SA, Iran A2, ±1 IRR precision residual) may
append separate Company.stock_adjustment_account rows. Compatible **same-side**
legs (debit-only with debit-only, credit-only with credit-only) collapse into
one GL row before persistence. Debit and Credit are never netted into a mixed
or net-balance row.

Also splits any mixed-side GL Entry (debit>0 AND credit>0) into two one-sided
rows so Core ``merge_similar_entries`` cannot persist a forbidden presentation
for stock vouchers.
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

# Fields that must match for SA aggregation (remarks / voucher_detail_no excluded).
_SA_AGGREGATION_FIELDS = (
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


def entry_accounting_side(entry: Any) -> str | None:
	"""Return 'debit', 'credit', or None for a zero / invalid mixed row.

	Pre-toggle Core maps may carry a negative amount on a single side; that is
	still one-sided. Mixed means both sides are non-zero (any sign).
	"""
	debit = flt(_g(entry, "debit"))
	credit = flt(_g(entry, "credit"))
	if abs(debit) > 0 and abs(credit) > 0:
		return None  # mixed — must be split before aggregation
	if abs(debit) > 0:
		return "debit"
	if abs(credit) > 0:
		return "credit"
	# Fall back to account-currency mirrors when company amounts are zeroed.
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
	"""True when at most one side is non-zero on company/account/transaction amounts.

	``require_non_negative`` enforces debit>=0 and credit>=0 (post-toggle /
	pre-persist). During ``merge_similar_entries`` Core may still have a
	negative single-side amount awaiting ``toggle_debit_credit_if_negative``.
	"""
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
		# Fall back so Core IRR legs (transaction_currency set) match A2 legs
		# that omit the mirror until make_entry.
		if field == "transaction_currency":
			val = _g(entry, "account_currency", "") or ""
		else:
			val = ""
	return str(val)


def sa_aggregation_key(entry: Any, dimension_fields: list[str], side: str | None = None) -> tuple:
	parts = []
	for field in _SA_AGGREGATION_FIELDS:
		parts.append(_norm_key_val(entry, field))
	for field in dimension_fields:
		if field in ("cost_center",):
			continue
		parts.append(_norm_key_val(entry, field))
	if side is None:
		side = entry_accounting_side(entry) or ""
	parts.append(str(side))
	return tuple(parts)


def _classify_sa_source(entry: Any) -> dict[str, Any]:
	remarks = str(_g(entry, "remarks", "") or "")
	debit = flt(_g(entry, "debit"))
	credit = flt(_g(entry, "credit"))
	signed = credit - debit  # positive = credit-side SA
	if _g(entry, "_irr_sabe_localization") or "IRR_SABE_A2|" in remarks:
		source = "iran_a2"
	elif "IRR stock-valuation currency-precision residual" in remarks:
		source = "iran_precision_residual"
	elif _g(entry, "_irr_inventory_valuation_residual") and "IRR" in remarks:
		source = "iran_residual"
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


def split_mixed_side_gl_entries(gl_map: list | None) -> list | None:
	"""Split any debit>0 AND credit>0 row into two one-sided rows.

	Preserves amounts, dimensions, and remarks on both halves. Does not net.
	"""
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
		company_mixed = abs(debit) > 0 and abs(credit) > 0
		acct_mixed = abs(dac) > 0 and abs(cac) > 0
		trx_mixed = abs(dtx) > 0 and abs(ctx) > 0
		if not (company_mixed or acct_mixed or trx_mixed):
			out.append(entry)
			continue

		# Build halves from a plain _dict so mutations cannot leak across sides.
		base = dict(entry) if hasattr(entry, "items") else dict(_copy_entry(entry))
		debit_amt = debit if abs(debit) > 0 else (dac if abs(dac) > 0 else 0.0)
		credit_amt = credit if abs(credit) > 0 else (cac if abs(cac) > 0 else 0.0)
		dac_amt = dac if abs(dac) > 0 else (debit_amt if abs(debit_amt) > 0 else 0.0)
		cac_amt = cac if abs(cac) > 0 else (credit_amt if abs(credit_amt) > 0 else 0.0)
		dtx_amt = dtx if abs(dtx) > 0 else (debit_amt if abs(debit_amt) > 0 else 0.0)
		ctx_amt = ctx if abs(ctx) > 0 else (credit_amt if abs(credit_amt) > 0 else 0.0)

		if abs(debit_amt) > 0 or abs(dac_amt) > 0:
			debit_row = frappe._dict(base)
			debit_row.update(
				{
					"debit": debit_amt,
					"credit": 0.0,
					"debit_in_account_currency": dac_amt,
					"credit_in_account_currency": 0.0,
					"debit_in_transaction_currency": dtx_amt,
					"credit_in_transaction_currency": 0.0,
					"_irr_split_from_mixed": 1,
				}
			)
			out.append(debit_row)
		if abs(credit_amt) > 0 or abs(cac_amt) > 0:
			credit_row = frappe._dict(base)
			credit_row.update(
				{
					"debit": 0.0,
					"credit": credit_amt,
					"debit_in_account_currency": 0.0,
					"credit_in_account_currency": cac_amt,
					"debit_in_transaction_currency": 0.0,
					"credit_in_transaction_currency": ctx_amt,
					"_irr_split_from_mixed": 1,
				}
			)
			out.append(credit_row)
	return out


def ensure_one_sided_gl_entries(
	gl_map: list | None, *, require_non_negative: bool = False
) -> list | None:
	"""Split mixed-side rows (retry once); throw only if a mixed row remains.

	During merge (pre-toggle) keep ``require_non_negative=False`` so a single
	negative side is allowed. Before persist, call with ``True``.
	"""
	gl_map = split_mixed_side_gl_entries(gl_map)
	# Second pass guards against exotic entry types that resisted the first copy.
	if any(
		not is_one_sided_gl_entry(e, require_non_negative=require_non_negative)
		for e in (gl_map or [])
	):
		gl_map = split_mixed_side_gl_entries(gl_map)
	for entry in gl_map or []:
		if not is_one_sided_gl_entry(entry, require_non_negative=require_non_negative):
			account = _g(entry, "account")
			frappe.throw(
				(
					f"GL Entry for {account} has both Debit and Credit amounts. "
					"Each stock-voucher GL row must be one-sided."
				),
				title="Mixed Debit/Credit GL Entry",
			)
	return gl_map


def aggregate_stock_adjustment_gl_entries(doc, gl_map: list | None) -> list | None:
	"""Same-side merge of Company.stock_adjustment_account legs.

	Debit legs aggregate only with debit legs; credit only with credit.
	Opposite sides on the same account remain separate rows (never netted).
	Non-SA accounts are left untouched.
	"""
	if not gl_map or not doc:
		return gl_map
	company = getattr(doc, "company", None) or _g(gl_map[0], "company")
	if not company or not is_irr_company(company):
		return gl_map

	# Split any accidental mixed SA/other rows before grouping.
	gl_map = ensure_one_sided_gl_entries(gl_map)

	sa = frappe.get_cached_value("Company", company, "stock_adjustment_account")
	if not sa:
		return gl_map

	ccy = get_company_currency(company)
	dimensions = [d for d in get_accounting_dimensions() if d != "cost_center"]

	sa_indices = [i for i, e in enumerate(gl_map) if _g(e, "account") == sa]
	if len(sa_indices) <= 1:
		return gl_map

	groups: dict[tuple, list[tuple[int, Any]]] = {}
	for i in sa_indices:
		side = entry_accounting_side(gl_map[i])
		if not side:
			continue
		key = sa_aggregation_key(gl_map[i], dimensions, side=side)
		groups.setdefault(key, []).append((i, gl_map[i]))

	if all(len(members) == 1 for members in groups.values()):
		return gl_map

	drop: set[int] = set()
	replacements: list[tuple[int, Any]] = []
	audit_groups = []

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
			src = _classify_sa_source(entry)
			sources.append(src)
			debit += flt(_g(entry, "debit"))
			credit += flt(_g(entry, "credit"))
			dac += flt(_g(entry, "debit_in_account_currency"))
			cac += flt(_g(entry, "credit_in_account_currency"))
			dtx += flt(_g(entry, "debit_in_transaction_currency"))
			ctx += flt(_g(entry, "credit_in_transaction_currency"))

		# Same-side only — never subtract opposite sides.
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

		audit = {
			"account": sa,
			"side": side,
			"aggregation_key": list(key),
			"components": sources,
			"gross_debit": float(round_currency(debit, ccy)),
			"gross_credit": float(round_currency(credit, ccy)),
			"agg_debit": agg_debit,
			"agg_credit": agg_credit,
		}
		audit_groups.append(audit)

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
		_s(agg, "voucher_detail_no", None)
		_s(agg, "_irr_sa_aggregated", 1)
		_s(agg, "_irr_sa_source_components", sources)
		# Do not set post_net_value — that flag invites Core to net opposite sides.
		if hasattr(agg, "pop"):
			agg.pop("post_net_value", None)
		summary_bits = [
			f"{s['source']}={s['signed_credit_minus_debit']:+.0f}" for s in sources
		]
		payload = {
			"iran_sa_aggregate": True,
			"side": side,
			"agg_credit": agg_credit,
			"agg_debit": agg_debit,
			"components": sources,
		}
		_s(
			agg,
			"remarks",
			(
				f"IRR aggregated Stock Adjustment side={side} "
				f"agg_credit={agg_credit} agg_debit={agg_debit} "
				f"[{', '.join(summary_bits)}] "
				f"IRR_SA_AGG|{json.dumps(payload, ensure_ascii=False, default=str)[:1500]}"
			),
		)
		if hasattr(agg, "pop"):
			agg.pop("_irr_sabe_localization", None)
		else:
			try:
				delattr(agg, "_irr_sabe_localization")
			except Exception:
				pass
		replacements.append((anchor_idx, agg))

	if not drop and not replacements:
		return gl_map

	new_map: list = []
	replaced_at = {idx: entry for idx, entry in replacements}
	for i, entry in enumerate(gl_map):
		if i in replaced_at:
			new_map.append(replaced_at[i])
			continue
		if i in drop:
			continue
		new_map.append(entry)

	new_map = ensure_one_sided_gl_entries(new_map)

	doc.flags = getattr(doc, "flags", None) or frappe._dict()
	doc.flags.iran_sa_aggregate = {
		"account": sa,
		"groups": audit_groups,
		"active_aggregated_rows": len(replacements),
	}
	return new_map


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

# Copyright (c) 2026, ERPNext Extensions contributors
"""Aggregate Stock Adjustment GL legs that share a compatible accounting key.

Multiple sources (Core Manufacture SA, Iran A2, ±1 IRR precision residual) may
append separate Company.stock_adjustment_account rows. When company, voucher,
account, currencies, and dimensions match, collapse them to one net debit or
credit before persistence — preserving a structured source trail for audit.
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

# Fields that must match for SA aggregation (against / remarks / voucher_detail_no excluded).
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


def sa_aggregation_key(entry: Any, dimension_fields: list[str]) -> tuple:
	parts = []
	for field in _SA_AGGREGATION_FIELDS:
		parts.append(str(_g(entry, field, "") or ""))
	for field in dimension_fields:
		if field in ("cost_center",):  # already included
			continue
		parts.append(str(_g(entry, field, "") or ""))
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
		"remarks": remarks[:240],
		"idempotency": (
			remarks[remarks.find("IRR_SABE_A2|") :][:180]
			if "IRR_SABE_A2|" in remarks
			else None
		),
	}


def _net_pair(debit: float, credit: float, ccy: str) -> tuple[float, float]:
	debit = float(round_currency(debit, ccy))
	credit = float(round_currency(credit, ccy))
	if debit and credit:
		if debit >= credit:
			return float(round_currency(debit - credit, ccy)), 0.0
		return 0.0, float(round_currency(credit - debit, ccy))
	return debit, credit


def aggregate_stock_adjustment_gl_entries(doc, gl_map: list | None) -> list | None:
	"""Net-merge Company.stock_adjustment_account legs with identical dimensions.

	Non-SA accounts are left untouched (including A2 WIP/Stock counter-entries).
	Returns the rewritten map. Stores audit detail on ``doc.flags.iran_sa_aggregate``.
	"""
	if not gl_map or not doc:
		return gl_map
	company = getattr(doc, "company", None) or _g(gl_map[0], "company")
	if not company or not is_irr_company(company):
		return gl_map

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
		key = sa_aggregation_key(gl_map[i], dimensions)
		groups.setdefault(key, []).append((i, gl_map[i]))

	# Nothing to merge if every SA leg is already unique by key.
	if all(len(members) == 1 for members in groups.values()):
		return gl_map

	drop: set[int] = set()
	replacements: list[tuple[int, Any]] = []
	audit_groups = []

	for key, members in groups.items():
		if len(members) == 1:
			continue
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

		net_debit, net_credit = _net_pair(debit, credit, ccy)
		net_dac, net_cac = _net_pair(dac, cac, ccy)
		net_dtx, net_ctx = _net_pair(dtx, ctx, ccy)

		audit = {
			"account": sa,
			"aggregation_key": list(key),
			"components": sources,
			"gross_debit": float(round_currency(debit, ccy)),
			"gross_credit": float(round_currency(credit, ccy)),
			"net_debit": net_debit,
			"net_credit": net_credit,
		}
		audit_groups.append(audit)

		if abs(net_debit) < 0.5 and abs(net_credit) < 0.5:
			# Fully cancelled within the group — drop all members, no replacement.
			continue

		agg = frappe._dict(dict(anchor) if hasattr(anchor, "items") else anchor.copy())
		# Prefer first non-empty dimension values already on the anchor.
		_s(agg, "debit", net_debit)
		_s(agg, "credit", net_credit)
		_s(agg, "debit_in_account_currency", net_dac)
		_s(agg, "credit_in_account_currency", net_cac)
		_s(agg, "debit_in_transaction_currency", net_dtx)
		_s(agg, "credit_in_transaction_currency", net_ctx)
		_s(agg, "account_currency", _g(agg, "account_currency") or ccy)
		_s(agg, "post_net_value", 1)
		_s(agg, "voucher_detail_no", None)
		_s(agg, "_irr_sa_aggregated", 1)
		_s(agg, "_irr_sa_source_components", sources)
		# Structured remarks: machine-readable JSON trailer + human summary.
		summary_bits = [
			f"{s['source']}={s['signed_credit_minus_debit']:+.0f}" for s in sources
		]
		payload = {
			"iran_sa_aggregate": True,
			"net_credit": net_credit,
			"net_debit": net_debit,
			"components": sources,
		}
		_s(
			agg,
			"remarks",
			(
				f"IRR aggregated Stock Adjustment net_credit={net_credit} net_debit={net_debit} "
				f"[{', '.join(summary_bits)}] "
				f"IRR_SA_AGG|{json.dumps(payload, ensure_ascii=False, default=str)[:1500]}"
			),
		)
		# Clear exclusive A2-only markers so the row represents the aggregate.
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

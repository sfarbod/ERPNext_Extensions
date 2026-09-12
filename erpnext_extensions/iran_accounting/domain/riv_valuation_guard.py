# Copyright (c) 2026, ERPNext Extensions contributors
"""Manufacture / SLE valuation integrity guards (fail-closed, pre-persist).

Negative incoming Manufacture valuation is not a rounding residual. These
assertions must run before Stock Entry row or SLE persistence. Do not repair
invalid signs with abs() or clamp-to-zero.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe import _
from frappe.utils import flt

from erpnext_extensions.iran_accounting.domain.currency import (
	get_company_currency,
	get_currency_precision,
	is_irr_company,
	round_currency,
)


class ValuationIntegrityError(frappe.ValidationError):
	"""Invalid stock valuation blocked before persistence (not rounding)."""


def _entry_get(obj, field, default=None):
	if obj is None:
		return default
	if hasattr(obj, "get"):
		value = obj.get(field)
		return default if value is None else value
	return getattr(obj, field, default)


def _quantum(company: str | None) -> float:
	currency = get_company_currency(company) if company else "IRR"
	precision = get_currency_precision(currency)
	return 1.0 if precision == 0 else (1.0 / (10**precision))


def _current_riv_name() -> str | None:
	if not frappe.flags.get("through_repost_item_valuation"):
		return None
	try:
		return frappe.db.get_value(
			"Repost Item Valuation",
			{"status": "In Progress"},
			"name",
			order_by="modified desc",
		)
	except Exception:
		return None


def format_valuation_integrity_message(payload: dict[str, Any]) -> str:
	"""Human-readable diagnostic; payload keys omitted when unset."""
	invariant = payload.get("invariant") or "unknown"
	lines = [
		_("Stock valuation integrity ({0}).").format(invariant),
		_("This is not a rounding residual."),
		_("Invalid valuation was blocked before persistence."),
	]
	order = (
		"invariant",
		"riv_name",
		"voucher_type",
		"voucher_no",
		"voucher_detail_no",
		"row_idx",
		"item_code",
		"warehouse",
		"purpose",
		"secondary_item_type",
		"valuation_type",
		"is_finished_item",
		"actual_qty",
		"transfer_qty",
		"qty_after_transaction",
		"incoming_rate",
		"outgoing_rate",
		"valuation_rate",
		"basic_rate",
		"amount",
		"basic_amount",
		"additional_cost",
		"landed_cost_voucher_amount",
		"stock_value_difference",
		"stock_value",
		"outgoing_pool",
		"other_incoming",
		"fg_amount",
		"issued_scrap_rate",
		"warehouse_scrap_rate",
		"detail",
	)
	for key in order:
		if key == "invariant":
			continue
		value = payload.get(key)
		if value in (None, ""):
			continue
		lines.append(f"{key}={value}")
	return "\n".join(lines)


def throw_valuation_integrity(
	invariant: str,
	*,
	detail: str | None = None,
	sle=None,
	row=None,
	doc=None,
	**extra: Any,
) -> None:
	payload: dict[str, Any] = {"invariant": invariant}
	if sle is not None:
		payload.update(
			{
				"voucher_type": _entry_get(sle, "voucher_type"),
				"voucher_no": _entry_get(sle, "voucher_no"),
				"voucher_detail_no": _entry_get(sle, "voucher_detail_no"),
				"item_code": _entry_get(sle, "item_code"),
				"warehouse": _entry_get(sle, "warehouse"),
				"actual_qty": _entry_get(sle, "actual_qty"),
				"qty_after_transaction": _entry_get(sle, "qty_after_transaction"),
				"incoming_rate": _entry_get(sle, "incoming_rate"),
				"outgoing_rate": _entry_get(sle, "outgoing_rate"),
				"valuation_rate": _entry_get(sle, "valuation_rate"),
				"stock_value_difference": _entry_get(sle, "stock_value_difference"),
				"stock_value": _entry_get(sle, "stock_value"),
			}
		)
	if doc is not None:
		payload.setdefault("voucher_type", _entry_get(doc, "doctype"))
		payload.setdefault("voucher_no", _entry_get(doc, "name"))
		payload.setdefault("purpose", _entry_get(doc, "purpose"))
	if row is not None:
		payload.setdefault("voucher_detail_no", _entry_get(row, "name"))
		payload.setdefault("row_idx", _entry_get(row, "idx"))
		payload.setdefault("item_code", _entry_get(row, "item_code"))
		payload.setdefault("warehouse", _entry_get(row, "t_warehouse") or _entry_get(row, "s_warehouse"))
		payload.setdefault("transfer_qty", _entry_get(row, "transfer_qty") or _entry_get(row, "qty"))
		payload.setdefault("incoming_rate", _entry_get(row, "valuation_rate"))
		payload.setdefault("valuation_rate", _entry_get(row, "valuation_rate"))
		payload.setdefault("basic_rate", _entry_get(row, "basic_rate"))
		payload.setdefault("amount", _entry_get(row, "amount"))
		payload.setdefault("basic_amount", _entry_get(row, "basic_amount"))
		payload.setdefault("additional_cost", _entry_get(row, "additional_cost"))
		payload.setdefault("landed_cost_voucher_amount", _entry_get(row, "landed_cost_voucher_amount"))
		payload.setdefault("secondary_item_type", _entry_get(row, "secondary_item_type") or _entry_get(row, "type"))
		payload.setdefault("valuation_type", _entry_get(row, "valuation_type"))
		payload.setdefault("is_finished_item", _entry_get(row, "is_finished_item"))
		payload.setdefault("purpose", payload.get("purpose") or (doc and _entry_get(doc, "purpose")))
	payload["riv_name"] = extra.pop("riv_name", None) or _current_riv_name()
	if detail:
		payload["detail"] = detail
	payload.update({k: v for k, v in extra.items() if v is not None})
	frappe.throw(
		format_valuation_integrity_message(payload),
		exc=ValuationIntegrityError,
		title=_("Stock valuation integrity"),
	)


def assert_non_negative_magnitude(magnitude: float, actual_qty: float, **context) -> None:
	"""row.amount is a non-negative magnitude. Never abs()-repair a negative."""
	if flt(magnitude) < 0:
		throw_valuation_integrity(
			"I2",
			detail="row.amount / movement magnitude is negative",
			actual_qty=actual_qty,
			amount=magnitude,
			**context,
		)


def assert_incoming_rate_not_negative(sle, **context) -> None:
	"""I1: incoming qty > 0 must not carry a negative incoming_rate."""
	actual_qty = flt(_entry_get(sle, "actual_qty"))
	incoming_rate = flt(_entry_get(sle, "incoming_rate"))
	if actual_qty > 0 and incoming_rate < 0:
		throw_valuation_integrity(
			"I1",
			detail="incoming_rate is negative on an incoming movement",
			sle=sle,
			**context,
		)


def assert_svd_direction(sle, **context) -> None:
	"""I3: SVD sign follows qty, never a possibly-negative valuation rate."""
	actual_qty = flt(_entry_get(sle, "actual_qty"))
	svd = flt(_entry_get(sle, "stock_value_difference"))
	if actual_qty == 0 or svd == 0:
		return
	company = _entry_get(sle, "company") or context.get("company")
	if abs(svd) <= _quantum(company):
		return
	if actual_qty > 0 and svd < 0:
		throw_valuation_integrity(
			"I3",
			detail="incoming qty has negative stock_value_difference",
			sle=sle,
			**context,
		)
	if actual_qty < 0 and svd > 0:
		throw_valuation_integrity(
			"I3",
			detail="outgoing qty has positive stock_value_difference",
			sle=sle,
			**context,
		)


def assert_zero_qty_stock_value(sle, **context) -> None:
	"""I4: qty_after==0 must not keep material leftover value (MA persist)."""
	if _entry_get(sle, "voucher_type") == "Stock Reconciliation":
		return
	method = _entry_get(sle, "valuation_method")
	if method and method != "Moving Average":
		return
	qty_after_raw = _entry_get(sle, "qty_after_transaction")
	if qty_after_raw in (None, ""):
		return
	qty_after = flt(qty_after_raw)
	if qty_after != 0:
		return
	stock_value = flt(_entry_get(sle, "stock_value"))
	company = _entry_get(sle, "company") or context.get("company")
	if abs(stock_value) <= _quantum(company):
		return
	throw_valuation_integrity(
		"I4",
		detail="qty_after_transaction is 0 but stock_value leftover exceeds ±1 IRR quantum",
		sle=sle,
		**context,
	)


def _incoming_se_row(row) -> bool:
	return bool(_entry_get(row, "t_warehouse")) and not _entry_get(row, "s_warehouse")


def _row_qty(row) -> float:
	value = _entry_get(row, "transfer_qty")
	if value in (None, ""):
		value = _entry_get(row, "qty")
	return flt(value)


def assert_manufacture_repack_incoming_amounts(doc) -> None:
	"""I2: Manufacture/Repack incoming row.amount must not be negative."""
	purpose = _entry_get(doc, "purpose")
	if purpose not in ("Manufacture", "Repack"):
		return
	for row in doc.get("items") or []:
		if not _incoming_se_row(row):
			continue
		if _row_qty(row) <= 0:
			continue
		amount = flt(_entry_get(row, "amount"))
		if amount < 0:
			throw_valuation_integrity(
				"I2",
				detail="incoming Manufacture/Repack row.amount is negative",
				doc=doc,
				row=row,
			)


def assert_manufacture_value_pool(doc) -> None:
	"""I5: single-FG Manufacture secondary incoming must not force FG negative."""
	if _entry_get(doc, "doctype") != "Stock Entry":
		return
	if _entry_get(doc, "purpose") != "Manufacture":
		return
	rows = doc.get("items") or []
	fg_rows = [row for row in rows if row.get("is_finished_item") and row.get("t_warehouse")]
	if len(fg_rows) != 1:
		return
	fg = fg_rows[0]
	fg_amount = flt(fg.get("amount"))
	company = _entry_get(doc, "company")
	quantum = _quantum(company)
	outgoing = sum(flt(row.get("amount")) for row in rows if row.get("s_warehouse"))
	other_incoming = sum(
		flt(row.get("amount"))
		for row in rows
		if _incoming_se_row(row) and row is not fg
	)
	incoming_capitalized = sum(
		flt(row.get("additional_cost")) + flt(row.get("landed_cost_voucher_amount"))
		for row in rows
		if row.get("t_warehouse")
	)
	available = outgoing + incoming_capitalized
	if fg_amount < 0 or other_incoming > available + quantum:
		issued = None
		warehouse_rate = None
		for row in rows:
			if not _incoming_se_row(row) or row is fg:
				continue
			warehouse_rate = flt(row.get("valuation_rate"))
			break
		throw_valuation_integrity(
			"I5",
			detail="secondary incoming value exceeds the manufacturing pool (FG would be negative)",
			doc=doc,
			row=fg,
			outgoing_pool=outgoing,
			other_incoming=other_incoming,
			fg_amount=fg_amount,
			warehouse_scrap_rate=warehouse_rate,
			issued_scrap_rate=issued,
		)


def assert_stock_entry_valuation_integrity(doc) -> None:
	"""SE-level I2 + I5. Transfer/MTfM skip Manufacture pool rules."""
	if not doc or _entry_get(doc, "doctype") != "Stock Entry":
		return
	company = _entry_get(doc, "company")
	if not company or not is_irr_company(company):
		return
	assert_manufacture_repack_incoming_amounts(doc)
	assert_manufacture_value_pool(doc)


def assert_sle_valuation_integrity_before_vanilla(engine, sle) -> None:
	"""L2: knowable invalid state must not enter vanilla process_sle."""
	if not sle:
		return
	company = getattr(engine, "company", None) or _entry_get(sle, "company")
	if company and not is_irr_company(company):
		return
	assert_incoming_rate_not_negative(sle)
	if _entry_get(sle, "voucher_type") != "Stock Entry":
		return
	detail_no = _entry_get(sle, "voucher_detail_no")
	if not detail_no:
		return
	row = frappe.db.get_value(
		"Stock Entry Detail",
		detail_no,
		[
			"name",
			"idx",
			"item_code",
			"qty",
			"transfer_qty",
			"amount",
			"basic_amount",
			"basic_rate",
			"valuation_rate",
			"additional_cost",
			"landed_cost_voucher_amount",
			"t_warehouse",
			"s_warehouse",
			"is_finished_item",
			"secondary_item_type",
			"valuation_type",
			"parent",
		],
		as_dict=True,
	)
	if not row:
		return
	purpose = frappe.db.get_value("Stock Entry", row.parent, "purpose")
	if purpose in ("Manufacture", "Repack") and _incoming_se_row(row) and _row_qty(row) > 0:
		if flt(row.amount) < 0:
			throw_valuation_integrity(
				"I2",
				detail="incoming Stock Entry row.amount is negative before SLE write",
				sle=sle,
				row=row,
				purpose=purpose,
			)
		if flt(row.valuation_rate) < 0 and flt(_entry_get(sle, "actual_qty")) > 0:
			throw_valuation_integrity(
				"I1",
				detail="incoming Stock Entry valuation_rate is negative before SLE write",
				sle=sle,
				row=row,
				purpose=purpose,
			)


def assert_sle_valuation_integrity_after_sync(sle) -> None:
	"""L3: after Iran SLE sync, before persist_processed_sle."""
	if not sle:
		return
	company = _entry_get(sle, "company")
	if not company or not is_irr_company(company):
		return
	assert_incoming_rate_not_negative(sle)
	assert_svd_direction(sle)
	assert_zero_qty_stock_value(sle)


def apply_irr_stock_entry_contract_after_calculate(doc) -> None:
	"""Canonical post-calculate Iran contract for IRR Stock Entries (submit + RIV)."""
	from erpnext_extensions.iran_accounting.domain.qty_rate_amount import (
		align_stock_entry_item_amounts,
	)
	from erpnext_extensions.iran_accounting.manufacture_rounding import (
		align_manufacture_finished_good_residual,
	)
	from erpnext_extensions.iran_accounting.scrap_costing import (
		apply_iran_manufacture_output_contract,
	)

	if not doc or not is_irr_company(doc.company):
		return
	apply_iran_manufacture_output_contract(doc)
	align_stock_entry_item_amounts(doc)
	if doc.purpose == "Manufacture":
		align_manufacture_finished_good_residual(doc)
	if hasattr(doc, "set_total_incoming_outgoing_value"):
		doc.set_total_incoming_outgoing_value()
	assert_stock_entry_valuation_integrity(doc)


def persist_stock_entry_after_recalculate(stock_entry, voucher_detail_no) -> None:
	"""Preserve ERPNext 16.34.2 recalculate persist rules after Iran contract."""
	stock_entry.db_update()
	update_additional_cost_rows = bool(stock_entry.get("additional_costs"))
	for row in stock_entry.items:
		if (
			row.name == voucher_detail_no
			or (not row.s_warehouse and row.t_warehouse)
			or stock_entry.purpose in ["Manufacture", "Repack"]
			or (update_additional_cost_rows and row.t_warehouse)
		):
			row.db_update()


def make_recalculate_amounts_wrapper(original):
	"""L1: apply Iran contract + integrity asserts before any SE row db_update."""

	def recalculate_amounts_in_stock_entry(self, voucher_no, voucher_detail_no):
		company = frappe.db.get_value("Stock Entry", voucher_no, "company")
		if not company or not is_irr_company(company):
			return original(self, voucher_no, voucher_detail_no)

		stock_entry = frappe.get_lazy_doc("Stock Entry", voucher_no, for_update=True)
		stock_entry.calculate_rate_and_amount(
			reset_outgoing_rate=False, raise_error_if_no_rate=False
		)
		apply_irr_stock_entry_contract_after_calculate(stock_entry)
		persist_stock_entry_after_recalculate(stock_entry, voucher_detail_no)

	recalculate_amounts_in_stock_entry._iran_riv_recalculate_wrapper = True
	recalculate_amounts_in_stock_entry._iran_original = original
	return recalculate_amounts_in_stock_entry


def round_currency_safe(value, company: str | None):
	return round_currency(value, get_company_currency(company) if company else "IRR")

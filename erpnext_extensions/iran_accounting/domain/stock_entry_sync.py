# Copyright (c) 2026, ERPNext Extensions contributors
"""Stock Entry: capitalization-aware row.amount is the source for SLE movement (IRR).

Architectural boundary
----------------------
* Approved manufacturing purposes (Manufacture / Repack / Material Consumption
  for Manufacture): document row economics remain authoritative — Iran contracts
  allocate FG / secondary / scrap / residual amounts, then SLE mirrors SED.
* Ordinary stock movements (Material Issue / Transfer / …): ERPNext native
  valuation (BatchNoValuation / SABB / moving average) remains authoritative.
  Iran only applies IRR monetary rounding and, when SED drifted from native
  SLE, realigns SED to the native movement so SED/SABB/SLE/GL stay consistent.

Never replace a correct native batch/MA movement with an unrelated SED.amount.
"""

from __future__ import annotations

import frappe
from frappe.utils import cint, flt

from erpnext_extensions.iran_accounting.domain.currency import (
	get_company_currency,
	is_irr_company,
	round_currency,
	round_monetary_rate,
	round_row_amount_financial,
)
from erpnext_extensions.iran_accounting.domain.riv_rate_guard import (
	is_valued_source_zero_outgoing,
)
from erpnext_extensions.iran_accounting.domain.riv_valuation_guard import (
	assert_non_negative_magnitude,
	enforce_zero_qty_terminal_stock_value,
)

# Purposes where ERPNext Core stock valuation (MA / batch / SABB) owns the
# outgoing movement. Document SED must not override that movement unless the
# row is explicitly manual or allow-zero.
NATIVE_OUTGOING_STOCK_VALUATION_PURPOSES = frozenset(
	{
		"Material Issue",
		"Material Transfer",
		"Material Transfer for Manufacture",
		"Send to Subcontractor",
	}
)

# Purposes that keep SED → SLE mirroring (approved manufacturing contracts).
DOCUMENT_AMOUNT_AUTHORITY_PURPOSES = frozenset(
	{
		"Manufacture",
		"Repack",
		"Material Consumption for Manufacture",
	}
)


def stock_entry_row_amount(row, company: str) -> float:
	"""Capitalization-aware row amount for SLE/GL mirroring.

	Prefer stored ``amount`` (already ERPNext composition after align).
	If missing, recompose: basic_amount + additional_cost + landed_cost_voucher_amount,
	with basic_amount from transfer_qty × integer basic_rate when needed.
	"""
	ccy = get_company_currency(company)
	if row.get("amount") not in (None, ""):
		return float(round_currency(row.amount, ccy))

	transfer_qty = flt(
		row.get("transfer_qty") if row.get("transfer_qty") not in (None, "") else row.get("qty")
	)
	basic_amount = row.get("basic_amount")
	if basic_amount in (None, "") and transfer_qty and row.get("basic_rate") not in (None, ""):
		basic_amount = round_row_amount_financial(transfer_qty, row.basic_rate, ccy)
	composed = (
		flt(basic_amount)
		+ flt(row.get("additional_cost"))
		+ flt(row.get("landed_cost_voucher_amount"))
	)
	if composed:
		return float(round_currency(composed, ccy))
	return 0.0


def signed_movement_from_row_amount(magnitude: float, actual_qty: float, **context) -> float:
	"""row.amount is a non-negative magnitude; direction comes from actual_qty.

	Do not abs()-repair a negative incoming amount into a positive SVD.
	"""
	assert_non_negative_magnitude(magnitude, actual_qty, **context)
	if actual_qty < 0:
		return -float(magnitude)
	if actual_qty > 0:
		return float(magnitude)
	return 0.0


def _sle_rate_from_row(row, ccy: str) -> float:
	"""Prefer integer valuation_rate; fall back to integer basic_rate."""
	if row.get("valuation_rate") not in (None, ""):
		return float(round_monetary_rate(row.valuation_rate, ccy))
	if row.get("basic_rate") not in (None, ""):
		return float(round_monetary_rate(row.basic_rate, ccy))
	return 0.0


def should_preserve_native_outgoing_valuation(sle, row, purpose: str | None) -> bool:
	"""True when vanilla SLE movement must not be replaced by SED.amount."""
	if str(purpose or "") not in NATIVE_OUTGOING_STOCK_VALUATION_PURPOSES:
		return False
	if str(purpose or "") in DOCUMENT_AMOUNT_AUTHORITY_PURPOSES:
		return False
	if flt(sle.actual_qty) >= 0:
		# Inward legs keep document/incoming rate semantics.
		return False
	if cint(row.get("set_basic_rate_manually")):
		return False
	# Explicit free issue: document zero is intentional.
	if cint(row.get("allow_zero_valuation_rate")) and abs(flt(row.get("basic_rate") or 0)) <= 1e-9:
		return False
	return True


def clear_sticky_native_outgoing_rate_for_riv(sle) -> bool:
	"""Clear DB-sticky SLE.outgoing_rate so Core MA uses warehouse valuation_rate.

	During Item+WH RIV, Core skips ``get_dynamic_incoming_outgoing_rate`` when
	``sle_id`` is set and keeps the persisted SLE.outgoing_rate. For Material
	Issue / Transfer that rate may be a poisoned SED stamp (e.g. 1,581,633 vs
	MA 1,500,000). ``get_moving_average_values`` then mixes it into wh_data and
	can drive stock_value negative → I3 when qty hits zero.

	Native-outgoing purposes must let MA own the rate; Iran then realigns SED
	via :func:`_preserve_native_outgoing_and_align_sed`. Manual / free-issue
	rows are left unchanged.
	"""
	if flt(getattr(sle, "actual_qty", 0) if not hasattr(sle, "get") else sle.get("actual_qty")) >= 0:
		return False
	voucher_type = sle.get("voucher_type") if hasattr(sle, "get") else getattr(sle, "voucher_type", None)
	if voucher_type != "Stock Entry":
		return False
	voucher_no = sle.get("voucher_no") if hasattr(sle, "get") else getattr(sle, "voucher_no", None)
	detail_no = (
		sle.get("voucher_detail_no") if hasattr(sle, "get") else getattr(sle, "voucher_detail_no", None)
	)
	if not voucher_no:
		return False
	purpose = frappe.db.get_value("Stock Entry", voucher_no, "purpose")
	if str(purpose or "") not in NATIVE_OUTGOING_STOCK_VALUATION_PURPOSES:
		return False
	if str(purpose or "") in DOCUMENT_AMOUNT_AUTHORITY_PURPOSES:
		return False
	row = None
	if detail_no:
		row = frappe.db.get_value(
			"Stock Entry Detail",
			detail_no,
			["set_basic_rate_manually", "allow_zero_valuation_rate", "basic_rate"],
			as_dict=True,
		)
	if row and cint(row.get("set_basic_rate_manually")):
		return False
	if (
		row
		and cint(row.get("allow_zero_valuation_rate"))
		and abs(flt(row.get("basic_rate") or 0)) <= 1e-9
	):
		return False
	# Only clear when a sticky non-zero rate is present (avoid noop churn).
	cur = flt(sle.get("outgoing_rate") if hasattr(sle, "get") else getattr(sle, "outgoing_rate", 0))
	if abs(cur) <= 1e-9:
		return False
	if hasattr(sle, "outgoing_rate"):
		sle.outgoing_rate = 0
	elif hasattr(sle, "__setitem__"):
		sle["outgoing_rate"] = 0
	return True


def _native_movement_rate(sle, ccy: str) -> tuple[float, float]:
	"""IRR-round the vanilla SLE movement; derive per-unit rate from it."""
	value_after = flt(sle.stock_value)
	value_before = value_after - flt(sle.stock_value_difference)
	movement = flt(round_currency(flt(sle.stock_value_difference), ccy))
	sle.stock_value_difference = movement
	sle.stock_value = flt(round_currency(value_before + movement, ccy))

	qty = abs(flt(sle.actual_qty))
	if qty > 1e-9:
		rate = flt(round_monetary_rate(abs(movement) / qty, ccy))
	else:
		rate = flt(
			round_monetary_rate(
				flt(sle.outgoing_rate) or flt(sle.incoming_rate) or flt(sle.valuation_rate),
				ccy,
			)
		)
	return movement, rate


def _apply_native_rates_to_sle(sle, rate: float, ccy: str) -> None:
	sle.valuation_rate = rate
	if flt(sle.actual_qty) > 0:
		sle.incoming_rate = rate
		sle.outgoing_rate = float(round_monetary_rate(sle.outgoing_rate or 0, ccy))
	elif flt(sle.actual_qty) < 0:
		sle.outgoing_rate = rate
		sle.incoming_rate = float(round_monetary_rate(sle.incoming_rate or 0, ccy))
	else:
		sle.incoming_rate = float(round_monetary_rate(sle.incoming_rate or 0, ccy))
		sle.outgoing_rate = float(round_monetary_rate(sle.outgoing_rate or 0, ccy))


def align_sed_from_native_outgoing_sle(sle, row, rate: float, movement: float, ccy: str) -> None:
	"""Realign SED basic/valuation amounts to native SLE movement (IRR).

	Preserves additional_cost / landed_cost_voucher_amount. Does not clear
	manual-rate flags (caller already excluded manual rows).
	"""
	if not sle.voucher_detail_no:
		return

	basic_amount = float(round_currency(abs(flt(movement)), ccy))
	amount = float(
		round_currency(
			basic_amount + flt(row.get("additional_cost")) + flt(row.get("landed_cost_voucher_amount")),
			ccy,
		)
	)
	# Source-warehouse outward: valuation_rate matches basic_rate (no addl on source).
	valuation_rate = rate
	if flt(row.get("additional_cost")) or flt(row.get("landed_cost_voucher_amount")):
		qty = abs(flt(sle.actual_qty)) or abs(
			flt(row.get("transfer_qty") if row.get("transfer_qty") not in (None, "") else row.get("qty"))
		)
		if qty > 1e-9:
			valuation_rate = float(round_monetary_rate(amount / qty, ccy))

	frappe.db.set_value(
		"Stock Entry Detail",
		sle.voucher_detail_no,
		{
			"basic_rate": rate,
			"valuation_rate": valuation_rate,
			"basic_amount": basic_amount,
			"amount": amount,
		},
		update_modified=False,
	)


def _preserve_native_outgoing_and_align_sed(sle, row, ccy: str) -> None:
	movement, rate = _native_movement_rate(sle, ccy)
	_apply_native_rates_to_sle(sle, rate, ccy)

	# Only rewrite SED when it disagrees with native movement (poison / stale MA).
	sed_mag = abs(flt(stock_entry_row_amount(row, sle.company)))
	if abs(sed_mag - abs(flt(movement))) > 0.5 or abs(
		flt(_sle_rate_from_row(row, ccy)) - rate
	) > 0.5:
		align_sed_from_native_outgoing_sle(sle, row, rate, movement, ccy)
	# Empty warehouse must not keep a residual layer after IRR rounding.
	enforce_zero_qty_terminal_stock_value(sle)


def _apply_document_authority_movement(sle, movement: float, rate: float, ccy: str) -> None:
	"""Stamp SED movement onto SLE while preserving Core empty-warehouse value.

	``stock_value_difference`` always mirrors signed SED.amount (document /
	transfer conservation). ``stock_value`` is ``previous + movement`` only while
	qty remains positive. When this SLE finishes a processed consume-to-zero,
	Core requires ``stock_value = 0`` — never reintroduce a nonzero residual from
	``previous + SED`` (MAT-STE-2026-40942 / I4 class).
	"""
	from erpnext_extensions.iran_accounting.domain.riv_valuation_guard import (
		is_final_zero_qty_consume_state,
	)

	value_after = flt(sle.stock_value)
	value_before = value_after - flt(sle.stock_value_difference)
	sle.stock_value_difference = movement
	if is_final_zero_qty_consume_state(sle):
		# Keep SED SVD; clear terminal warehouse value (Core invariant).
		sle.stock_value = float(round_currency(0, ccy))
		enforce_zero_qty_terminal_stock_value(sle)
	else:
		# Insert/transient qty_after=0 still uses movement as stock_value so the
		# engine can copy running balance later — do not clamp that shape.
		sle.stock_value = round_currency(value_before + movement, ccy)

	sle.valuation_rate = rate
	if flt(sle.actual_qty) > 0:
		sle.incoming_rate = rate
		sle.outgoing_rate = round_monetary_rate(sle.outgoing_rate or 0, ccy)
	elif flt(sle.actual_qty) < 0:
		sle.outgoing_rate = rate
		sle.incoming_rate = round_monetary_rate(sle.incoming_rate or 0, ccy)
	else:
		sle.incoming_rate = round_monetary_rate(sle.incoming_rate or 0, ccy)
		sle.outgoing_rate = round_monetary_rate(sle.outgoing_rate or 0, ccy)


def sync_irr_sle_from_stock_entry_row(sle) -> None:
	"""Apply Iran SLE economics after vanilla process_sle.

	* Manufacturing purposes: SLE movement mirrors signed SED.amount (contracts).
	* Native outgoing stock purposes: keep vanilla SLE movement (SABB/MA), IRR-round,
	  and realign SED when it conflicts — never stamp a poisoned SED onto SLE.
	* Consume-to-zero: never persist nonzero ``stock_value`` after Core emptied
	  the warehouse (see :func:`enforce_zero_qty_terminal_stock_value`).

	During Item-scoped RIV, out-of-scope legacy poison (different item_code than the
	declared RIV target) must not abort the target repost — log and skip the Iran
	mirror so vanilla SLE economics for that unrelated row are left alone.
	"""
	if not sle.company or not is_irr_company(sle.company):
		return
	if sle.voucher_type != "Stock Entry" or not sle.voucher_detail_no:
		return
	if cint(sle.get("is_cancelled")):
		return

	row = frappe.db.get_value(
		"Stock Entry Detail",
		sle.voucher_detail_no,
		[
			"qty",
			"transfer_qty",
			"basic_rate",
			"basic_amount",
			"additional_cost",
			"landed_cost_voucher_amount",
			"valuation_rate",
			"amount",
			"allow_zero_valuation_rate",
			"set_basic_rate_manually",
			"parent",
			"serial_and_batch_bundle",
			"s_warehouse",
			"t_warehouse",
		],
		as_dict=True,
	)
	if not row:
		return

	from erpnext_extensions.iran_accounting.domain.riv_valuation_guard import (
		ValuationIntegrityError,
		throw_valuation_integrity,
	)

	ccy = get_company_currency(sle.company)
	purpose = frappe.db.get_value("Stock Entry", row.parent or sle.voucher_no, "purpose")

	# Native outgoing stock valuation: preserve vanilla SLE; do not SED-stamp.
	if should_preserve_native_outgoing_valuation(sle, row, purpose):
		_preserve_native_outgoing_and_align_sed(sle, row, ccy)
		return

	try:
		magnitude = stock_entry_row_amount(row, sle.company)
		# VALUED_SOURCE_ZERO_OUTGOING: document basic_rate/amount is 0 but
		# vanilla moving-average already priced a Manufacture consume from a
		# valued warehouse layer. Stamping the document zero would wipe
		# provenance. Leftover-MA Material Transfer/Receipt stays excluded.
		if is_valued_source_zero_outgoing(
			actual_qty=sle.actual_qty,
			basic_rate=row.basic_rate,
			allow_zero_valuation_rate=row.get("allow_zero_valuation_rate"),
			outgoing_rate=(
				sle.outgoing_rate
				if abs(flt(sle.outgoing_rate)) > 1e-6
				else (
					abs(flt(sle.stock_value_difference)) / abs(flt(sle.actual_qty))
					if abs(flt(sle.actual_qty)) > 1e-9 and abs(flt(sle.stock_value_difference)) > 1e-6
					else 0
				)
			),
			purpose=purpose,
		):
			if abs(flt(sle.outgoing_rate)) <= 1e-6 and abs(flt(sle.actual_qty)) > 1e-9:
				sle.outgoing_rate = abs(flt(sle.stock_value_difference)) / abs(flt(sle.actual_qty))
			enforce_zero_qty_terminal_stock_value(sle)
			return
		movement = round_currency(
			signed_movement_from_row_amount(
				magnitude,
				flt(sle.actual_qty),
				sle=sle,
				row=row,
			),
			ccy,
		)
		rate = _sle_rate_from_row(row, ccy)
		if flt(sle.actual_qty) > 0 and rate < 0:
			throw_valuation_integrity(
				"I1",
				detail="incoming Stock Entry valuation_rate is negative",
				sle=sle,
				row=row,
			)
	except ValuationIntegrityError as exc:
		engine = getattr(frappe.local, "iran_riv_update_entries_after", None)
		if engine is None:
			raise
		from erpnext_extensions.iran_accounting.domain.riv_valuation_scope import (
			is_sle_in_riv_blocking_scope,
			log_out_of_scope_integrity_anomaly,
		)

		if is_sle_in_riv_blocking_scope(engine, sle):
			raise
		log_out_of_scope_integrity_anomaly(engine, sle=sle, exc=exc)
		return

	_apply_document_authority_movement(sle, movement, rate, ccy)


def gl_movement_from_row_only(item_row, sle, company: str) -> float:
	"""GL debit/credit leg from row.amount + SLE sign (actual_qty); no SLE magnitude fallback."""
	magnitude = stock_entry_row_amount(item_row, company)
	return signed_movement_from_row_amount(magnitude, flt(sle.actual_qty))


def sum_stock_entry_row_amounts(doc) -> float:
	"""Σ positive row.amount (gross); for transfers compare to incoming leg."""
	total = 0.0
	for row in doc.get("items") or []:
		total += stock_entry_row_amount(row, doc.company)
	return total


def assert_stock_entry_row_sle_mirror(voucher_no: str, company: str) -> list[str]:
	failures = []
	sles = frappe.get_all(
		"Stock Ledger Entry",
		filters={"voucher_type": "Stock Entry", "voucher_no": voucher_no, "is_cancelled": 0},
		fields=["name", "voucher_detail_no", "actual_qty", "stock_value_difference"],
	)
	for sle in sles:
		if not sle.voucher_detail_no:
			continue
		row = frappe.db.get_value(
			"Stock Entry Detail",
			sle.voucher_detail_no,
			[
				"qty",
				"transfer_qty",
				"basic_rate",
				"basic_amount",
				"additional_cost",
				"landed_cost_voucher_amount",
				"valuation_rate",
				"amount",
			],
			as_dict=True,
		)
		if not row:
			continue
		mag = stock_entry_row_amount(row, company)
		expected = signed_movement_from_row_amount(mag, flt(sle.actual_qty))
		if flt(sle.stock_value_difference) != expected:
			failures.append(f"SLE {sle.name}: movement {sle.stock_value_difference} != row mirror {expected}")
	return failures


def refresh_native_outgoing_sed_from_db(doc) -> None:
	"""Reload SED rates from DB into ``doc.items`` after process_sle alignment.

	Iran ``on_submit`` otherwise ``db_update``s stale in-memory rates and can
	re-poison rows that ``sync_irr_sle_from_stock_entry_row`` already corrected.

	Also required before ``get_gl_entries`` / balanced-transfer GL: process_sle
	writes authoritative native amounts to DB while in-memory SED may still hold
	pre-submit integer document rates (v5.5.46).
	"""
	if not doc or getattr(doc, "doctype", None) != "Stock Entry":
		return
	if str(doc.purpose or "") not in NATIVE_OUTGOING_STOCK_VALUATION_PURPOSES:
		return
	for row in doc.get("items") or []:
		if not row.get("name") or not row.get("s_warehouse"):
			continue
		if cint(row.get("set_basic_rate_manually")):
			continue
		live = frappe.db.get_value(
			"Stock Entry Detail",
			row.name,
			["basic_rate", "valuation_rate", "basic_amount", "amount"],
			as_dict=True,
		)
		if not live:
			continue
		row.basic_rate = live.basic_rate
		row.valuation_rate = live.valuation_rate
		row.basic_amount = live.basic_amount
		row.amount = live.amount


def sync_native_outgoing_sed_into_memory_for_gl(doc) -> bool:
	"""Refresh in-memory SED (+ header totals) before Stock Entry inventory GL.

	Returns True when a native-outgoing Stock Entry was refreshed. Callers must
	invoke this before ``get_gl_entries`` so transfer / issue GL consumes the
	authoritative BatchNoValuation / SABB movement already persisted by
	``sync_irr_sle_from_stock_entry_row``, not stale draft SED amounts.

	Does not reverse or remake GL; does not invent Stock Adjustment.
	"""
	if not doc or getattr(doc, "doctype", None) != "Stock Entry":
		return False
	from erpnext_extensions.iran_accounting.domain.currency import is_irr_company

	if not is_irr_company(doc.company):
		return False
	if str(doc.purpose or "") not in NATIVE_OUTGOING_STOCK_VALUATION_PURPOSES:
		return False

	refresh_native_outgoing_sed_from_db(doc)

	# Keep ZVT / balanced-transfer header gates coherent with refreshed rows.
	from erpnext_extensions.iran_accounting.stock_entry import align_zero_value_transfer_totals

	align_zero_value_transfer_totals(doc)
	if hasattr(doc, "set_total_incoming_outgoing_value"):
		doc.set_total_incoming_outgoing_value()
	return True


# Backward-compatible alias
irr_gl_movement_amount = gl_movement_from_row_only

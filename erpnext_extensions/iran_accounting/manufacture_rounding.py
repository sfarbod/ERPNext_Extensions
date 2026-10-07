# Copyright (c) 2026, ERPNext Extensions contributors
"""Manufacture residual alignment — never wipe legitimate capitalization."""

from __future__ import annotations

from frappe.utils import flt

from erpnext_extensions.iran_accounting.domain.currency import (
	integer_valuation_rate_from_amount,
	round_monetary_rate,
)
from erpnext_extensions.iran_accounting.rounding import (
	get_company_currency,
	get_currency_precision,
	is_irr_company,
	round_currency,
)


def _residual_tolerance(currency: str) -> float:
	precision = get_currency_precision(currency)
	return 1.0 if precision == 0 else (1.0 / (10**precision))


def _row_capitalized_cost(row) -> float:
	return flt(row.get("additional_cost")) + flt(row.get("landed_cost_voucher_amount"))


def _row_qty(row) -> float:
	value = row.get("transfer_qty")
	if value in (None, ""):
		value = row.get("qty")
	return flt(value)


def _row_basic_amount(row) -> float:
	value = row.get("basic_amount")
	if value in (None, ""):
		value = row.get("amount")
	return abs(flt(value))


def _is_costed_out_output(row) -> bool:
	"""Incoming scrap / costed-out secondary — mirrors ERPNext costed-out-of-FG."""
	if row.get("is_finished_item"):
		return False
	if not row.get("t_warehouse"):
		return False
	if row.get("is_scrap_item"):
		return True
	sec = str(row.get("secondary_item_type") or row.get("type") or "").lower()
	if sec in ("scrap", "by-product", "byproduct"):
		return True
	return str(row.get("valuation_type") or "") in ("Valuation Rate", "Manual")


# Same threshold as MANUFACTURE_SCRAP_FG_RECONSTRUCTION. Warehouse scrap
# above this multiple of same-voucher consume rate is exploded — but only
# rewritten when it would make FG residual negative (37090 stays intact).
EXPLODED_SAME_ITEM_SCRAP_RATIO = 2.0


def correct_exploded_same_item_scrap_for_riv(doc) -> bool:
	"""RIV recalculate must not re-price same-item scrap from an exploded warehouse.

	``calculate_rate_and_amount`` fetches scrap from ``get_valuation_rate(t_warehouse)``.
	When that warehouse moving average is itself exploded, scrap consumes more
	than the source pool and FG goes negative (I2).

	Authority: same-voucher consume ``basic_rate`` for the same item.
	Gate: rewrite only when current scrap amounts make FG residual negative.
	Healthy high warehouse scrap (37090-class) is left unchanged.
	"""
	if getattr(doc, "doctype", None) != "Stock Entry":
		return False
	if getattr(doc, "purpose", None) != "Manufacture":
		return False
	try:
		irr = is_irr_company(getattr(doc, "company", None))
	except Exception:
		# bench-less unit tests / missing cache — do not rewrite
		return False
	if not irr:
		return False

	items = doc.get("items") or []
	fg_rows = [row for row in items if row.get("is_finished_item") and row.get("t_warehouse")]
	if len(fg_rows) != 1:
		return False

	consume_rate_by_item: dict[str, float] = {}
	outgoing = 0.0
	for row in items:
		if not row.get("s_warehouse"):
			continue
		qty = _row_qty(row)
		amt = _row_basic_amount(row)
		rate = abs(flt(row.get("basic_rate")))
		if rate <= 0 and qty > 0 and amt > 0:
			rate = amt / qty
		outgoing += amt
		item = row.get("item_code")
		if item and qty > 0 and rate > 0:
			consume_rate_by_item.setdefault(str(item), rate)

	scrap_rows = [row for row in items if _is_costed_out_output(row)]
	if not scrap_rows:
		return False

	scrap_value = sum(_row_basic_amount(row) for row in scrap_rows)
	fg = fg_rows[0]
	residual = outgoing - scrap_value
	fg_negative = (
		residual < -1.0
		or flt(fg.get("basic_rate")) < -1.0
		or flt(fg.get("basic_amount")) < -1.0
		or flt(fg.get("amount")) < -1.0
	)
	if not fg_negative:
		return False

	currency = get_company_currency(doc.company)
	changed = False
	for row in scrap_rows:
		item = str(row.get("item_code") or "")
		peer_rate = consume_rate_by_item.get(item)
		if not peer_rate:
			continue
		qty = _row_qty(row)
		current_rate = abs(flt(row.get("basic_rate")))
		if current_rate <= peer_rate * EXPLODED_SAME_ITEM_SCRAP_RATIO:
			continue
		material = round_currency(peer_rate * qty, currency)
		row.basic_rate = round_monetary_rate(peer_rate, currency)
		row.valuation_rate = row.basic_rate
		row.basic_amount = material
		row.amount = round_currency(material + _row_capitalized_cost(row), currency)
		if hasattr(row, "allow_zero_valuation_rate"):
			row.allow_zero_valuation_rate = 0
		changed = True

	if not changed:
		return False

	other_incoming = sum(
		_row_basic_amount(row) for row in items if row.get("t_warehouse") and row is not fg
	)
	material = round_currency(outgoing - other_incoming, currency)
	qty = _row_qty(fg)
	if material < 0 or qty <= 0:
		_refresh_header_totals(doc)
		return True

	fg.basic_amount = material
	fg.basic_rate = round_monetary_rate(material / qty, currency)
	fg.amount = round_currency(material + _row_capitalized_cost(fg), currency)
	fg.valuation_rate = integer_valuation_rate_from_amount(fg.amount, qty, currency)
	_refresh_header_totals(doc)
	return True


def align_manufacture_finished_good_residual(doc) -> None:
	"""Single-FG Manufacture: absorb only true IRR rounding residuals.

	Expected economic identity (simple single FG):
	  incoming ≈ outgoing + FG capitalized costs (additional_cost + LCV)

	Never force Incoming = Outgoing or value_difference = 0 when capitalized
	cost exceeds residual tolerance. Multi-FG is skipped. Repack is skipped
	(purpose must be Manufacture).

	Under TYPE C (v5.3.34+ Product Reject whole-IRR allocation residual), do
	NOT close the pool into FG amount — that residual is intentional and posts
	via native Stock Adjustment.

	IRR residual rule for valuation_rate:
	  amount remains authoritative; valuation_rate = ROUND_HALF_UP(amount/qty);
	  residual = amount − valuation_rate × qty may be non-zero (±1 typical).
	"""
	if doc.doctype != "Stock Entry" or doc.purpose != "Manufacture":
		return
	if not is_irr_company(doc.company):
		return

	from erpnext_extensions.iran_accounting.domain.manufacture_irr_residual import (
		CLASS_TYPE_C,
		classify_manufacture_irr_residual,
		uses_type_c_sa_residual_policy,
	)

	# TYPE C policy: contract owns Product Reject / equal-rate composition.
	# CLASS_TYPE_C leftover posts via Stock Adjustment — never close into FG.
	# With real header operating cost spread across FG + Product Reject, classify
	# may return TYPE_A_REAL_ADDITIONAL_COST; pool-closing into FG still destroys
	# the equal issued rate (historical MAT-STE Product Reject + operating cost).
	if uses_type_c_sa_residual_policy(doc):
		result = classify_manufacture_irr_residual(doc)
		if result.classification == CLASS_TYPE_C:
			_refresh_header_totals(doc)
			return
		from erpnext_extensions.iran_accounting.scrap_costing import _has_product_reject

		if _has_product_reject(doc):
			fg_only = [
				row
				for row in (doc.get("items") or [])
				if row.get("is_finished_item") and row.get("t_warehouse")
			]
			if len(fg_only) == 1:
				currency = get_company_currency(doc.company)
				qty = flt(
					fg_only[0].transfer_qty
					if fg_only[0].get("transfer_qty") not in (None, "")
					else fg_only[0].get("qty")
				)
				if qty:
					_apply_integer_rates(fg_only[0], qty, currency)
			_refresh_header_totals(doc)
			return

	# Stage-equivalent allocation already owns and closes the material and
	# operating-cost pools for participating Co-Products. Legacy FG residual
	# pool-close must not reconsume Co-Product capitalized cost as material
	# (other_incoming uses row.amount, which embeds additional_cost).
	# Keep the 5.5.18 guard; also honour unified closed-plan ownership (Phase 1
	# SAME_ITEM_MULTI_FG and STAGE_CO claim stamps).
	from erpnext_extensions.iran_accounting.manufacture_output_contract import (
		is_allocation_closed,
		polish_closed_plan_after_align,
	)
	from erpnext_extensions.iran_accounting.manufacture_stage_costing import (
		has_stage_participating_co_product,
	)

	if is_allocation_closed(doc) or has_stage_participating_co_product(doc):
		fg_only = [
			row
			for row in (doc.get("items") or [])
			if row.get("is_finished_item") and row.get("t_warehouse")
		]
		currency = get_company_currency(doc.company)
		# Closed Multi-FG: polish integer rates on every owned FG row without
		# recomposing basic_amount from outgoing − peer amounts.
		for fg in fg_only:
			qty = flt(
				fg.transfer_qty if fg.get("transfer_qty") not in (None, "") else fg.get("qty")
			)
			if qty:
				_apply_integer_rates(fg, qty, currency)
		_refresh_header_totals(doc)
		polish_closed_plan_after_align(doc)
		return

	fg_rows = [
		row
		for row in doc.get("items") or []
		if row.get("is_finished_item") and row.get("t_warehouse")
	]
	if len(fg_rows) != 1:
		return

	currency = get_company_currency(doc.company)
	tol = _residual_tolerance(currency)

	outgoing_total = sum(flt(row.amount) for row in doc.get("items") or [] if row.get("s_warehouse"))

	fg = fg_rows[0]
	qty = flt(fg.transfer_qty if fg.get("transfer_qty") not in (None, "") else fg.get("qty"))
	if not qty:
		return

	capitalized = _row_capitalized_cost(fg)
	# Other incoming rows (scrap/secondary) keep their amounts — compute from
	# peer rows, not from (incoming_total - fg.amount), so pool restore is stable
	# even when the current FG amount is the wrong qty×rate figure.
	other_incoming = sum(
		flt(row.amount)
		for row in doc.get("items") or []
		if row.get("t_warehouse") and row is not fg
	)
	if capitalized >= tol:
		# Pool identity with capitalization (incl. add_cost == IRR quantum):
		#   FG.amount + other_incoming = outgoing + FG capitalized costs
		# Rate-first align alone yields qty×integer_rate + add_cost, which can
		# drop a valid Manufacture residual (v5.3.30). Restore the pool here so
		# additional_cost >= IRR tolerance cannot skip residual absorption.
		pool_target = round_currency(outgoing_total - other_incoming + capitalized, currency)
		expected_basic = round_currency(pool_target - capitalized, currency)
		needs_pool = abs(flt(fg.amount) - pool_target) > tol or abs(flt(fg.basic_amount) - expected_basic) > tol
		if needs_pool:
			fg.basic_amount = expected_basic
			fg.basic_rate = round_monetary_rate(
				flt(expected_basic / qty) if qty else fg.basic_rate, currency
			)
			fg.amount = pool_target
			fg.valuation_rate = integer_valuation_rate_from_amount(fg.amount, qty, currency)
			_refresh_header_totals(doc)
			return
		_apply_integer_rates(fg, qty, currency)
		_refresh_header_totals(doc)
		return

	# No material capitalization on FG: absorb Incoming vs Outgoing only within residual tol.
	incoming_total = other_incoming + flt(fg.amount)
	delta = abs(incoming_total - outgoing_total)
	if delta > tol:
		_apply_integer_rates(fg, qty, currency)
		_refresh_header_totals(doc)
		return
	if delta == 0:
		_apply_integer_rates(fg, qty, currency)
		_refresh_header_totals(doc)
		return

	# Align FG so incoming equals outgoing (true ±1 IRR case) — TYPE B only.
	target_fg = round_currency(outgoing_total - other_incoming, currency)
	_apply_fg_amount(fg, target_fg, qty, currency)
	# Keep basic_* consistent with material-only target when no capitalized cost.
	fg.basic_amount = target_fg
	fg.basic_rate = round_monetary_rate(flt(target_fg / qty) if qty else fg.basic_rate, currency)
	_refresh_header_totals(doc)


def _apply_integer_rates(fg, qty: float, currency: str) -> None:
	"""Amount authoritative; integer valuation_rate; residual may be non-zero."""
	if fg.get("basic_rate") is not None:
		fg.basic_rate = round_monetary_rate(fg.basic_rate, currency)
	if qty and flt(fg.amount):
		fg.valuation_rate = integer_valuation_rate_from_amount(fg.amount, qty, currency)
	elif fg.get("valuation_rate") is not None:
		fg.valuation_rate = round_monetary_rate(fg.valuation_rate, currency)


def _apply_fg_amount(fg, amount: float, qty: float, currency: str) -> None:
	fg.amount = round_currency(amount, currency)
	capitalized = _row_capitalized_cost(fg)
	if capitalized > _residual_tolerance(currency):
		# Preserve ERPNext basic_* ownership; only amount/valuation absorb residual.
		_apply_integer_rates(fg, qty, currency)
		return
	fg.basic_amount = fg.amount
	fg.basic_rate = round_monetary_rate(flt(fg.amount / qty) if qty else fg.basic_rate, currency)
	fg.valuation_rate = integer_valuation_rate_from_amount(fg.amount, qty, currency)


def _refresh_header_totals(doc) -> None:
	inc = sum(flt(d.amount) for d in doc.get("items") or [] if d.get("t_warehouse"))
	out = sum(flt(d.amount) for d in doc.get("items") or [] if d.get("s_warehouse"))
	doc.total_incoming_value = inc
	doc.total_outgoing_value = out
	doc.value_difference = flt(inc) - flt(out)


# Backward-compatible name used by hooks / repost.
def align_manufacture_finished_good_to_outgoing(doc) -> None:
	align_manufacture_finished_good_residual(doc)

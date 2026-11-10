# Copyright (c) 2026, ERPNext Extensions contributors
"""Physical Two-Slice allocator for integer IRR batch outgoing valuation (v5.6.10).

Policy:
- out_value = ROUND_HALF_UP(pool_value × out_qty / pool_qty); full deplete → pool_value
- Represent as slices at integer rates R and/or R+1 with rate×qty identity on each slice
- R = mathematical floor(pool_value/pool_qty) via ROUND_FLOOR — never Decimal //
  (Decimal // truncates toward 0; breaks negative pools — HRC-00015 / v5.7.22)
- Historical fractional pools are refused (no silent Stock Adjustment)
- Fractional quantities: single integer rate with RHU(qty×rate) == out_value, else fail closed
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal, getcontext
from typing import Sequence

getcontext().prec = 80


def _d(value) -> Decimal:
	return Decimal(str(value if value is not None else 0))


def _rhu(value: Decimal) -> Decimal:
	return value.quantize(Decimal("1"), rounding=ROUND_HALF_UP)


def _floor_div(numerator: Decimal, denominator: Decimal) -> int:
	"""Mathematical floor(numerator/denominator) as int.

	``Decimal.__floordiv__`` follows ``divmod`` remainder-sign rules and truncates
	toward zero for negative dividends — NOT toward −∞. Two-slice rates require
	true floor so ``out_v ∈ [oq·R, oq·(R+1)]`` holds for negative pools
	(HRC-00015 / ``n_high out of range``).
	"""
	if denominator == 0:
		raise AllocationError("floor_div by zero")
	return int((numerator / denominator).to_integral_value(rounding=ROUND_FLOOR))


@dataclass(frozen=True)
class ValuationSlice:
	qty: Decimal  # signed stock qty (outgoing negative)
	rate: int
	amount: Decimal  # signed SVD

	def abs_qty(self) -> Decimal:
		return abs(self.qty)


@dataclass(frozen=True)
class AllocationPlan:
	pool_qty: Decimal
	pool_value: Decimal
	out_qty_abs: Decimal
	out_value_abs: Decimal
	base_rate: int
	slices: tuple[ValuationSlice, ...]
	mode: str  # "two_slice" | "single" | "full_deplete"


class AllocationError(Exception):
	"""Allocator could not satisfy the integer IRR contract."""


def assert_slice_identity(slice_: ValuationSlice) -> None:
	"""Whole-qty slices: amount == qty × rate. Fractional qty: RHU form."""
	aq = abs(slice_.qty)
	if aq == aq.to_integral_value():
		product = _d(slice_.rate) * slice_.qty
		if product != slice_.amount:
			raise AssertionError(
				f"identity fail: qty={slice_.qty} rate={slice_.rate} "
				f"product={product} amount={slice_.amount}"
			)
	else:
		if _rhu(aq * _d(slice_.rate)) != abs(slice_.amount):
			raise AssertionError(
				f"fractional RHU identity fail: qty={slice_.qty} rate={slice_.rate} "
				f"amount={slice_.amount}"
			)
	if slice_.rate != int(slice_.rate):
		raise AssertionError(f"non-integer rate {slice_.rate}")
	if slice_.amount != slice_.amount.to_integral_value():
		raise AssertionError(f"non-integer amount {slice_.amount}")


def allocate_physical_two_slice(
	pool_qty,
	pool_value,
	out_qty,
	*,
	outgoing_sign: int = -1,
) -> AllocationPlan:
	"""Allocate integer IRR slices for an outgoing batch movement."""
	pq = _d(pool_qty)
	pv = _d(pool_value)
	oq = abs(_d(out_qty))
	if pq <= 0:
		raise AllocationError(f"non-positive pool_qty={pq}")
	if oq <= 0:
		raise AllocationError(f"non-positive out_qty={oq}")
	if oq - pq > Decimal("1e-12"):
		raise AllocationError(f"out_qty {oq} exceeds pool_qty {pq}")
	if pv != pv.to_integral_value():
		raise AllocationError(
			f"historical fractional pool_value={pv}; repair required before allocate"
		)
	if oq != oq.to_integral_value():
		return _allocate_fractional_qty(pq, pv, oq, outgoing_sign=outgoing_sign)

	sign = Decimal(-1 if outgoing_sign < 0 else 1)
	full = abs(oq - pq) <= Decimal("1e-12")
	if full:
		out_v_signed = pv
		mode = "full_deplete"
	else:
		out_v_signed = _rhu(pv * oq / pq)
		mode = "two_slice"

	# True mathematical floor — do NOT use Decimal // (truncates toward 0).
	R = _floor_div(pv, pq)
	n_high = int(out_v_signed - oq * R)
	if n_high < 0 or n_high > int(oq):
		raise AllocationError(
			f"n_high out of range: out_v={out_v_signed} R={R} oq={oq} n={n_high}"
		)

	n_base = int(oq) - n_high
	slices: list[ValuationSlice] = []
	if n_base:
		qty = sign * Decimal(n_base)
		amt = qty * Decimal(R)
		slices.append(ValuationSlice(qty=qty, rate=R, amount=amt))
	if n_high:
		qty = sign * Decimal(n_high)
		amt = qty * Decimal(R + 1)
		slices.append(ValuationSlice(qty=qty, rate=R + 1, amount=amt))

	if not slices:
		raise AllocationError("empty slices")

	for s in slices:
		assert_slice_identity(s)

	total = sum((s.amount for s in slices), Decimal(0))
	# Slice amounts carry outgoing_sign; out_v_signed carries pool-value sign.
	# Magnitudes must match (positive pools: both sides positive magnitude).
	if abs(total) != abs(out_v_signed):
		raise AllocationError(f"slice sum {abs(total)} != out_v {abs(out_v_signed)}")

	if len(slices) == 1:
		mode = "single" if not full else mode

	return AllocationPlan(
		pool_qty=pq,
		pool_value=pv,
		out_qty_abs=oq,
		out_value_abs=abs(out_v_signed),
		base_rate=R,
		slices=tuple(slices),
		mode=mode,
	)


def _allocate_fractional_qty(pq, pv, oq, *, outgoing_sign: int) -> AllocationPlan:
	"""Single-rate attempt: integer R with RHU(q×R) == RHU(exact pool share)."""
	exact = _rhu(pv * oq / pq)
	sign = Decimal(-1 if outgoing_sign < 0 else 1)
	R0 = _floor_div(pv, pq)
	found = None
	for delta in range(0, 20):
		candidates = {R0} if delta == 0 else {R0 - delta, R0 + delta}
		for cand in candidates:
			if cand == 0:
				continue
			# Positive pools keep legacy (cand > 0). Negative pools allow negative rates.
			if pv >= 0 and cand <= 0:
				continue
			if _rhu(oq * Decimal(cand)) == exact:
				found = cand
				break
		if found is not None:
			break
	if found is None:
		raise AllocationError(
			f"fractional qty {oq}: no integer rate satisfies "
			f"ROUND_HALF_UP(qty×rate)=={exact}"
		)
	qty = sign * oq
	# Fractional qty: amount is RHU(qty×|rate|) with outgoing sign and rate sign.
	mag = _rhu(oq * Decimal(abs(found)))
	amt = sign * mag * (Decimal(1) if found > 0 else Decimal(-1))
	slice_ = ValuationSlice(qty=qty, rate=found, amount=amt)
	assert_slice_identity(slice_)
	return AllocationPlan(
		pool_qty=pq,
		pool_value=pv,
		out_qty_abs=oq,
		out_value_abs=abs(exact),
		base_rate=found,
		slices=(slice_,),
		mode="single",
	)


def simulate_sequential(
	pool_qty,
	pool_value,
	out_qtys: Sequence,
) -> list[tuple[AllocationPlan, Decimal, Decimal]]:
	"""Return list of (plan, rem_qty, rem_value) after each movement."""
	q, v = _d(pool_qty), _d(pool_value)
	out = []
	for oq in out_qtys:
		plan = allocate_physical_two_slice(q, v, oq)
		q = q - plan.out_qty_abs
		# Slice amounts are signed SVD; adding them updates remaining pool value
		# for both positive and negative pools.
		v = v + sum((s.amount for s in plan.slices), Decimal(0))
		out.append((plan, q, v))
	return out

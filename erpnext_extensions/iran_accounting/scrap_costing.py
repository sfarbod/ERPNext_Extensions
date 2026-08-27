# Copyright (c) 2026, ERPNext Extensions contributors
"""Absorbed-cost valuation for manufacturing scrap.

A unit rejected at an operation consumed exactly the same materials and the
same operations as a good one, so its cost per unit is identical. Stock
ERPNext gives scrap either a fixed BOM percentage — correct only when actual
scrap equals the plan — or, when the BOM defines no secondary items at all,
nothing whatsoever. In the latter case 100% of the cost pool lands on the good
units: the finished good is overstated and the scrap enters stock as a
worthless asset.

Why this lives in application code rather than a Server Script
--------------------------------------------------------------
The absorbed rate can only be computed once ERPNext has priced the consumed
rows, which happens inside ``StockEntry.validate()`` →
``calculate_rate_and_amount()``. But ERPNext *also* raises

    Valuation Rate for the Item ..., is required to do accounting entries

from inside that same ``validate()`` whenever an outgoing row has no rate and
no valuation history in the target warehouse. A Server Script bound to
``before_submit`` therefore never executes on exactly the documents that need
it, and one bound to ``before_validate`` runs too early to see any prices.

Two application hooks close that gap:

``before_validate``
    :func:`permit_scrap_zero_valuation` marks unpriced scrap rows so ERPNext's
    own check lets the document through. Nothing is valued here.

``validate``
    :func:`allocate_scrap_absorbed_cost` runs after the controller's
    ``validate()`` (doc_events hooks are composed after the class method), so
    the consumed rows carry real amounts. It splits the pool and clears the
    permission flag again, so nothing zero-valued ever reaches the ledger.

What absorbs the product's cost
-------------------------------
Only a reject that is a unit of the operation's OWN output — same item code as
the finished good, or a legacy ``Z`` form of it. A Job Card's scrap table also
carries rejected COMPONENTS (5 broken stoppers beside 100 rejected syringes),
and those are material coming back out of WIP: they keep the rate they were
issued at on the same document. Pricing a stopper at the product's absorbed
rate overstated it roughly fivefold and took the difference out of the
finished good.

Scope
-----
Only rate/amount fields on the *output* rows are written. Quantities, batch
allocation, substitutions, ``fg_completed_qty``, ``process_loss_qty``,
``process_loss_percentage``, transferred/consumed quantities and Job Card
identity are all left strictly alone.
"""

from __future__ import annotations

import frappe
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

SCRAP_ROW_TYPE = "Scrap"


def _is_incoming(row) -> bool:
	return bool(row.get("t_warehouse")) and not row.get("s_warehouse")


def secondary_item_type_of(row) -> str | None:
	"""ERPNext 16.33+ ``secondary_item_type``, with legacy ``type`` fallback.

	Prefer the renamed field when set; otherwise use legacy ``type`` so pre-16.33
	rows and unit stubs keep working. A non-empty non-scrap secondary type wins
	over a stale legacy ``type`` value.
	"""
	if row is None:
		return None
	secondary = row.get("secondary_item_type")
	if secondary:
		return secondary
	legacy = row.get("type")
	return legacy or None


def is_scrap_row(row) -> bool:
	"""A rejected-output row, by explicit type or by the site's Z-code convention."""
	if row.get("is_finished_item"):
		return False
	if not _is_incoming(row):
		return False
	if secondary_item_type_of(row) == SCRAP_ROW_TYPE:
		return True
	if row.get("is_legacy_scrap_item"):
		return True
	item_code = row.get("item_code")
	if not item_code:
		return False
	# Scrap items on this site are independent items carrying the code of the
	# material they came from (13100018 -> Z13100018).
	return bool(frappe.db.get_value("Item", item_code, "custom_main_item_code"))


def permit_scrap_zero_valuation(doc, method=None) -> None:
	"""Let an unpriced scrap row survive ERPNext's own valuation check.

	Without this the document dies inside ``validate()`` and the absorbed cost
	is never computed. The flag is removed again by
	:func:`allocate_scrap_absorbed_cost` once a real rate exists.
	"""
	if doc.doctype != "Stock Entry" or doc.purpose != "Manufacture":
		return
	for row in doc.get("items") or []:
		if not is_scrap_row(row):
			continue
		if flt(row.get("basic_rate")) or flt(row.get("valuation_rate")):
			continue
		row.allow_zero_valuation_rate = 1


def _integer_rate_pair(pool: float, good_qty: float, scrap_qty: float) -> tuple[float, float, float] | None:
	"""Whole-unit rates for both output rows, nearest to parity.

	The IRR ledger contract compares each row's ``rate x qty`` against its
	amount, so BOTH rates must be whole units — a remainder may never be dumped
	into one row's amount. That makes an exact split of the pool impossible in
	general: with 92 good and 2 scrap, ``92 x rate`` is always even, so an odd
	pool can never be consumed exactly.

	The search therefore minimises the leftover instead of demanding zero. The
	leftover is bounded by ``gcd(good_qty, scrap_qty) / 2`` — a few rial against
	hundreds of millions — and surfaces as the document's ordinary rounding
	residual rather than as a distortion of either rate.

	Returns ``(good_rate, scrap_rate, leftover)``.
	"""
	units = good_qty + scrap_qty
	if units <= 0:
		return None
	parity = pool / units
	target = round(parity)
	best = None
	# One full period of scrap_qty covers every reachable residue.
	span = int(scrap_qty) + 2
	for offset in range(span + 1):
		for candidate in (target,) if offset == 0 else (target - offset, target + offset):
			if candidate <= 0:
				continue
			rest = pool - good_qty * candidate
			if rest <= 0:
				continue
			scrap_rate = round(rest / scrap_qty)
			if scrap_rate <= 0:
				continue
			leftover = pool - good_qty * candidate - scrap_qty * scrap_rate
			if best is None or abs(leftover) < abs(best[2]):
				best = (float(candidate), float(scrap_rate), float(leftover))
			if leftover == 0:
				return best
	return best


def _row_qty(row) -> float:
	value = row.get("transfer_qty")
	if value in (None, ""):
		value = row.get("qty")
	return flt(value)


def _capitalized(row) -> float:
	"""Operating cost and landed cost ERPNext capitalises onto an output row."""
	return flt(row.get("additional_cost")) + flt(row.get("landed_cost_voucher_amount"))


def _apply(row, rate: float, currency: str) -> None:
	"""Set the material valuation, preserving anything capitalised onto the row.

	``basic_amount`` is the material share; ``amount`` additionally carries the
	capitalised operating cost, which keeps the document's economic identity

	    incoming = outgoing + capitalised

	intact. Overwriting ``amount`` with rate x qty would silently discard the
	Work Order's operating cost — on a real product that is tens of millions of
	rial and trips the ledger contract.
	"""
	qty = flt(row.get("transfer_qty") if row.get("transfer_qty") not in (None, "") else row.get("qty"))
	material = round_currency(rate * qty, currency)
	row.basic_rate = rate
	row.valuation_rate = rate
	row.basic_amount = material
	row.amount = round_currency(material + _capitalized(row), currency)
	# A real rate exists now, so the permission granted in before_validate is
	# withdrawn rather than left to mask genuine valuation errors later.
	row.allow_zero_valuation_rate = 0


def is_product_reject(row, finished_item: str | None) -> bool:
	"""A reject that is a unit of THIS operation's own output.

	Only these absorb the product's cost. A Job Card's scrap table also carries
	rejected components — 5 stoppers alongside 100 rejected syringes — and a
	stopper priced at the product's absorbed rate is overstated several fold
	while the finished good is understated by the same amount.
	"""
	if not finished_item:
		return False
	item_code = row.get("item_code")
	if item_code == finished_item:
		return True
	# a legacy Z form of the same product (Z30100023 -> 30100023)
	return frappe.db.get_value("Item", item_code, "custom_main_item_code") == finished_item


def _issued_rate(doc, item_code: str) -> float:
	"""The rate this material left WIP at on this very document.

	A rejected component is the same material coming back out, so it carries
	the value it was issued at. Nothing is invented: if the document does not
	consume that item, no rate is returned and ERPNext's own figure stands.
	"""
	qty = 0.0
	amount = 0.0
	for row in doc.get("items") or []:
		if not row.get("s_warehouse") or row.get("t_warehouse"):
			continue
		if row.get("item_code") != item_code:
			continue
		value = row.get("transfer_qty")
		if value in (None, ""):
			value = row.get("qty")
		qty += flt(value)
		amount += flt(row.get("basic_amount"))
	return (amount / qty) if qty > 0 else 0.0


def _spread_operating_cost(product_rows, currency, leftover: float = 0.0) -> None:
	"""Share capitalised operating cost over every unit that was processed.

	ERPNext's ``distribute_additional_costs`` puts operating cost only on rows
	flagged ``is_finished_item`` and explicitly zeroes it everywhere else, so a
	rejected unit absorbs none of it. But a reject ran through the same
	operation for the same time as a good one — that is the whole basis of
	absorbed costing — so it carries the same operating cost per unit.

	The total is unchanged, only its distribution, which keeps the document's
	identity ``incoming = outgoing + capitalised`` intact. Only rejects OF THE
	PRODUCT share it; a rejected component never entered the operation as a
	unit of output and keeps whatever it already had.
	"""
	total = sum(flt(row.get("additional_cost")) for row in product_rows)
	units = sum(_row_qty(row) for row in product_rows)
	if units <= 0:
		return

	if total > 0:
		# The finished good takes the rounding remainder, so the total is
		# preserved to the rial rather than drifting by the number of scrap rows.
		assigned = 0.0
		for row in product_rows[1:]:
			share = round_currency(total * _row_qty(row) / units, currency)
			row.additional_cost = share
			assigned += share
		product_rows[0].additional_cost = round_currency(total - assigned, currency)

	# Absorb the integer-rate leftover here rather than letting it reach the
	# ledger. Whole-rial rates cannot consume the pool exactly — with 650 good
	# and 100 rejected units every reachable total is a multiple of gcd = 50
	# while the pool is 31 mod 50, so 19 rial is the closest any rate pair can
	# come. Moving a rate to close that gap would misstate a unit cost.
	#
	# `additional_cost` is the one field the ledger contract leaves free
	# (`amount = basic_amount + additional_cost + LCV`, and `basic_amount`
	# stays exactly `qty x integer rate`). Nudging it by the leftover makes
	# incoming equal outgoing + capitalised to the rial, so nothing lands in
	# Stock Adjustment; the effect is simply that those few rial of operating
	# cost stay expensed instead of being capitalised into inventory.
	if leftover:
		absorbed = flt(product_rows[0].get("additional_cost")) + leftover
		if absorbed >= 0:
			product_rows[0].additional_cost = round_currency(absorbed, currency)

	for row in product_rows:
		row.amount = round_currency(
			flt(row.get("basic_amount")) + _capitalized(row), currency
		)

def allocate_scrap_absorbed_cost(doc, method=None) -> bool:
	"""Split the manufacturing cost pool between finished good and scrap.

	Returns ``True`` when an allocation was applied, so callers can tell an
	untouched document from a costed one.

	Process loss takes no share: only units that physically exist at the end of
	the operation (good + scrap) receive cost, which is precisely how the cost
	of the lost units ends up carried by the survivors.
	"""
	if doc.doctype != "Stock Entry" or doc.purpose != "Manufacture":
		return False
	if not is_irr_company(doc.company):
		return False

	rows = doc.get("items") or []
	rejects = [row for row in rows if is_scrap_row(row)]
	if not rejects:
		return False

	good_rows = [row for row in rows if row.get("is_finished_item") and row.get("t_warehouse")]
	if len(good_rows) != 1:
		# Multi-FG output is a different allocation question (co-products carry
		# their own basis); leave ERPNext's own numbers alone.
		return False

	finished_item = good_rows[0].get("item_code")
	scrap_rows = [row for row in rejects if is_product_reject(row, finished_item)]
	component_rows = [row for row in rejects if row not in scrap_rows]

	# A rejected component is material coming back out of WIP, so it keeps the
	# value it was issued at rather than a share of the product's cost. Priced
	# here so it is removed from the pool below like any other incoming row.
	currency_code = get_company_currency(doc.company)
	for row in component_rows:
		rate = _issued_rate(doc, row.get("item_code"))
		if rate > 0:
			_apply(row, round_monetary_rate(rate, currency_code), currency_code)

	if not scrap_rows:
		# Component rejects only: nothing absorbs the product's cost, and the
		# finished good keeps every rial ERPNext gave it.
		return bool(component_rows)

	def _qty(row) -> float:
		value = row.get("transfer_qty")
		if value in (None, ""):
			value = row.get("qty")
		return flt(value)

	good_qty = _qty(good_rows[0])
	scrap_qty = sum(_qty(row) for row in scrap_rows)
	if good_qty <= 0 or scrap_qty <= 0:
		return False

	# The material pool. Capitalised operating cost is not mixed in here — it
	# keeps ERPNext's own semantics of riding on `amount` rather than
	# `basic_amount` — but it IS shared across the processed units afterwards
	# by _spread_operating_cost.
	outgoing = sum(flt(row.get("basic_amount")) for row in rows if row.get("s_warehouse"))
	# Co-Product / By-Product / Additional Finished Good and rejected components
	# keep their own allocation and are removed from the pool before it is split.
	other_incoming = sum(
		flt(row.get("basic_amount"))
		for row in rows
		if _is_incoming(row) and row not in scrap_rows and row is not good_rows[0]
	)
	pool = outgoing - other_incoming
	if pool <= 0:
		return False

	currency = get_company_currency(doc.company)
	precision = get_currency_precision(currency)

	if precision != 0:
		rate = round_monetary_rate(pool / (good_qty + scrap_qty), currency)
		_apply(good_rows[0], rate, currency)
		for row in scrap_rows:
			_apply(row, rate, currency)
		_spread_operating_cost([good_rows[0]] + scrap_rows, currency)
		return True

	pair = _integer_rate_pair(pool, good_qty, scrap_qty)
	if pair is None:
		return False

	good_rate, scrap_rate, leftover = pair
	# Both rows keep rate x qty == amount exactly, which is what the ledger
	# contract checks. Any leftover stays in the document's rounding residual
	# and is absorbed by align_manufacture_finished_good_residual.
	_apply(good_rows[0], good_rate, currency)
	for row in scrap_rows:
		_apply(row, scrap_rate, currency)
	_spread_operating_cost([good_rows[0]] + scrap_rows, currency, leftover)
	return True

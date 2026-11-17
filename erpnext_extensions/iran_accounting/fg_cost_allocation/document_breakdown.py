# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Document-level Account Control breakdown for MFG Cost Product Allocation.

Methodology (quantity-aware, no independent allocation engine):
  1. Take Batch × Expense allocated amounts grouped by explicit ``account_control``.
  2. Scale by the product's existing ``product_share`` to get product-level
     overhead (211237) and labor (211238) amounts.
  3. Split each category across Manufacture Stock Entries by that product's
     finished-goods quantity on each source document.
  4. Absorb currency residual into the largest-qty document per category.

Missing / blank Account Control amounts are excluded from both categories
(never inferred from Account hierarchy or names).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable

from erpnext_extensions.iran_accounting.domain.currency import get_currency_precision

OVERHEAD_ACCOUNT_CONTROL_PREFIX = "211237"
LABOR_ACCOUNT_CONTROL_PREFIX = "211238"


def _money(value: Decimal, precision: int) -> Decimal:
	q = Decimal("1") if precision <= 0 else Decimal("1").scaleb(-precision)
	return Decimal(value).quantize(q, rounding=ROUND_HALF_UP)


def classify_account_control(account_control: str | None) -> str | None:
	"""Return 'overhead', 'labor', or None. Never infers from original Account."""
	ac = (account_control or "").strip()
	if not ac:
		return None
	# Match Account name / number prefix (e.g. "211237 - کنترل سربار - E").
	code = ac.split(" - ", 1)[0].strip()
	if code == OVERHEAD_ACCOUNT_CONTROL_PREFIX or ac.startswith(OVERHEAD_ACCOUNT_CONTROL_PREFIX):
		return "overhead"
	if code == LABOR_ACCOUNT_CONTROL_PREFIX or ac.startswith(LABOR_ACCOUNT_CONTROL_PREFIX):
		return "labor"
	return None


def sum_batch_expense_by_account_control(
	rows: Iterable[dict],
) -> tuple[Decimal, Decimal, Decimal]:
	"""Return (overhead, labor, other_or_blank) from Batch Expense rows.

	Each row must expose ``account_control`` and ``allocated_amount``.
	"""
	overhead = Decimal("0")
	labor = Decimal("0")
	other = Decimal("0")
	for row in rows:
		amt = Decimal(str(row.get("allocated_amount") or 0))
		kind = classify_account_control(row.get("account_control"))
		if kind == "overhead":
			overhead += amt
		elif kind == "labor":
			labor += amt
		else:
			other += amt
	return overhead, labor, other


def scale_to_product(batch_amount: Decimal, product_share: Decimal, precision: int) -> Decimal:
	return _money(batch_amount * Decimal(str(product_share or 0)), precision)


@dataclass(frozen=True)
class DocumentBreakdownRow:
	source_document_type: str
	source_document: str
	quantity: Decimal
	overhead_amount: Decimal
	labor_amount: Decimal
	total_allocated_amount: Decimal


@dataclass(frozen=True)
class DocumentBreakdownResult:
	rows: list[DocumentBreakdownRow]
	total_quantity: Decimal
	total_overhead: Decimal
	total_labor: Decimal
	total_allocated_amount: Decimal


def build_document_breakdown(
	*,
	stock_entry_qtys: dict[str, Decimal],
	batch_overhead: Decimal,
	batch_labor: Decimal,
	product_share: Decimal,
	currency: str,
) -> DocumentBreakdownResult:
	"""Split product-level Account Control amounts across source documents by FG qty."""
	precision = get_currency_precision(currency)
	prod_oh = scale_to_product(Decimal(str(batch_overhead or 0)), product_share, precision)
	prod_lab = scale_to_product(Decimal(str(batch_labor or 0)), product_share, precision)

	entries = [
		(se, Decimal(str(qty)))
		for se, qty in sorted((stock_entry_qtys or {}).items(), key=lambda x: x[0])
		if se and Decimal(str(qty)) > 0
	]
	if not entries:
		return DocumentBreakdownResult(
			rows=[],
			total_quantity=Decimal("0"),
			total_overhead=Decimal("0"),
			total_labor=Decimal("0"),
			total_allocated_amount=Decimal("0"),
		)

	total_qty = sum((q for _, q in entries), Decimal("0"))
	oh_parts: list[tuple[str, Decimal, Decimal]] = []
	lab_parts: list[tuple[str, Decimal]] = []
	oh_sum = lab_sum = Decimal("0")
	for se, qty in entries:
		share = (qty / total_qty) if total_qty else Decimal("0")
		oh = _money(prod_oh * share, precision)
		lab = _money(prod_lab * share, precision)
		oh_parts.append((se, qty, oh))
		lab_parts.append((se, lab))
		oh_sum += oh
		lab_sum += lab

	# Absorb currency residual into largest-qty document (stable: max qty, then name).
	largest = max(entries, key=lambda x: (x[1], x[0]))[0]
	oh_diff = _money(prod_oh - oh_sum, precision)
	lab_diff = _money(prod_lab - lab_sum, precision)

	rows: list[DocumentBreakdownRow] = []
	for se, qty, oh in oh_parts:
		lab = next(v for s, v in lab_parts if s == se)
		if se == largest:
			oh = _money(oh + oh_diff, precision)
			lab = _money(lab + lab_diff, precision)
		total = _money(oh + lab, precision)
		rows.append(
			DocumentBreakdownRow(
				source_document_type="Stock Entry",
				source_document=se,
				quantity=qty,
				overhead_amount=oh,
				labor_amount=lab,
				total_allocated_amount=total,
			)
		)

	return DocumentBreakdownResult(
		rows=rows,
		total_quantity=total_qty,
		total_overhead=_money(sum((r.overhead_amount for r in rows), Decimal("0")), precision),
		total_labor=_money(sum((r.labor_amount for r in rows), Decimal("0")), precision),
		total_allocated_amount=_money(
			sum((r.total_allocated_amount for r in rows), Decimal("0")), precision
		),
	)

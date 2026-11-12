# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Reconciliation identities for three-level allocation."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from erpnext_extensions.iran_accounting.domain.currency import get_currency_precision
from erpnext_extensions.iran_accounting.fg_cost_allocation.allocate import (
	BatchAllocationState,
	BatchExpenseAlloc,
	ProductExpenseAlloc,
	_money,
)
from erpnext_extensions.iran_accounting.fg_cost_allocation.gl_extract import ExpensePoolSnapshot


@dataclass
class ReconciliationResult:
	ok: bool
	checks: list[dict] = field(default_factory=list)
	messages: list[str] = field(default_factory=list)

	def add(self, name: str, ok: bool, expected=None, actual=None, detail: str = ""):
		self.checks.append(
			{
				"name": name,
				"ok": ok,
				"expected": str(expected) if expected is not None else None,
				"actual": str(actual) if actual is not None else None,
				"detail": detail,
			}
		)
		if not ok:
			self.ok = False
			self.messages.append(f"{name}: expected={expected} actual={actual} {detail}".strip())


def reconcile(
	*,
	pools: list[ExpensePoolSnapshot],
	batches: list[BatchAllocationState],
	batch_expenses: list[BatchExpenseAlloc],
	product_rows: list,
	product_expenses: list[ProductExpenseAlloc],
	month_norm: Decimal,
	share_variance: Decimal,
	share_precision: int,
	currency: str,
	blocking_exceptions: list[str],
	uom_complete: bool = True,
) -> ReconciliationResult:
	result = ReconciliationResult(ok=True)
	precision = get_currency_precision(currency)
	eps_share = Decimal("1").scaleb(-max(share_precision, 6))

	# 1) GL lines → pool
	for pool in pools:
		line_sum = sum((l.net_amount for l in pool.lines), Decimal("0"))
		ok = _money(line_sum, precision) == _money(pool.monthly_net_expense, precision)
		result.add("gl_lines_to_pool", ok, pool.monthly_net_expense, line_sum, pool.account)

	# 2) sum batch norms = month_norm
	batch_norm_sum = sum((b.normalized_qty for b in batches), Decimal("0"))
	result.add("batch_norm_sum", batch_norm_sum == month_norm, month_norm, batch_norm_sum)

	# 3) calculated shares ≈ 1 only when UOM-complete and month_norm > 0
	if month_norm > 0 and uom_complete:
		calc_sum = sum((b.calculated_share for b in batches), Decimal("0"))
		result.add(
			"calculated_share_sum",
			abs(calc_sum - Decimal("1")) <= eps_share * Decimal(max(len(batches), 1)),
			Decimal("1"),
			calc_sum,
		)
	elif not uom_complete:
		result.add(
			"calculated_share_sum",
			False,
			Decimal("1"),
			None,
			"provisional — UOM incomplete; not a valid 100% allocation",
		)

	# 4) applied share variance (informational / blocking via Incomplete)
	result.add(
		"applied_share_sum",
		uom_complete
		and (abs(share_variance) <= eps_share * Decimal(max(len(batches), 1)) or month_norm == 0),
		Decimal("0"),
		share_variance,
		"variance from 100%",
	)

	# 5) per pool: sum batch allocations = pool net (only when share ok and production)
	share_ok = uom_complete and (
		abs(share_variance) <= eps_share * Decimal(max(len(batches), 1)) or month_norm == 0
	)
	if share_ok and month_norm > 0:
		for pool in pools:
			key = (pool.account, pool.cost_center or "", pool.department or "")
			alloc = sum(
				(bx.allocated_amount for bx in batch_expenses if bx.pool_key == key),
				Decimal("0"),
			)
			# Multiple rules may share same account/cc/dept - match settings_rule too
			alloc = sum(
				(
					bx.allocated_amount
					for bx in batch_expenses
					if bx.account == pool.account
					and (bx.cost_center or "") == (pool.cost_center or "")
					and (bx.department or "") == (pool.department or "")
					and bx.settings_rule == pool.settings_rule
				),
				Decimal("0"),
			)
			result.add(
				"pool_to_batches",
				_money(alloc, precision) == _money(pool.monthly_net_expense, precision),
				pool.monthly_net_expense,
				alloc,
				pool.settings_rule,
			)

	# 6) per batch: sum products = batch total
	prod_by_batch: dict[str, Decimal] = {}
	for batch, _p, _share, amt in product_rows:
		prod_by_batch[batch.batch_no] = prod_by_batch.get(batch.batch_no, Decimal("0")) + amt
	for batch in batches:
		actual = prod_by_batch.get(batch.batch_no, Decimal("0"))
		result.add(
			"batch_to_products",
			_money(actual, precision) == _money(batch.total_allocated_expense, precision),
			batch.total_allocated_expense,
			actual,
			batch.batch_no,
		)

	# 7–8) Product × Expense Pool is historical/display-only.
	# When rows exist (legacy revisions), still reconcile; new revisions may omit them.
	if product_expenses:
		px_by_product: dict[tuple[str, str], Decimal] = {}
		for px in product_expenses:
			k = (px.batch_no, px.item_code)
			px_by_product[k] = px_by_product.get(k, Decimal("0")) + px.product_pool_allocated_amount
		for batch, product, _share, amt in product_rows:
			k = (batch.batch_no, product.item_code)
			actual = px_by_product.get(k, Decimal("0"))
			result.add(
				"product_to_product_pool",
				_money(actual, precision) == _money(amt, precision),
				amt,
				actual,
				f"{k}",
			)

		bx_map = {(bx.batch_no, bx.pool_key, bx.settings_rule): bx.allocated_amount for bx in batch_expenses}
		px_group: dict[tuple, Decimal] = {}
		for px in product_expenses:
			k = (px.batch_no, px.pool_key, px.settings_rule)
			px_group[k] = px_group.get(k, Decimal("0")) + px.product_pool_allocated_amount
		for k, expected in bx_map.items():
			actual = px_group.get(k, Decimal("0"))
			result.add(
				"batch_pool_to_product_pool",
				_money(actual, precision) == _money(expected, precision),
				expected,
				actual,
				str(k),
			)

	# 9) Each Batch×Pool amount = pool_net × applied_share (within money precision).
	# Rows marked residual_applied intentionally absorb pool-level rounding and are
	# validated via pool-sum identity elsewhere; skip strict per-row formula there.
	for bx in batch_expenses:
		expected = _money(bx.pool_monthly_net_expense * bx.applied_batch_share, precision)
		if bx.residual_applied:
			ok = True
		else:
			ok = _money(bx.allocated_amount, precision) == expected
		result.add(
			"batch_pool_formula",
			ok,
			expected,
			bx.allocated_amount,
			f"{bx.batch_no}/{bx.account}",
		)

	if blocking_exceptions:
		result.ok = False
		result.messages.extend(blocking_exceptions[:50])
		result.add("exceptions", False, 0, len(blocking_exceptions), "blocking exceptions present")

	return result

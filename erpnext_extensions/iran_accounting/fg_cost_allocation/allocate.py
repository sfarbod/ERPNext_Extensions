# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Month-wide batch shares and three-level allocation with residual policy."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP

from erpnext_extensions.iran_accounting.domain.currency import get_currency_precision
from erpnext_extensions.iran_accounting.fg_cost_allocation.gl_extract import ExpensePoolSnapshot
from erpnext_extensions.iran_accounting.fg_cost_allocation.production_extract import ProductBucket


def _money(value: Decimal, precision: int) -> Decimal:
	q = Decimal("1") if precision <= 0 else Decimal("1").scaleb(-precision)
	return Decimal(value).quantize(q, rounding=ROUND_HALF_UP)


def _share(value: Decimal, precision: int) -> Decimal:
	# Keep more digits than money; share_precision typically 8
	q = Decimal("1").scaleb(-max(precision, 8))
	return Decimal(value).quantize(q, rounding=ROUND_HALF_UP)


@dataclass
class BatchAllocationState:
	batch_no: str
	finished_item: str
	actual_qty: Decimal
	uom: str
	normalization_factor: Decimal | None
	normalized_qty: Decimal
	total_monthly_normalized_qty: Decimal
	calculated_share: Decimal
	manual_share: Decimal | None
	is_manual_override: bool
	applied_share: Decimal
	work_orders: list[str] = field(default_factory=list)
	stock_entries: list[str] = field(default_factory=list)
	stock_entry_details: list[str] = field(default_factory=list)
	posting_date_min: str | None = None
	posting_date_max: str | None = None
	total_allocated_expense: Decimal = Decimal("0")
	products: list[ProductBucket] = field(default_factory=list)


@dataclass
class BatchExpenseAlloc:
	batch_no: str
	pool_key: tuple[str, str, str]  # account, cc, dept
	settings_rule: str
	account: str
	cost_center: str
	department: str
	pool_monthly_net_expense: Decimal
	applied_batch_share: Decimal
	allocated_amount: Decimal
	residual_applied: bool = False


@dataclass
class ProductExpenseAlloc:
	batch_no: str
	item_code: str
	pool_key: tuple[str, str, str]
	settings_rule: str
	account: str
	cost_center: str
	department: str
	pool_monthly_net_expense: Decimal
	applied_batch_share: Decimal
	product_share: Decimal
	batch_pool_allocated_amount: Decimal
	product_pool_allocated_amount: Decimal
	residual_applied: bool = False


def build_batches(
	products: dict[tuple[str, str], ProductBucket],
	*,
	share_precision: int,
	overrides: dict[str, Decimal] | None = None,
	finalize_share_sum: bool = True,
) -> tuple[list[BatchAllocationState], Decimal, Decimal]:
	"""Roll up products to batches; compute month-wide calculated/applied shares.

	Returns (batches, month_norm, share_variance) where share_variance = sum(applied)-1.
	Does NOT silently renormalize other batches.

	When finalize_share_sum is False (UOM incomplete), residual-to-100% is skipped —
	shares are provisional diagnostics only.
	"""
	overrides = overrides or {}
	by_batch: dict[str, list[ProductBucket]] = {}
	for (_batch, _item), prod in products.items():
		if prod.missing_uom_factor:
			# Still attach for visibility but exclude from share base via norm=0
			pass
		by_batch.setdefault(prod.batch_no, []).append(prod)

	batches: list[BatchAllocationState] = []
	for batch_no, prods in sorted(by_batch.items()):
		norm = sum((p.normalized_qty for p in prods), Decimal("0"))
		actual = sum((p.actual_qty for p in prods), Decimal("0"))
		# representative item: largest normalized then actual
		rep = sorted(prods, key=lambda p: (p.normalized_qty, p.actual_qty), reverse=True)[0]
		dates = [d for p in prods for d in p.posting_dates if d]
		wo, se, sed = set(), set(), set()
		for p in prods:
			wo |= p.work_orders
			se |= p.stock_entries
			sed |= p.stock_entry_details
		factor = rep.normalization_factor
		batches.append(
			BatchAllocationState(
				batch_no=batch_no,
				finished_item=rep.item_code,
				actual_qty=actual,
				uom=rep.uom,
				normalization_factor=factor,
				normalized_qty=norm,
				total_monthly_normalized_qty=Decimal("0"),
				calculated_share=Decimal("0"),
				manual_share=overrides.get(batch_no),
				is_manual_override=batch_no in overrides,
				applied_share=Decimal("0"),
				work_orders=sorted(x for x in wo if x),
				stock_entries=sorted(x for x in se if x),
				stock_entry_details=sorted(x for x in sed if x),
				posting_date_min=str(min(dates)) if dates else None,
				posting_date_max=str(max(dates)) if dates else None,
				products=prods,
			)
		)

	month_norm = sum((b.normalized_qty for b in batches), Decimal("0"))
	for b in batches:
		b.total_monthly_normalized_qty = month_norm
		if month_norm > 0 and b.normalized_qty > 0:
			b.calculated_share = _share(b.normalized_qty / month_norm, share_precision)
		else:
			b.calculated_share = Decimal("0")

	# Absorb quantization residual into largest batch so Σ calculated_share == 1
	# Only when all eligible production is normalized (finalize_share_sum).
	if finalize_share_sum and month_norm > 0:
		eligible = [b for b in batches if b.normalized_qty > 0]
		calc_sum = sum((b.calculated_share for b in eligible), Decimal("0"))
		diff = _share(Decimal("1") - calc_sum, share_precision)
		if eligible and diff != 0:
			target = max(eligible, key=lambda x: (x.normalized_qty, x.batch_no))
			target.calculated_share = _share(target.calculated_share + diff, share_precision)

	for b in batches:
		if b.is_manual_override and b.manual_share is not None:
			b.applied_share = _share(Decimal(b.manual_share), share_precision)
		else:
			b.applied_share = b.calculated_share
			b.manual_share = None

	applied_sum = sum((b.applied_share for b in batches), Decimal("0"))
	variance = applied_sum - (Decimal("1") if month_norm > 0 else Decimal("0"))
	return batches, month_norm, variance


def allocate_pools_to_batches(
	pools: list[ExpensePoolSnapshot],
	batches: list[BatchAllocationState],
	*,
	currency: str,
) -> list[BatchExpenseAlloc]:
	precision = get_currency_precision(currency)
	results: list[BatchExpenseAlloc] = []
	eligible = [b for b in batches if b.normalized_qty > 0]
	for pool in pools:
		# Zero-net pools remain as Expense Pool headers; skip dense zero cross-alloc rows.
		if pool.monthly_net_expense == 0:
			continue
		pool_key = (pool.account, pool.cost_center or "", pool.department or "")
		raw_amounts: list[tuple[BatchAllocationState, Decimal]] = []
		allocated_sum = Decimal("0")
		for b in eligible:
			raw = _money(pool.monthly_net_expense * b.applied_share, precision)
			raw_amounts.append((b, raw))
			allocated_sum += raw
		# Residual to largest normalized batch
		diff = _money(pool.monthly_net_expense - allocated_sum, precision)
		residual_batch = None
		if eligible and diff != 0:
			residual_batch = max(eligible, key=lambda x: (x.normalized_qty, x.batch_no))
		for b, amount in raw_amounts:
			residual = False
			final_amt = amount
			if residual_batch and b.batch_no == residual_batch.batch_no and diff != 0:
				final_amt = _money(amount + diff, precision)
				residual = True
			results.append(
				BatchExpenseAlloc(
					batch_no=b.batch_no,
					pool_key=pool_key,
					settings_rule=pool.settings_rule,
					account=pool.account,
					cost_center=pool.cost_center or "",
					department=pool.department or "",
					pool_monthly_net_expense=pool.monthly_net_expense,
					applied_batch_share=b.applied_share,
					allocated_amount=final_amt,
					residual_applied=residual,
				)
			)
		# Update batch totals
		for b, _amount in raw_amounts:
			contrib = next(
				r.allocated_amount for r in results if r.batch_no == b.batch_no and r.pool_key == pool_key
			)
			b.total_allocated_expense = _money(b.total_allocated_expense + contrib, precision)
	return results


def allocate_products(
	batches: list[BatchAllocationState],
	batch_expenses: list[BatchExpenseAlloc],
	*,
	currency: str,
	share_precision: int,
) -> tuple[list[tuple[BatchAllocationState, ProductBucket, Decimal, Decimal]], list[ProductExpenseAlloc]]:
	"""Return (product rows with product_share & product_allocated_expense, product×pool)."""
	precision = get_currency_precision(currency)
	product_rows: list[tuple[BatchAllocationState, ProductBucket, Decimal, Decimal]] = []
	product_expenses: list[ProductExpenseAlloc] = []

	bx_by_batch: dict[str, list[BatchExpenseAlloc]] = {}
	for bx in batch_expenses:
		bx_by_batch.setdefault(bx.batch_no, []).append(bx)

	for batch in batches:
		eligible_products = [p for p in batch.products if p.normalized_qty > 0 and not p.missing_uom_factor]
		# Persist missing-UOM / zero-norm products with zero share for audit
		for p in batch.products:
			if p.missing_uom_factor or p.normalized_qty <= 0:
				product_rows.append((batch, p, Decimal("0"), Decimal("0")))
		batch_norm = batch.normalized_qty
		# First compute product shares and total product allocation from batch total
		share_map: dict[str, Decimal] = {}
		for p in eligible_products:
			share_map[p.item_code] = (
				_share(p.normalized_qty / batch_norm, share_precision) if batch_norm > 0 else Decimal("0")
			)

		# Distribute batch total to products with residual
		raw_prod_amts: list[tuple[ProductBucket, Decimal]] = []
		sum_amt = Decimal("0")
		for p in eligible_products:
			amt = _money(batch.total_allocated_expense * share_map[p.item_code], precision)
			raw_prod_amts.append((p, amt))
			sum_amt += amt
		diff = _money(batch.total_allocated_expense - sum_amt, precision)
		largest = (
			max(eligible_products, key=lambda x: (x.normalized_qty, x.item_code))
			if eligible_products
			else None
		)
		prod_total_map: dict[str, Decimal] = {}
		for p, amt in raw_prod_amts:
			final = amt
			if largest and p.item_code == largest.item_code and diff != 0:
				final = _money(amt + diff, precision)
			prod_total_map[p.item_code] = final
			product_rows.append((batch, p, share_map[p.item_code], final))

		# Product × Pool
		for bx in bx_by_batch.get(batch.batch_no, []):
			raw_px: list[tuple[ProductBucket, Decimal]] = []
			s = Decimal("0")
			for p in eligible_products:
				amt = _money(bx.allocated_amount * share_map[p.item_code], precision)
				raw_px.append((p, amt))
				s += amt
			px_diff = _money(bx.allocated_amount - s, precision)
			for p, amt in raw_px:
				residual = False
				final = amt
				if largest and p.item_code == largest.item_code and px_diff != 0:
					final = _money(amt + px_diff, precision)
					residual = True
				product_expenses.append(
					ProductExpenseAlloc(
						batch_no=batch.batch_no,
						item_code=p.item_code,
						pool_key=bx.pool_key,
						settings_rule=bx.settings_rule,
						account=bx.account,
						cost_center=bx.cost_center,
						department=bx.department,
						pool_monthly_net_expense=bx.pool_monthly_net_expense,
						applied_batch_share=bx.applied_batch_share,
						product_share=share_map[p.item_code],
						batch_pool_allocated_amount=bx.allocated_amount,
						product_pool_allocated_amount=final,
						residual_applied=residual,
					)
				)

	return product_rows, product_expenses

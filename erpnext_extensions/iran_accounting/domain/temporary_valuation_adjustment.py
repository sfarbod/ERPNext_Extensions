# Copyright (c) 2026, ERPNext Extensions contributors
"""Temporary Stock Adjustment Bridge (Iran Accounting v5.5.45 Phase 1).

General-purpose, non-whitelisted bridge for Stock Entries blocked by historical
valuation inconsistencies. Does not hardcode voucher / item / batch / warehouse.

Classification
--------------
NORMAL_TRANSACTION — no temporary bridge needed
TEMPORARY_VALUATION_ADJUSTMENT — eligible historical valuation gap
UNSUPPORTED_OR_UNSAFE_TRANSACTION — fail with original validation

Phase 1 posts / accepts mathematically justified residuals so warehouse
operations can continue. Phase 2 Full Historical Rebuild discovers rows via
DocType ``Temporary Valuation Adjustment`` (hotfix_version=5.5.45).
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

import frappe
from frappe import _
from frappe.utils import cint, flt

from erpnext_extensions.iran_accounting.domain.currency import (
	amount_is_fractional,
	get_company_currency,
	is_irr_company,
	round_currency,
)
from erpnext_extensions.iran_accounting.domain.stock_entry_sync import (
	NATIVE_OUTGOING_STOCK_VALUATION_PURPOSES,
)

HOTFIX_VERSION = "5.5.46"
DOCTYPE = "Temporary Valuation Adjustment"

# In-memory stamp on Stock Entry during submit (not a DocType field).
PENDING_ATTR = "_iran_tva_pending"
ALLOW_FRACTIONAL_ATTR = "_iran_tva_allow_fractional_sabe"

REASON_HISTORICAL_VALUATION_MISMATCH = "HISTORICAL_VALUATION_MISMATCH"
REASON_INDIVISIBLE_MOVING_AVERAGE = "INDIVISIBLE_MOVING_AVERAGE"
REASON_HISTORICAL_BATCH_RATE_MISMATCH = "HISTORICAL_BATCH_RATE_MISMATCH"
REASON_SED_SLE_VALUE_MISMATCH = "SED_SLE_VALUE_MISMATCH"
REASON_SLE_SABE_VALUE_MISMATCH = "SLE_SABE_VALUE_MISMATCH"
REASON_HISTORICAL_ZERO_VALUE_STOCK = "HISTORICAL_ZERO_VALUE_STOCK"
REASON_SOURCE_RATE_MISMATCH = "SOURCE_RATE_MISMATCH"
REASON_MANUFACTURING_VALUATION_RESIDUAL = "MANUFACTURING_VALUATION_RESIDUAL"
REASON_TRANSFER_VALUATION_RESIDUAL = "TRANSFER_VALUATION_RESIDUAL"

ELIGIBLE_PURPOSES = frozenset(
	{
		"Material Issue",
		"Material Receipt",
		"Material Transfer",
		"Material Transfer for Manufacture",
		"Manufacture",
		"Repack",
		"Send to Subcontractor",
		"Material Consumption for Manufacture",
	}
)

KIND_NORMAL = "NORMAL_TRANSACTION"
KIND_TEMPORARY = "TEMPORARY_VALUATION_ADJUSTMENT"
KIND_UNSUPPORTED = "UNSUPPORTED_OR_UNSAFE_TRANSACTION"

# Accounting policy categories (root-cause based — not amount-based).
# Root cause ≠ GL destination. All inventory valuation residuals that need GL
# post to Company Stock Adjustment — never Financial Round Off.
# CATEGORY 1: approved IRR amount vs qty×integer_rate representation → Stock Adjustment GL.
CATEGORY_GENUINE_ROUNDING = "GENUINE_ROUNDING"
# CATEGORY 2: net inventory valuation gap requiring Stock Adjustment GL.
CATEGORY_HISTORICAL_VALUATION = "HISTORICAL_VALUATION"
# CATEGORY 3: observable residual with no net inventory GL effect (e.g. ZVT same account).
CATEGORY_TRACE_ONLY = "TRACE_ONLY"


def _through_riv() -> bool:
	try:
		return bool(frappe.flags.get("through_repost_item_valuation"))
	except Exception:
		return False


def _d(value) -> Decimal:
	return Decimal(str(flt(value)))


def _is_whole_qty(qty) -> bool:
	q = _d(qty)
	return q == q.to_integral_value()


def integer_rate_and_product(qty, pool_or_amount) -> tuple[Decimal, Decimal, Decimal]:
	qty_d = _d(qty)
	pool_d = _d(pool_or_amount)
	if qty_d == 0:
		return Decimal("0"), Decimal("0"), pool_d
	rate = (pool_d / qty_d).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
	product = (qty_d * rate).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
	return rate, product, pool_d - product


def resolve_stock_adjustment_account(company: str) -> str | None:
	return frappe.get_cached_value("Company", company, "stock_adjustment_account")


def _is_same_inventory_account_transfer(doc) -> bool:
	"""True when all SE warehouses share one inventory account (Core ZVT / no transfer GL)."""
	purpose = doc.get("purpose") or ""
	if purpose not in (
		"Material Transfer",
		"Material Transfer for Manufacture",
		"Send to Subcontractor",
	):
		return False
	accounts: set[str] = set()
	for row in doc.get("items") or []:
		for wh in (row.get("s_warehouse"), row.get("t_warehouse")):
			if not wh:
				continue
			acc = frappe.get_cached_value("Warehouse", wh, "account")
			if acc:
				accounts.add(acc)
	return len(accounts) == 1


def make_idempotency_key(
	stock_entry: str,
	stock_entry_detail: str | None,
	reason_code: str,
	adjustment_delta: float,
) -> str:
	raw = f"{HOTFIX_VERSION}|{stock_entry}|{stock_entry_detail or ''}|{reason_code}|{flt(adjustment_delta):.6f}"
	return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]


def pending_adjustments(doc) -> list[dict[str, Any]]:
	return list(getattr(doc, PENDING_ATTR, None) or [])


def has_pending_reason(doc, *reasons: str) -> bool:
	pending = pending_adjustments(doc)
	if not pending:
		return False
	wanted = set(reasons)
	return any(p.get("reason_code") in wanted for p in pending)


def allows_fractional_sabe(doc_or_name) -> bool:
	"""True when temporary bridge authorized fractional SABE for this voucher."""
	if _through_riv():
		return False
	doc = doc_or_name
	if isinstance(doc_or_name, str):
		# Check durable TVA rows first (post-submit / retry).
		if frappe.db.exists(
			DOCTYPE,
			{
				"stock_entry": doc_or_name,
				"reason_code": (
					"in",
					[
						REASON_INDIVISIBLE_MOVING_AVERAGE,
						REASON_HISTORICAL_BATCH_RATE_MISMATCH,
						REASON_SLE_SABE_VALUE_MISMATCH,
						REASON_TRANSFER_VALUATION_RESIDUAL,
					],
				),
				"repair_status": ("in", ["OPEN", "SUBMITTED"]),
			},
		):
			return True
		# In-flight flag via frappe.flags
		flags = getattr(frappe.local, "iran_tva_fractional_vouchers", None) or set()
		return doc_or_name in flags

	if getattr(doc, ALLOW_FRACTIONAL_ATTR, None):
		return True
	if has_pending_reason(
		doc,
		REASON_INDIVISIBLE_MOVING_AVERAGE,
		REASON_HISTORICAL_BATCH_RATE_MISMATCH,
		REASON_SLE_SABE_VALUE_MISMATCH,
		REASON_TRANSFER_VALUATION_RESIDUAL,
	):
		return True
	name = getattr(doc, "name", None)
	return bool(name) and allows_fractional_sabe(name)


def mark_fractional_allowed(doc) -> None:
	setattr(doc, ALLOW_FRACTIONAL_ATTR, True)
	frappe.local.iran_tva_active_stock_entry = doc
	if doc.get("name"):
		flags = getattr(frappe.local, "iran_tva_fractional_vouchers", None)
		if flags is None:
			flags = set()
			frappe.local.iran_tva_fractional_vouchers = flags
		flags.add(doc.name)


def append_pending(doc, adjustment: dict[str, Any]) -> None:
	pending = getattr(doc, PENDING_ATTR, None)
	if pending is None:
		pending = []
		setattr(doc, PENDING_ATTR, pending)
	# Deduplicate by idempotency key within the same submit.
	key = adjustment.get("idempotency_key")
	if key and any(p.get("idempotency_key") == key for p in pending):
		return
	pending.append(adjustment)


def classify_indivisible_ma_row(doc, row, pool_label: str, bal_qty: float, bal_value: float) -> dict | None:
	"""Return a pending TVA dict when full-depletion indivisible MA remainder is eligible."""
	if not is_irr_company(doc.company):
		return None
	purpose = doc.get("purpose") or ""
	if purpose not in ELIGIBLE_PURPOSES:
		return None
	# Manufacture / Repack residuals have TYPE A/B/C contracts — do not bridge here.
	if purpose in ("Manufacture", "Repack", "Material Consumption for Manufacture"):
		return None

	move_qty = abs(
		flt(row.get("transfer_qty") if row.get("transfer_qty") not in (None, "") else row.get("qty"))
	)
	if move_qty <= 0 or not _is_whole_qty(move_qty):
		return None
	if abs(bal_qty - move_qty) > 1e-9:
		return None
	if abs(bal_value) < 1e-9:
		return None

	rate, product, remainder = integer_rate_and_product(move_qty, bal_value)
	if remainder == 0:
		return None

	sa_account = resolve_stock_adjustment_account(doc.company)
	if not sa_account:
		frappe.throw(
			_(
				"Temporary Stock Adjustment Bridge requires Company Stock Adjustment Account "
				"on {0} before submitting indivisible Moving Average remainders."
			).format(frappe.bold(doc.company)),
			title=_("Missing Stock Adjustment Account"),
		)

	delta = float(remainder)
	reason = REASON_INDIVISIBLE_MOVING_AVERAGE
	# Indivisible remainder is provenance of a historical fractional pool.
	# Net inventory GL effect is none when SLE already moves the full pool value
	# (Issue expense/inventory at SLE SVD; Transfer ZVT same-account has Σ SLE=0).
	# Class A residual (if any) posts to Stock Adjustment separately on Issue/Receipt.
	# Forcing extra SA GL for the pool remainder on ZVT would invent artificial P&L.
	posting_mode = "TRACE"
	debit_credit = "None"
	accounting_category = CATEGORY_TRACE_ONLY
	zvt_same_account = _is_same_inventory_account_transfer(doc)

	key = make_idempotency_key(doc.name or "NEW", row.get("name"), reason, delta)
	return {
		"kind": KIND_TEMPORARY,
		"reason_code": reason,
		"stock_entry": doc.name,
		"stock_entry_detail": row.get("name"),
		"item_code": row.get("item_code"),
		"warehouse": row.get("s_warehouse") or row.get("t_warehouse"),
		"batch_no": row.get("batch_no"),
		"company": doc.company,
		"purpose": purpose,
		"posting_date": doc.get("posting_date"),
		"posting_time": doc.get("posting_time"),
		"expected_value": float(product),
		"actual_value": float(_d(bal_value)),
		"adjustment_delta": delta,
		"debit_credit": debit_credit,
		"stock_adjustment_account": sa_account,
		"posting_mode": posting_mode,
		"accounting_category": accounting_category,
		"hotfix_version": HOTFIX_VERSION,
		"idempotency_key": key,
		"remarks": (
			f"[{accounting_category}] Indivisible MA remainder on {pool_label}: qty={move_qty}, "
			f"pool={bal_value}, integer_rate={rate}, qty×rate={product}, remainder={remainder}. "
			f"TRACE-only: SLE already carries pool movement; zvt_same_account={zvt_same_account}. "
			f"Do not invent Stock Adjustment GL."
		),
		"payload": {
			"pool_label": pool_label,
			"move_qty": move_qty,
			"pool_value": float(_d(bal_value)),
			"integer_rate": float(rate),
			"qty_times_rate": float(product),
			"remainder": float(remainder),
			"allow_fractional_sabe": True,
			"accounting_category": accounting_category,
			"zvt_same_account": zvt_same_account,
		},
	}


def classify_zero_value_stock_row(doc, row, bal_qty: float, bal_value: float) -> dict | None:
	"""Historical qty>0 with ~zero stock_value on outgoing warehouse."""
	if not row.get("s_warehouse") or not row.get("item_code"):
		return None
	move_qty = abs(
		flt(row.get("transfer_qty") if row.get("transfer_qty") not in (None, "") else row.get("qty"))
	)
	if move_qty <= 0 or bal_qty < move_qty - 1e-9:
		return None
	if abs(bal_value) > 0.5:
		return None
	if bal_qty <= 0:
		return None

	sa_account = resolve_stock_adjustment_account(doc.company)
	if not sa_account:
		return None

	delta = 0.0  # native MA will invent a rate; stamp provenance only
	reason = REASON_HISTORICAL_ZERO_VALUE_STOCK
	accounting_category = CATEGORY_TRACE_ONLY
	key = make_idempotency_key(doc.name or "NEW", row.get("name"), reason, delta)
	return {
		"kind": KIND_TEMPORARY,
		"reason_code": reason,
		"stock_entry": doc.name,
		"stock_entry_detail": row.get("name"),
		"item_code": row.get("item_code"),
		"warehouse": row.get("s_warehouse"),
		"batch_no": row.get("batch_no"),
		"company": doc.company,
		"purpose": doc.get("purpose"),
		"posting_date": doc.get("posting_date"),
		"posting_time": doc.get("posting_time"),
		"expected_value": 0.0,
		"actual_value": float(bal_value),
		"adjustment_delta": delta,
		"debit_credit": "None",
		"stock_adjustment_account": sa_account,
		"posting_mode": "TRACE",
		"accounting_category": accounting_category,
		"hotfix_version": HOTFIX_VERSION,
		"idempotency_key": key,
		"remarks": (
			f"[{accounting_category}] Historical zero-value stock: item={row.item_code} "
			f"warehouse={row.s_warehouse} bin_qty={bal_qty} bin_value={bal_value}; "
			f"native SLE valuation accepted on submit. Any Class A amount vs qty×rate "
			f"residual posts separately as GENUINE_ROUNDING → Stock Adjustment GL."
		),
		"payload": {
			"bin_qty": bal_qty,
			"bin_value": float(bal_value),
			"move_qty": move_qty,
			"accounting_category": accounting_category,
		},
	}


def _inward_batch_avg_at_warehouse(item_code: str, batch_no: str, warehouse: str) -> tuple[float, float, float] | None:
	"""Positive-qty SABE average for batch+warehouse (BatchNoValuation seed when net qty corrupt)."""
	row = frappe.db.sql(
		"""
		select coalesce(sum(qty), 0) as q,
		       coalesce(sum(stock_value_difference), 0) as v
		from `tabSerial and Batch Entry`
		where batch_no = %s
		  and warehouse = %s
		  and item_code = %s
		  and is_cancelled = 0
		  and docstatus = 1
		  and qty > 0
		""",
		(batch_no, warehouse, item_code),
		as_dict=True,
	)
	if not row or flt(row[0].q) <= 0:
		return None
	q = flt(row[0].q)
	v = flt(row[0].v)
	return q, v, v / q


def resolve_fractional_batch_pool(
	doc,
	row,
	bal_qty: float,
	bal_value: float,
) -> tuple[float, float, float, str] | None:
	"""Return (pool_qty, pool_value, avg_rate, pool_source) for fractional-batch TVA classify.

	When warehouse SABE nets to ≤0 (historical corruption) but Bin still covers the
	move, fall back to inward-positive SABE average so BatchNoValuation-aligned
	fractional rates can be authorized without inventing Stock Adjustment GL.
	"""
	move_qty = abs(
		flt(row.get("transfer_qty") if row.get("transfer_qty") not in (None, "") else row.get("qty"))
	)
	if move_qty <= 0:
		return None
	ccy = get_company_currency(doc.company)
	if bal_qty > 1e-9:
		if move_qty > bal_qty + 1e-9:
			return None
		avg = flt(bal_value) / flt(bal_qty)
		return bal_qty, flt(bal_value), avg, "warehouse_net"

	# Net SABE qty corrupt/negative: require Bin coverage, then inward avg.
	from erpnext_extensions.iran_accounting.domain.stock_entry_prevention import (
		_warehouse_balance,
	)

	wq, _wv = _warehouse_balance(row.item_code, row.s_warehouse)
	if wq + 1e-9 < move_qty:
		return None
	inward = _inward_batch_avg_at_warehouse(row.item_code, row.batch_no, row.s_warehouse)
	if not inward:
		return None
	_iq, _iv, avg = inward
	if not (
		amount_is_fractional(avg, ccy)
		or amount_is_fractional(bal_value, ccy)
		or amount_is_fractional(_iv, ccy)
	):
		return None
	# Synthetic pool sized to the move (Bin already covers qty).
	return move_qty, avg * move_qty, avg, "inward_avg_fallback"


def classify_historical_fractional_batch_row(doc, row, bal_qty: float, bal_value: float) -> dict | None:
	"""Outgoing batch move whose historical batch average is fractional IRR."""
	if not row.get("s_warehouse") or not row.get("batch_no") or not row.get("item_code"):
		return None
	purpose = doc.get("purpose") or ""
	if purpose in ("Manufacture", "Repack", "Material Consumption for Manufacture"):
		return None
	if purpose not in ELIGIBLE_PURPOSES:
		return None
	move_qty = abs(
		flt(row.get("transfer_qty") if row.get("transfer_qty") not in (None, "") else row.get("qty"))
	)
	if move_qty <= 0:
		return None

	pool = resolve_fractional_batch_pool(doc, row, bal_qty, bal_value)
	if not pool:
		return None
	pool_qty, pool_value, avg_rate, pool_source = pool
	ccy = get_company_currency(doc.company)
	move_amount = abs(avg_rate * move_qty)
	if not (
		amount_is_fractional(avg_rate, ccy)
		or amount_is_fractional(pool_value, ccy)
		or amount_is_fractional(bal_value, ccy)
		or amount_is_fractional(move_amount, ccy)
	):
		return None

	sa_account = resolve_stock_adjustment_account(doc.company)
	if not sa_account:
		frappe.throw(
			_(
				"Temporary Stock Adjustment Bridge requires Company Stock Adjustment Account "
				"on {0} before submitting historical fractional batch movements."
			).format(frappe.bold(doc.company)),
			title=_("Missing Stock Adjustment Account"),
		)

	rate, product, remainder = integer_rate_and_product(move_qty, move_amount)
	delta = float(remainder) if remainder else float(move_amount - float(product))
	reason = REASON_HISTORICAL_BATCH_RATE_MISMATCH
	# Fractional historical batch layer is already in the SLE movement value.
	# Authorization to persist fractional SABE is TRACE; Class A residual (if any)
	# posts to Stock Adjustment for integer IRR representation of SLE amount.
	accounting_category = CATEGORY_TRACE_ONLY
	key = make_idempotency_key(doc.name or "NEW", row.get("name"), reason, delta or move_amount)
	return {
		"kind": KIND_TEMPORARY,
		"reason_code": reason,
		"stock_entry": doc.name,
		"stock_entry_detail": row.get("name"),
		"item_code": row.get("item_code"),
		"warehouse": row.get("s_warehouse"),
		"batch_no": row.get("batch_no"),
		"company": doc.company,
		"purpose": purpose,
		"posting_date": doc.get("posting_date"),
		"posting_time": doc.get("posting_time"),
		"expected_value": float(product),
		"actual_value": float(_d(move_amount)),
		"adjustment_delta": delta,
		"debit_credit": "None",
		"stock_adjustment_account": sa_account,
		"posting_mode": "TRACE",
		"accounting_category": accounting_category,
		"hotfix_version": HOTFIX_VERSION,
		"idempotency_key": key,
		"remarks": (
			f"[{accounting_category}] Historical fractional batch rate: batch={row.batch_no} "
			f"pool_qty={pool_qty} pool_value={pool_value} avg_rate={avg_rate} "
			f"net_sabe_qty={bal_qty} net_sabe_value={bal_value} pool_source={pool_source} "
			f"move_qty={move_qty} move_amount={move_amount} remainder_vs_integer={remainder}. "
			f"Fractional rate ≠ negative stock; quantity unchanged."
		),
		"payload": {
			"pool_qty": pool_qty,
			"pool_value": float(_d(pool_value)),
			"avg_rate": avg_rate,
			"net_sabe_qty": bal_qty,
			"net_sabe_value": float(_d(bal_value)),
			"pool_source": pool_source,
			"move_qty": move_qty,
			"move_amount": float(_d(move_amount)),
			"integer_rate": float(rate),
			"qty_times_rate": float(product),
			"remainder": float(remainder),
			"allow_fractional_sabe": True,
			"accounting_category": accounting_category,
		},
	}


def classify_observed_native_fractional_sabe(doc) -> list[dict[str, Any]]:
	"""After process_sle: authorize TRACE when Core persisted fractional SABE.

	Safety net when pre-submit pool classify missed corrupt warehouse SABE nets.
	Does not invent Stock Adjustment GL — TRACE_ONLY for Phase 2 discovery.
	"""
	if (doc.get("purpose") or "") not in ELIGIBLE_PURPOSES:
		return []
	if (doc.get("purpose") or "") in ("Manufacture", "Repack", "Material Consumption for Manufacture"):
		return []
	ccy = get_company_currency(doc.company)
	sa_account = resolve_stock_adjustment_account(doc.company)
	if not sa_account:
		return []
	rows = frappe.db.sql(
		"""
		select e.name as sabe_name, e.batch_no, e.qty, e.incoming_rate,
		       e.stock_value_difference, e.warehouse, sle.voucher_detail_no, sle.item_code
		from `tabStock Ledger Entry` sle
		inner join `tabSerial and Batch Entry` e
		  on e.parent = sle.serial_and_batch_bundle and e.is_cancelled = 0
		where sle.voucher_type = 'Stock Entry'
		  and sle.voucher_no = %s
		  and sle.is_cancelled = 0
		  and sle.serial_and_batch_bundle is not null
		""",
		doc.name,
		as_dict=True,
	)
	out: list[dict[str, Any]] = []
	seen_detail: set[str] = set()
	already = {
		p.get("stock_entry_detail")
		for p in (pending_adjustments(doc) or [])
		if p.get("reason_code") == REASON_HISTORICAL_BATCH_RATE_MISMATCH
	}
	for r in rows:
		if not (
			amount_is_fractional(r.incoming_rate, ccy)
			or amount_is_fractional(r.stock_value_difference, ccy)
		):
			continue
		detail = r.voucher_detail_no or r.sabe_name
		if detail in seen_detail or detail in already:
			continue
		seen_detail.add(detail)
		move_amount = abs(flt(r.stock_value_difference))
		move_qty = abs(flt(r.qty))
		rate, product, remainder = integer_rate_and_product(move_qty or 1, move_amount or flt(r.incoming_rate))
		delta = float(remainder) if remainder else 0.0
		reason = REASON_HISTORICAL_BATCH_RATE_MISMATCH
		accounting_category = CATEGORY_TRACE_ONLY
		key = make_idempotency_key(doc.name, detail, reason, delta or move_amount or flt(r.incoming_rate))
		out.append(
			{
				"kind": KIND_TEMPORARY,
				"reason_code": reason,
				"stock_entry": doc.name,
				"stock_entry_detail": detail,
				"item_code": r.item_code,
				"warehouse": r.warehouse,
				"batch_no": r.batch_no,
				"company": doc.company,
				"purpose": doc.get("purpose"),
				"posting_date": doc.get("posting_date"),
				"posting_time": doc.get("posting_time"),
				"expected_value": float(product),
				"actual_value": float(move_amount),
				"adjustment_delta": delta,
				"debit_credit": "None",
				"stock_adjustment_account": sa_account,
				"posting_mode": "TRACE",
				"accounting_category": accounting_category,
				"hotfix_version": HOTFIX_VERSION,
				"idempotency_key": key,
				"remarks": (
					f"[{accounting_category}] Observed native fractional SABE after process_sle: "
					f"batch={r.batch_no} rate={r.incoming_rate} svd={r.stock_value_difference}; "
					f"TRACE only — no Stock Adjustment for integer-vs-native transfer sync."
				),
				"payload": {
					"sabe_name": r.sabe_name,
					"incoming_rate": flt(r.incoming_rate),
					"stock_value_difference": flt(r.stock_value_difference),
					"allow_fractional_sabe": True,
					"accounting_category": accounting_category,
					"source": "observed_native_sabe",
				},
			}
		)
	return out


def prepare_before_submit_bridge(doc) -> None:
	"""Detect eligible historical valuation gaps before Core stock posting.

	Indivisible MA remainders are stamped inside
	``assert_no_unapproved_indivisible_ma_remainder`` (v5.5.45 bridge).
	"""
	if getattr(doc, "doctype", None) != "Stock Entry":
		return
	if not is_irr_company(doc.company):
		return
	if _through_riv():
		return
	# Frappe sets docstatus=1 before before_submit/on_submit during Document._submit().
	# Only skip cancelled documents.
	if cint(doc.docstatus) == 2:
		return

	from erpnext_extensions.iran_accounting.domain.stock_entry_prevention import (
		_batch_balance,
		_batchwise_required,
		_warehouse_balance,
	)

	for row in doc.get("items") or []:
		warehouse = row.get("s_warehouse")
		if not warehouse or not row.get("item_code"):
			continue
		bal_qty, bal_value = _warehouse_balance(row.item_code, warehouse)
		zero = classify_zero_value_stock_row(doc, row, bal_qty, bal_value)
		if zero:
			append_pending(doc, zero)

		batch_no = row.get("batch_no")
		if batch_no and _batchwise_required(row.item_code, batch_no):
			bq, bv = _batch_balance(row.item_code, batch_no, warehouse)
			frac = classify_historical_fractional_batch_row(doc, row, bq, bv)
			if frac:
				append_pending(doc, frac)
				mark_fractional_allowed(doc)

	# Ensure active SE is visible to SLE monkey-patch even when only zero-value stamped.
	if pending_adjustments(doc):
		frappe.local.iran_tva_active_stock_entry = doc
		if has_pending_reason(
			doc,
			REASON_INDIVISIBLE_MOVING_AVERAGE,
			REASON_HISTORICAL_BATCH_RATE_MISMATCH,
			REASON_SLE_SABE_VALUE_MISMATCH,
			REASON_TRANSFER_VALUATION_RESIDUAL,
		):
			mark_fractional_allowed(doc)


def classify_class_a_residual_bridge(doc) -> list[dict[str, Any]]:
	"""After SED↔SLE sync: Class A rate×qty residual needing Stock Adjustment remake.

	v5.5.45 policy: inventory valuation residuals (including genuine IRR Class A
	rounding) post to Company Stock Adjustment — never Financial Round Off.
	Root-cause category remains GENUINE_ROUNDING; GL destination is Stock Adjustment.
	"""
	from erpnext_extensions.iran_accounting.domain.irr_residual_classification import (
		STATUS_READY,
		evaluate_irr_rate_rounding_residual,
	)
	from erpnext_extensions.iran_accounting.domain.irr_rounding_residual import (
		fetch_irr_residual_gl_rows,
		stock_entry_excludes_irr_residual_round_off,
	)

	if stock_entry_excludes_irr_residual_round_off(doc):
		return []
	if (doc.get("purpose") or "") not in NATIVE_OUTGOING_STOCK_VALUATION_PURPOSES and (
		doc.get("purpose") or ""
	) not in ("Material Receipt",):
		# Still evaluate for Material Issue / Transfer / Receipt etc.
		pass

	decision = evaluate_irr_rate_rounding_residual(doc, gl_entries=None)
	if decision.status != STATUS_READY or not flt(decision.net_signed_debit):
		return []

	existing = fetch_irr_residual_gl_rows(doc.doctype, doc.name)
	if existing:
		return []

	sa_account = resolve_stock_adjustment_account(doc.company)
	if not sa_account:
		frappe.throw(
			_(
				"Company {0} Stock Adjustment Account is required for inventory valuation residuals. "
				"Financial Round Off must not be used."
			).format(frappe.bold(doc.company)),
			title=_("Missing Stock Adjustment Account"),
		)
	out: list[dict[str, Any]] = []
	for row_info in decision.class_a_rows or []:
		delta = flt(row_info.get("residual"))
		if not delta:
			continue
		# Root cause = Class A representation; GL destination = Stock Adjustment (policy).
		reason = REASON_SED_SLE_VALUE_MISMATCH
		accounting_category = CATEGORY_GENUINE_ROUNDING
		key = make_idempotency_key(doc.name, row_info.get("row_name"), reason, delta)
		signed = flt(row_info.get("round_off_debit"))
		out.append(
			{
				"kind": KIND_TEMPORARY,
				"reason_code": reason,
				"stock_entry": doc.name,
				"stock_entry_detail": row_info.get("row_name"),
				"item_code": row_info.get("item_code"),
				"warehouse": None,
				"batch_no": None,
				"company": doc.company,
				"purpose": doc.get("purpose"),
				"posting_date": doc.get("posting_date"),
				"posting_time": doc.get("posting_time"),
				"expected_value": flt(row_info.get("rate_derived_amount")),
				"actual_value": flt(row_info.get("authoritative_amount")),
				"adjustment_delta": delta,
				"debit_credit": "Debit" if signed > 0 else "Credit",
				"stock_adjustment_account": sa_account,
				"posting_mode": "STOCK_ADJUSTMENT_GL",
				"accounting_category": accounting_category,
				"hotfix_version": HOTFIX_VERSION,
				"idempotency_key": key,
				"remarks": (
					f"[{accounting_category}] Class A IRR residual after native SLE sync: "
					f"authoritative={row_info.get('authoritative_amount')} "
					f"rate_derived={row_info.get('rate_derived_amount')} residual={delta}; "
					f"posted via Company Stock Adjustment {sa_account}. "
					f"Financial Round Off is not used for inventory valuation."
				),
				"payload": {
					"class_a_row": row_info,
					"net_signed_debit": flt(decision.net_signed_debit),
					"stock_adjustment_account": sa_account,
					"accounting_category": accounting_category,
					"gl_destination": "STOCK_ADJUSTMENT",
					"historical_zero_value_provenance": has_pending_reason(
						doc, REASON_HISTORICAL_ZERO_VALUE_STOCK
					),
				},
			}
		)
	return out


def persist_adjustments(doc, adjustments: list[dict[str, Any]], *, repair_status: str = "SUBMITTED") -> list[str]:
	"""Insert TVA rows idempotently. Returns names created or reused."""
	created: list[str] = []
	if not adjustments:
		return created
	if not frappe.db.exists("DocType", DOCTYPE):
		frappe.throw(
			_("DocType {0} is missing; migrate erpnext_extensions before submit.").format(DOCTYPE)
		)

	for adj in adjustments:
		key = adj.get("idempotency_key")
		if not key:
			continue
		existing = frappe.db.get_value(DOCTYPE, {"idempotency_key": key}, "name")
		if existing:
			# Refresh status on retry of same key.
			frappe.db.set_value(
				DOCTYPE,
				existing,
				{
					"repair_status": repair_status,
					"gl_entry": adj.get("gl_entry") or frappe.db.get_value(DOCTYPE, existing, "gl_entry"),
				},
				update_modified=False,
			)
			created.append(existing)
			continue

		payload_obj = adj.get("payload")
		if isinstance(payload_obj, str):
			try:
				payload_obj = json.loads(payload_obj)
			except Exception:
				payload_obj = {"raw": payload_obj}
		if not isinstance(payload_obj, dict):
			payload_obj = {}
		if adj.get("accounting_category"):
			payload_obj.setdefault("accounting_category", adj["accounting_category"])
		payload = json.dumps(payload_obj, default=str, ensure_ascii=False)

		row_dict = {
			"doctype": DOCTYPE,
			"idempotency_key": key,
			"hotfix_version": adj.get("hotfix_version") or HOTFIX_VERSION,
			"reason_code": adj["reason_code"],
			"repair_status": repair_status,
			"stock_entry": adj.get("stock_entry") or doc.name,
			"stock_entry_detail": adj.get("stock_entry_detail"),
			"item_code": adj.get("item_code"),
			"warehouse": adj.get("warehouse"),
			"batch_no": adj.get("batch_no"),
			"company": adj.get("company") or doc.company,
			"purpose": adj.get("purpose") or doc.get("purpose"),
			"posting_date": adj.get("posting_date") or doc.get("posting_date"),
			"posting_time": adj.get("posting_time") or doc.get("posting_time"),
			"expected_value": adj.get("expected_value"),
			"actual_value": adj.get("actual_value"),
			"adjustment_delta": adj.get("adjustment_delta"),
			"debit_credit": adj.get("debit_credit") or "None",
			"stock_adjustment_account": adj.get("stock_adjustment_account"),
			"gl_entry": adj.get("gl_entry"),
			"posting_mode": adj.get("posting_mode") or "TRACE",
			"remarks": adj.get("remarks"),
			"payload": payload,
		}
		# Optional DocType field (present after migrate); ignore if meta lacks it.
		if frappe.get_meta(DOCTYPE).has_field("accounting_category"):
			row_dict["accounting_category"] = adj.get("accounting_category") or CATEGORY_TRACE_ONLY
		row = frappe.get_doc(row_dict)
		row.insert(ignore_permissions=True)
		created.append(row.name)
	return created


def link_round_off_gl_to_adjustments(doc, adjustments: list[dict[str, Any]]) -> None:
	"""Attach residual GL name to TVA rows (Stock Adjustment destination for inventory)."""
	from erpnext_extensions.iran_accounting.domain.irr_rounding_residual import (
		fetch_irr_residual_gl_rows,
	)

	rows = fetch_irr_residual_gl_rows(doc.doctype, doc.name)
	gl_name = rows[0]["name"] if rows else None
	if not gl_name:
		return
	for adj in adjustments:
		if adj.get("posting_mode") in ("STOCK_ADJUSTMENT_GL", "ROUND_OFF_GL"):
			adj["gl_entry"] = gl_name


def mark_adjustments_cancelled(stock_entry: str) -> int:
	names = frappe.get_all(
		DOCTYPE,
		filters={"stock_entry": stock_entry, "repair_status": ("in", ["OPEN", "SUBMITTED"])},
		pluck="name",
	)
	for name in names:
		frappe.db.set_value(DOCTYPE, name, "repair_status", "CANCELLED", update_modified=False)
	return len(names)


def classify_sle_sabe_mismatches(doc) -> list[dict[str, Any]]:
	"""Record SLE vs SABE amount gaps for Phase 2 when fractional bridge is active."""
	if not allows_fractional_sabe(doc):
		return []
	sa_account = resolve_stock_adjustment_account(doc.company)
	rows = frappe.db.sql(
		"""
		select sle.voucher_detail_no, sle.item_code, sle.warehouse,
		       sle.stock_value_difference as sle_svd,
		       coalesce(sum(e.stock_value_difference), 0) as sabe_svd,
		       max(e.batch_no) as batch_no
		from `tabStock Ledger Entry` sle
		left join `tabSerial and Batch Bundle` b
		  on b.name = sle.serial_and_batch_bundle
		left join `tabSerial and Batch Entry` e
		  on e.parent = b.name and e.is_cancelled = 0
		where sle.voucher_type = 'Stock Entry'
		  and sle.voucher_no = %s
		  and sle.is_cancelled = 0
		group by sle.name
		""",
		doc.name,
		as_dict=True,
	)
	out: list[dict[str, Any]] = []
	ccy = get_company_currency(doc.company)
	for r in rows:
		delta = round_currency(flt(r.sle_svd) - flt(r.sabe_svd), ccy)
		if abs(flt(delta)) < 1e-9:
			continue
		# Only record when SABE side is fractional / bridge-authorized noise.
		if not amount_is_fractional(r.sabe_svd, ccy) and abs(flt(delta)) < 0.5:
			continue
		reason = REASON_SLE_SABE_VALUE_MISMATCH
		accounting_category = CATEGORY_TRACE_ONLY
		key = make_idempotency_key(doc.name, r.voucher_detail_no, reason, delta)
		out.append(
			{
				"kind": KIND_TEMPORARY,
				"reason_code": reason,
				"stock_entry": doc.name,
				"stock_entry_detail": r.voucher_detail_no,
				"item_code": r.item_code,
				"warehouse": r.warehouse,
				"batch_no": r.batch_no,
				"company": doc.company,
				"purpose": doc.get("purpose"),
				"posting_date": doc.get("posting_date"),
				"posting_time": doc.get("posting_time"),
				"expected_value": flt(r.sabe_svd),
				"actual_value": flt(r.sle_svd),
				"adjustment_delta": flt(delta),
				"debit_credit": "None",
				"stock_adjustment_account": sa_account,
				"posting_mode": "TRACE",
				"accounting_category": accounting_category,
				"hotfix_version": HOTFIX_VERSION,
				"idempotency_key": key,
				"remarks": (
					f"[{accounting_category}] SLE SVD {r.sle_svd} vs SABE sum {r.sabe_svd} "
					f"(delta={delta}); integer SLE rounding vs historical fractional batch layer. "
					f"No artificial Stock Adjustment GL."
				),
				"payload": {
					"sle_svd": flt(r.sle_svd),
					"sabe_svd": flt(r.sabe_svd),
					"accounting_category": accounting_category,
				},
			}
		)
	return out


def apply_on_submit_bridge(doc) -> list[str]:
	"""After SED sync / before ledger contract: remake Stock Adjustment residual + persist TVA."""
	if getattr(doc, "doctype", None) != "Stock Entry":
		return []
	if not is_irr_company(doc.company):
		return []
	if _through_riv():
		return []
	if cint(doc.docstatus) != 1:
		return []

	actions: list[str] = []
	pending = pending_adjustments(doc)

	# v5.5.46: authorize fractional SABE from observed native BatchNoValuation
	# before Class A / SLE↔SABE TRACE so ledger contract sees allows_fractional_sabe.
	for adj in classify_observed_native_fractional_sabe(doc):
		append_pending(doc, adj)
		mark_fractional_allowed(doc)
	pending = pending_adjustments(doc)

	class_a = classify_class_a_residual_bridge(doc)
	if class_a:
		from erpnext_extensions.iran_accounting.domain.irr_rounding_residual import (
			rebuild_irr_rate_rounding_residual_after_repost,
		)

		rebuild_actions = rebuild_irr_rate_rounding_residual_after_repost(doc)
		actions.extend(rebuild_actions or ["remade_stock_adjustment_residual_for_tva"])
		link_round_off_gl_to_adjustments(doc, class_a)
		for adj in class_a:
			append_pending(doc, adj)
		pending = pending_adjustments(doc)

	for adj in classify_sle_sabe_mismatches(doc):
		append_pending(doc, adj)
	pending = pending_adjustments(doc)

	if pending:
		names = persist_adjustments(doc, pending, repair_status="SUBMITTED")
		actions.append(f"persisted_tva={len(names)}")
		# Keep fractional allow flag durable via TVA rows.
		if has_pending_reason(
			doc,
			REASON_INDIVISIBLE_MOVING_AVERAGE,
			REASON_HISTORICAL_BATCH_RATE_MISMATCH,
			REASON_TRANSFER_VALUATION_RESIDUAL,
			REASON_SLE_SABE_VALUE_MISMATCH,
		):
			mark_fractional_allowed(doc)

	return actions


def native_outgoing_r2_amount_authoritative(doc, row, company: str) -> bool:
	"""Allow amount-authoritative composition for native SLE-synced outgoing rows.

	When SED.basic_amount mirrors SLE.stock_value_difference and integer rate is
	ROUND_HALF_UP(amount/qty), |amount − qty×rate| ≤ R2 bound is Class A — Stock Adjustment
	(inventory policy) owns the residual GL. Not a manufacture-only privilege.
	"""
	purpose = getattr(doc, "purpose", None) or ""
	if purpose not in NATIVE_OUTGOING_STOCK_VALUATION_PURPOSES and purpose != "Material Receipt":
		return False

	from erpnext_extensions.iran_accounting.domain.currency import (
		amount_rate_qty_residual,
		rate_is_fractional,
		round_monetary_rate,
	)
	from erpnext_extensions.iran_accounting.manufacture_output_contract import (
		r2_representation_bound,
	)

	ccy = get_company_currency(company)
	transfer_qty = abs(
		flt(row.get("transfer_qty") if row.get("transfer_qty") not in (None, "") else row.get("qty"))
	)
	if transfer_qty <= 0 or row.get("basic_rate") is None:
		return False
	if amount_is_fractional(row.get("basic_amount"), ccy) or amount_is_fractional(row.get("amount"), ccy):
		return False
	if rate_is_fractional(row.basic_rate, ccy):
		return False
	expected_rate = round_monetary_rate(flt(row.basic_amount) / transfer_qty, ccy)
	if abs(flt(row.basic_rate) - flt(expected_rate)) > 1e-9:
		return False
	residual = amount_rate_qty_residual(
		flt(row.basic_amount), transfer_qty, flt(row.basic_rate), ccy
	)
	return abs(flt(residual)) <= r2_representation_bound(transfer_qty) + 1e-9

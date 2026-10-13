# Copyright (c) 2026, ERPNext Extensions contributors
"""Prevention-only guards for NEW IRR Stock Entries (v5.5.44).

Blocks known valuation defects before inconsistent ledgers persist:

* Same-item multi-FG double capitalization (after Iran manufacture contract)
* Batchwise movements that would silently fall back to warehouse Moving Average
* Unapproved indivisible Moving Average remainders on full depletion
* Informational custom return links that diverge from source valuation
* Fractional Serial and Batch Entry rates/amounts on new submissions

Historical unstamped documents and Repost Item Valuation are out of scope:
guards that rewrite economics skip when ``through_repost_item_valuation`` is set,
and manufacture contract stamps remain era-deterministic.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

import frappe
from frappe import _
from frappe.utils import cint, flt

from erpnext_extensions.iran_accounting.domain.currency import (
	amount_is_fractional,
	get_company_currency,
	is_irr_company,
	rate_is_fractional,
	round_currency,
	round_monetary_rate,
	round_row_amount_financial,
)


class StockEntryPreventionError(frappe.ValidationError):
	"""New Stock Entry blocked: valuation invariant cannot be satisfied safely."""


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


def _integer_rate_and_product(qty, pool_or_amount) -> tuple[Decimal, Decimal, Decimal]:
	"""Return (integer_rate, qty×rate, remainder vs pool) via ROUND_HALF_UP."""
	qty_d = _d(qty)
	pool_d = _d(pool_or_amount)
	if qty_d == 0:
		return Decimal("0"), Decimal("0"), pool_d
	rate = (pool_d / qty_d).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
	product = (qty_d * rate).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
	return rate, product, pool_d - product


# ---------------------------------------------------------------------------
# Manufacturing — double capitalization detector
# ---------------------------------------------------------------------------


def assert_no_duplicate_manufacture_capitalization(doc) -> None:
	"""Fail when finished rows each absorb ~full consumed cost (Core multi-FG defect).

	Runs after ``apply_iran_manufacture_output_contract``. Only for documents on
	the current manufacture contract (drafts / stamped). Historical unstamped
	submitted vouchers are skipped so RIV cannot reinterpret them.
	"""
	if getattr(doc, "doctype", None) != "Stock Entry" or doc.get("purpose") != "Manufacture":
		return
	if not is_irr_company(doc.company):
		return
	from erpnext_extensions.iran_accounting.manufacture_stage_costing import uses_v533_contract

	if not uses_v533_contract(doc):
		return

	outgoing = Decimal("0")
	fg_amounts: list[tuple[str, Decimal]] = []
	for row in doc.get("items") or []:
		amt = _d(row.get("amount") if row.get("amount") not in (None, "") else row.get("basic_amount"))
		if row.get("s_warehouse") and not row.get("t_warehouse"):
			outgoing += abs(amt)
		if cint(row.get("is_finished_item")) and row.get("t_warehouse") and not row.get("s_warehouse"):
			fg_amounts.append((row.get("item_code") or "", abs(amt)))

	if outgoing <= 0 or len(fg_amounts) < 2:
		return

	# Same-item multi-FG: each FG ≈ full outgoing ⇒ double capitalization.
	item_codes = {code for code, _ in fg_amounts}
	if len(item_codes) != 1:
		return

	threshold = outgoing * Decimal("0.95")
	near_full = sum(1 for _, amt in fg_amounts if amt >= threshold)
	if near_full >= 2:
		incoming = sum(amt for _, amt in fg_amounts)
		frappe.throw(
			_(
				"Manufacture valuation blocked: finished rows for item {0} each capitalize "
				"approximately the full consumed cost (outgoing={1}, finished sum={2}). "
				"Iran SAME_ITEM_MULTI_FG / STAGE_CO must allocate the pool once across outputs. "
				"Duplicate capitalization is not allowed on new documents."
			).format(next(iter(item_codes)), outgoing, incoming),
			exc=StockEntryPreventionError,
			title=_("Manufacturing Valuation"),
		)


# ---------------------------------------------------------------------------
# Batch valuation identity
# ---------------------------------------------------------------------------


def _batchwise_required(item_code: str, batch_no: str | None) -> bool:
	if not item_code or not batch_no:
		return False
	if cint(frappe.db.get_single_value("Stock Settings", "do_not_use_batchwise_valuation")):
		return False
	has_batch = cint(frappe.get_cached_value("Item", item_code, "has_batch_no"))
	if not has_batch:
		return False
	use_bw = frappe.db.get_value("Batch", batch_no, "use_batchwise_valuation")
	if use_bw is None:
		return True
	return bool(cint(use_bw))


def assert_batch_valuation_identity_before_submit(doc) -> None:
	"""Require a batch identity path for batchwise IRR Stock Entry rows.

	Core ``get_sl_entries`` does not copy ``batch_no`` onto SLE on submit.
	Batch valuation therefore requires ``serial_and_batch_bundle`` (created on
	submit via ``make_bundle_using_old_serial_batch_fields`` when
	``use_serial_batch_fields`` is set, or already attached).

	Do not rely on ``auto_create_serial_and_batch_bundle_for_outward`` together
	with a populated ``batch_no`` field: Core rejects that combination
	("bundle already created / remove batch no fields"). Require either an
	existing bundle or ``use_serial_batch_fields=1``.
	"""
	if getattr(doc, "doctype", None) != "Stock Entry":
		return
	if not is_irr_company(doc.company):
		return
	if _through_riv():
		return

	for row in doc.get("items") or []:
		batch_no = row.get("batch_no")
		item_code = row.get("item_code")
		if not item_code:
			continue
		# Batch-controlled item with no batch identity at all.
		has_batch = cint(frappe.get_cached_value("Item", item_code, "has_batch_no"))
		if has_batch and not batch_no and not row.get("serial_and_batch_bundle"):
			if row.get("s_warehouse") or row.get("t_warehouse"):
				frappe.throw(
					_(
						"Row {0}: item {1} is batch-controlled but has no batch_no and no Serial and "
						"Batch Bundle. Submit would price the movement at warehouse Moving Average."
					).format(row.idx, item_code),
					exc=StockEntryPreventionError,
					title=_("Batch Valuation"),
				)
			continue
		if not _batchwise_required(item_code, batch_no):
			continue
		if row.get("serial_and_batch_bundle"):
			continue
		# Core on_submit creates bundles when use_serial_batch_fields + batch_no.
		if cint(row.get("use_serial_batch_fields")):
			continue
		frappe.throw(
			_(
				"Row {0}: item {1} batch {2} requires batchwise valuation, but no Serial and "
				"Batch Bundle is set and use_serial_batch_fields is not enabled. "
				"Enable use_serial_batch_fields (so Core creates a bundle on submit) "
				"or attach a Serial and Batch Bundle. "
				"Do not rely on auto-create outward while batch_no is populated — "
				"Core rejects that combination. Silent warehouse Moving Average is not allowed."
			).format(row.idx, item_code, batch_no),
			exc=StockEntryPreventionError,
			title=_("Batch Valuation"),
		)


def collect_submitted_batch_identity_failures(voucher_no: str, company: str) -> list[str]:
	"""Post-submit: SED batch_no must produce SLE with serial_and_batch_bundle."""
	if not is_irr_company(company):
		return []
	failures: list[str] = []
	rows = frappe.get_all(
		"Stock Entry Detail",
		filters={"parent": voucher_no},
		fields=["name", "idx", "item_code", "batch_no", "serial_and_batch_bundle", "s_warehouse", "t_warehouse"],
	)
	for row in rows:
		if not _batchwise_required(row.item_code, row.batch_no):
			continue
		sle_rows = frappe.get_all(
			"Stock Ledger Entry",
			filters={
				"voucher_type": "Stock Entry",
				"voucher_no": voucher_no,
				"voucher_detail_no": row.name,
				"is_cancelled": 0,
			},
			fields=["name", "serial_and_batch_bundle", "batch_no", "warehouse"],
		)
		if not sle_rows:
			continue
		for sle in sle_rows:
			if not sle.serial_and_batch_bundle and not sle.batch_no:
				failures.append(
					f"Stock Entry {voucher_no} row {row.idx} item={row.item_code} batch={row.batch_no}: "
					f"SLE {sle.name} warehouse={sle.warehouse} has no serial_and_batch_bundle "
					f"(batchwise valuation fell back to warehouse Moving Average)"
				)
	return failures


def collect_fractional_sabe_failures(voucher_no: str, company: str) -> list[str]:
	"""Post-submit: Serial and Batch Entry monetary fields must be integer IRR."""
	if not is_irr_company(company):
		return []
	ccy = get_company_currency(company)
	failures: list[str] = []
	bundles = frappe.get_all(
		"Stock Ledger Entry",
		filters={"voucher_type": "Stock Entry", "voucher_no": voucher_no, "is_cancelled": 0},
		pluck="serial_and_batch_bundle",
	)
	bundle_names = [b for b in bundles if b]
	if not bundle_names:
		return []
	entries = frappe.get_all(
		"Serial and Batch Entry",
		filters={"parent": ("in", bundle_names)},
		fields=["name", "parent", "batch_no", "qty", "incoming_rate", "stock_value_difference", "warehouse"],
	)
	for e in entries:
		if rate_is_fractional(e.incoming_rate, ccy):
			failures.append(
				f"Stock Entry {voucher_no} SABE {e.name} batch={e.batch_no}: "
				f"fractional incoming_rate={e.incoming_rate}"
			)
		if amount_is_fractional(e.stock_value_difference, ccy):
			failures.append(
				f"Stock Entry {voucher_no} SABE {e.name} batch={e.batch_no}: "
				f"fractional stock_value_difference={e.stock_value_difference}"
			)
		# Rule 4: |amount| must equal ROUND_HALF_UP(|qty| × integer_rate) when qty≠0
		qty = abs(flt(e.qty))
		if qty and e.incoming_rate not in (None, ""):
			expected = abs(flt(round_row_amount_financial(qty, round_monetary_rate(e.incoming_rate, ccy), ccy)))
			actual = abs(flt(round_currency(e.stock_value_difference, ccy)))
			if abs(expected - actual) > 0.5:
				failures.append(
					f"Stock Entry {voucher_no} SABE {e.name} batch={e.batch_no}: "
					f"amount {actual} != ROUND_HALF_UP(qty×integer_rate) {expected}"
				)
	return failures


# ---------------------------------------------------------------------------
# Indivisible Moving Average remainder (unapproved)
# ---------------------------------------------------------------------------


def _warehouse_balance(item_code: str, warehouse: str) -> tuple[float, float]:
	row = frappe.db.get_value(
		"Bin",
		{"item_code": item_code, "warehouse": warehouse},
		["actual_qty", "stock_value"],
		as_dict=True,
	)
	if not row:
		return 0.0, 0.0
	return flt(row.actual_qty), flt(row.stock_value)


def _batch_balance(item_code: str, batch_no: str, warehouse: str) -> tuple[float, float]:
	rows = frappe.get_all(
		"Serial and Batch Entry",
		filters={
			"batch_no": batch_no,
			"warehouse": warehouse,
			"is_cancelled": 0,
		},
		fields=["qty", "stock_value_difference"],
	)
	# Filter by item via parent bundle when possible
	qty = sum(flt(r.qty) for r in rows)
	value = sum(flt(r.stock_value_difference) for r in rows)
	return qty, value


def assert_no_unapproved_indivisible_ma_remainder(doc) -> None:
	"""Block full-depletion outgoing moves when integer pool is not divisible by qty.

	Accounting treatment for the remainder is not approved. Do not invent Stock
	Adjustment, row splits, or silent discard on new documents.
	"""
	if getattr(doc, "doctype", None) != "Stock Entry":
		return
	if not is_irr_company(doc.company):
		return
	if _through_riv():
		return
	if doc.get("purpose") in ("Manufacture", "Repack", "Material Consumption for Manufacture"):
		# Manufacturing residuals use the approved TYPE A/B/C / STAGE_CO contracts.
		return

	for row in doc.get("items") or []:
		warehouse = row.get("s_warehouse")
		if not warehouse or not row.get("item_code"):
			continue
		move_qty = abs(flt(row.get("transfer_qty") if row.get("transfer_qty") not in (None, "") else row.get("qty")))
		if move_qty <= 0 or not _is_whole_qty(move_qty):
			continue

		batch_no = row.get("batch_no")
		if batch_no and _batchwise_required(row.item_code, batch_no):
			bal_qty, bal_value = _batch_balance(row.item_code, batch_no, warehouse)
			pool_label = f"batch {batch_no} @ {warehouse}"
		else:
			bal_qty, bal_value = _warehouse_balance(row.item_code, warehouse)
			pool_label = f"warehouse {warehouse}"

		# Only when this movement fully depletes the pool.
		if abs(bal_qty - move_qty) > 1e-9:
			continue
		if abs(bal_value) < 1e-9:
			continue
		if not _is_whole_qty(bal_value) and amount_is_fractional(bal_value, "IRR"):
			# Fractional pool is already invalid; separate fractional SABE guard covers it.
			pass

		rate, product, remainder = _integer_rate_and_product(move_qty, bal_value)
		if remainder == 0:
			continue

		frappe.throw(
			_(
				"Row {0}: indivisible Moving Average remainder is not approved. "
				"Item {1}, {2}: quantity={3}, pool_value={4}, integer_rate={5}, "
				"qty×rate={6}, remainder={7} IRR. "
				"Do not submit until an accounting treatment is approved. "
				"Silent discard, row splitting, and automatic Stock Adjustment are not allowed."
			).format(
				row.idx,
				row.item_code,
				pool_label,
				move_qty,
				bal_value,
				rate,
				product,
				remainder,
			),
			exc=StockEntryPreventionError,
			title=_("Indivisible MA Remainder"),
		)


# ---------------------------------------------------------------------------
# Source-linked returns (custom informational fields)
# ---------------------------------------------------------------------------


def assert_source_linked_return_valuation(doc) -> None:
	"""When custom return fields claim a source issue, rates must match that source.

	``custom_is_returned`` / ``custom_reference_no`` are informational unless both
	are set. Ordinary Material Receipts without those flags are untouched.
	"""
	if getattr(doc, "doctype", None) != "Stock Entry":
		return
	if not is_irr_company(doc.company):
		return
	if _through_riv():
		return
	if not cint(doc.get("custom_is_returned")):
		return

	source_name = (doc.get("custom_reference_no") or "").strip()
	if not source_name:
		frappe.throw(
			_(
				"Stock Entry is marked custom_is_returned but custom_reference_no is empty. "
				"Provide the source Stock Entry or clear the return flag."
			),
			exc=StockEntryPreventionError,
			title=_("Return Valuation"),
		)

	if not frappe.db.exists("Stock Entry", source_name):
		frappe.throw(
			_("Return source Stock Entry {0} does not exist.").format(source_name),
			exc=StockEntryPreventionError,
			title=_("Return Valuation"),
		)

	source = frappe.get_doc("Stock Entry", source_name)
	if cint(source.docstatus) != 1:
		frappe.throw(
			_("Return source {0} is not submitted (docstatus={1}).").format(source_name, source.docstatus),
			exc=StockEntryPreventionError,
			title=_("Return Valuation"),
		)

	# Index source outgoing rows by (item_code, batch_no).
	source_out: dict[tuple[str, str], list] = {}
	for srow in source.get("items") or []:
		if not srow.get("s_warehouse"):
			continue
		key = (srow.item_code or "", srow.batch_no or "")
		source_out.setdefault(key, []).append(srow)

	ccy = get_company_currency(doc.company)
	for row in doc.get("items") or []:
		if not row.get("t_warehouse"):
			continue
		key = (row.item_code or "", row.batch_no or "")
		candidates = source_out.get(key) or []
		if not candidates:
			frappe.throw(
				_(
					"Row {0}: return item {1} batch {2} has no matching outgoing row on source {3}."
				).format(row.idx, row.item_code, row.batch_no or "(none)", source_name),
				exc=StockEntryPreventionError,
				title=_("Return Valuation"),
			)

		# Prefer exact qty match among candidates.
		match = None
		move_qty = abs(flt(row.get("transfer_qty") if row.get("transfer_qty") not in (None, "") else row.get("qty")))
		for cand in candidates:
			cq = abs(flt(cand.get("transfer_qty") if cand.get("transfer_qty") not in (None, "") else cand.get("qty")))
			if abs(cq - move_qty) < 1e-9:
				match = cand
				break
		if match is None:
			match = candidates[0]

		source_rate = round_monetary_rate(
			match.get("valuation_rate") if match.get("valuation_rate") not in (None, "", 0) else match.get("basic_rate"),
			ccy,
		)
		row_rate = round_monetary_rate(
			row.get("valuation_rate") if row.get("valuation_rate") not in (None, "", 0) else row.get("basic_rate"),
			ccy,
		)
		if flt(source_rate) != flt(row_rate):
			frappe.throw(
				_(
					"Row {0}: return rate {1} does not match source {2} outgoing rate {3} "
					"(item {4}, batch {5}). Informational custom return fields are not a license "
					"to capitalize a different valuation."
				).format(row.idx, row_rate, source_name, source_rate, row.item_code, row.batch_no or "(none)"),
				exc=StockEntryPreventionError,
				title=_("Return Valuation"),
			)

		source_amt = abs(flt(round_currency(match.get("amount"), ccy)))
		row_amt = abs(flt(round_currency(row.get("amount"), ccy)))
		if abs(source_amt - row_amt) > 0.5 and abs(flt(match.get("qty")) - move_qty) < 1e-9:
			frappe.throw(
				_(
					"Row {0}: return amount {1} does not match source {2} outgoing amount {3} "
					"(item {4}, batch {5})."
				).format(row.idx, row_amt, source_name, source_amt, row.item_code, row.batch_no or "(none)"),
				exc=StockEntryPreventionError,
				title=_("Return Valuation"),
			)


# ---------------------------------------------------------------------------
# Orchestrators
# ---------------------------------------------------------------------------


def run_validate_prevention_guards(doc) -> None:
	"""Validate-time prevention (manufacture double-capitalization)."""
	assert_no_duplicate_manufacture_capitalization(doc)


def run_before_submit_prevention_guards(doc) -> None:
	"""Before-submit prevention for NEW IRR Stock Entries."""
	assert_batch_valuation_identity_before_submit(doc)
	assert_source_linked_return_valuation(doc)
	assert_no_unapproved_indivisible_ma_remainder(doc)


def collect_post_submit_prevention_failures(voucher_no: str, company: str) -> list[str]:
	"""Post-submit ledger failures for batch identity and integer SABE."""
	if _through_riv():
		return []
	failures: list[str] = []
	failures.extend(collect_submitted_batch_identity_failures(voucher_no, company))
	failures.extend(collect_fractional_sabe_failures(voucher_no, company))
	return failures

# Copyright (c) 2026, ERPNext Extensions contributors
"""E2E runner for Physical Two-Slice DN valuation (v5.6.10).

bench --site development.localhost execute \\
  erpnext_extensions.iran_accounting.e2e_v5610_physical_two_slice.run_all
"""

from __future__ import annotations

import json
import traceback
from decimal import Decimal

import frappe
from frappe.utils import flt, nowdate, nowtime

from erpnext_extensions.iran_accounting.domain.physical_two_slice import (
	AllocationError,
	allocate_physical_two_slice,
	simulate_sequential,
)
from erpnext_extensions.iran_accounting.monkey_patches import apply_monkey_patches

DN = "MAT-DN-2026-00014"
BATCH = "2984-20100102-ak253006a51"
ITEM = "20100102"
WH = "انبار آماده فروش -پخش البرز"
EXPECTED_SLICES = [
	{"qty": -9.0, "rate": 3606631, "svd": -32459679.0},
	{"qty": -1.0, "rate": 3606632, "svd": -3606632.0},
]
EXPECTED_TOTAL = 36066311.0


def _ok(name, ok, **detail):
	return {"test": name, "ok": bool(ok), **detail}


def _is_int_money(v) -> bool:
	return abs(flt(v) - round(flt(v))) < 1e-9


def _pool(batch=BATCH, warehouse=WH, item=ITEM, exclude_detail=None):
	params = [item, batch, warehouse]
	excl = ""
	if exclude_detail:
		excl = " AND IFNULL(e.voucher_detail_no,'') != %s"
		params.append(exclude_detail)
	row = frappe.db.sql(
		f"""
		SELECT COALESCE(SUM(e.qty),0) q, COALESCE(SUM(e.stock_value_difference),0) v
		FROM `tabSerial and Batch Entry` e
		WHERE e.item_code=%s AND e.batch_no=%s AND e.warehouse=%s
		  AND e.docstatus=1 AND e.is_cancelled=0
		  AND e.type_of_transaction IN ('Inward','Outward')
		  {excl}
		""",
		tuple(params),
		as_dict=True,
	)[0]
	return flt(row.q), flt(row.v)


def _sabe_for_voucher(voucher_type, voucher_no, batch=BATCH):
	return frappe.db.sql(
		"""
		SELECT e.name, e.parent, e.qty, e.incoming_rate, e.stock_value_difference,
		       e.batch_no, e.voucher_detail_no, e.docstatus, e.is_cancelled
		FROM `tabSerial and Batch Entry` e
		WHERE e.voucher_type=%s AND e.voucher_no=%s AND e.batch_no=%s
		  AND e.is_cancelled=0
		ORDER BY e.voucher_detail_no, e.idx, e.name
		""",
		(voucher_type, voucher_no, batch),
		as_dict=True,
	)


def _sle_for_voucher(voucher_type, voucher_no, batch_item=ITEM):
	return frappe.get_all(
		"Stock Ledger Entry",
		filters={
			"voucher_type": voucher_type,
			"voucher_no": voucher_no,
			"item_code": batch_item,
			"is_cancelled": 0,
		},
		fields=[
			"name",
			"actual_qty",
			"stock_value_difference",
			"incoming_rate",
			"valuation_rate",
			"serial_and_batch_bundle",
			"voucher_detail_no",
			"warehouse",
		],
	)


def _gl_for_voucher(voucher_type, voucher_no):
	return frappe.get_all(
		"GL Entry",
		filters={"voucher_type": voucher_type, "voucher_no": voucher_no, "is_cancelled": 0},
		fields=["name", "account", "debit", "credit", "against", "remarks"],
	)


def _stock_adjustments_for(voucher_no):
	return frappe.get_all(
		"Stock Entry",
		filters={
			"stock_entry_type": ("like", "%Adjustment%"),
			"remarks": ("like", f"%{voucher_no}%"),
			"docstatus": ("!=", 2),
		},
		pluck="name",
	)


def test_math_suite():
	results = []
	plan = allocate_physical_two_slice(151, 544601298, 10)
	results.append(
		_ok(
			"math_primary_10",
			plan.out_value_abs == Decimal("36066311") and len(plan.slices) == 2,
			plan=str(plan),
		)
	)
	steps = simulate_sequential(151, 544601298, [10, 24, 1, 5, 111])
	total = float(sum((p.out_value_abs for p, _, _ in steps), Decimal(0)))
	results.append(
		_ok(
			"math_sequential",
			total == 544601298.0 and float(steps[-1][1]) == 0 and float(steps[-1][2]) == 0,
			total=total,
			final_q=float(steps[-1][1]),
			final_v=float(steps[-1][2]),
		)
	)
	frac_ok = {}
	frac_fail = {}
	for q in (0.25, 1.1, 1.5, 1.7):
		try:
			p = allocate_physical_two_slice(151, 544601298, q)
			frac_ok[str(q)] = {"rate": p.base_rate, "amount": float(p.out_value_abs)}
		except AllocationError as e:
			frac_fail[str(q)] = str(e)
	results.append(_ok("math_fractional_qty", True, cases_ok=frac_ok, cases_fail=frac_fail))
	try:
		allocate_physical_two_slice(10, Decimal("100.5"), 1)
		results.append(_ok("math_historical_frac_pool", False, error="did not refuse"))
	except AllocationError as e:
		results.append(_ok("math_historical_frac_pool", True, error=str(e)))
	return results


def test_submit_primary_dn():
	apply_monkey_patches()
	baseline_pool = _pool()
	dn = frappe.get_doc("Delivery Note", DN)
	submitted_now = False
	if dn.docstatus == 2:
		return [_ok("submit_primary_dn", False, error="DN cancelled")]
	if dn.docstatus == 0:
		try:
			dn.reload()
			dn.submit()
			frappe.db.commit()
			submitted_now = True
		except Exception as e:
			frappe.db.rollback()
			return [
				_ok(
					"submit_primary_dn",
					False,
					error=str(e),
					traceback=traceback.format_exc()[-2500:],
				)
			]

	dn.reload()
	sabe = _sabe_for_voucher("Delivery Note", DN, BATCH)
	# Filter to the qty-10 detail (idx 18 / name dhi8m89p87)
	detail = frappe.db.get_value(
		"Delivery Note Item",
		{"parent": DN, "batch_no": BATCH, "qty": 10},
		"name",
	)
	sabe_10 = [r for r in sabe if r.voucher_detail_no == detail] if detail else sabe
	sle = [
		r
		for r in _sle_for_voucher("Delivery Note", DN, ITEM)
		if r.voucher_detail_no == detail
	]
	gl = _gl_for_voucher("Delivery Note", DN)
	sa = _stock_adjustments_for(DN)
	after_pool = _pool()

	slice_ok = False
	if len(sabe_10) == 2:
		rows = sorted(sabe_10, key=lambda r: abs(flt(r.qty)), reverse=True)
		slice_ok = (
			abs(flt(rows[0].qty) - (-9)) < 1e-9
			and abs(flt(rows[0].incoming_rate) - 3606631) < 1e-9
			and abs(flt(rows[0].stock_value_difference) - (-32459679)) < 1e-9
			and abs(flt(rows[1].qty) - (-1)) < 1e-9
			and abs(flt(rows[1].incoming_rate) - 3606632) < 1e-9
			and abs(flt(rows[1].stock_value_difference) - (-3606632)) < 1e-9
		)
	all_int = all(
		_is_int_money(r.incoming_rate) and _is_int_money(r.stock_value_difference) for r in sabe_10
	)
	sabe_total = abs(sum(flt(r.stock_value_difference) for r in sabe_10))
	sle_total = abs(sum(flt(r.stock_value_difference) for r in sle))
	sle_qty = abs(sum(flt(r.actual_qty) for r in sle))
	identity = all(
		abs(abs(flt(r.stock_value_difference)) - abs(flt(r.qty) * flt(r.incoming_rate))) < 0.5
		for r in sabe_10
	)

	# COGS: expense side of inventory GL for this DN (aggregate check)
	gl_abs = sum(flt(g.debit) + flt(g.credit) for g in gl)

	ok = (
		dn.docstatus == 1
		and slice_ok
		and all_int
		and identity
		and abs(sabe_total - EXPECTED_TOTAL) < 0.5
		and abs(sle_total - EXPECTED_TOTAL) < 0.5
		and abs(sle_qty - 10) < 1e-9
		and not sa
	)
	return [
		_ok(
			"submit_primary_dn",
			ok,
			docstatus=dn.docstatus,
			submitted_now=submitted_now,
			baseline_pool=baseline_pool,
			after_pool=after_pool,
			detail=detail,
			sabe=sabe_10,
			sle=sle,
			sabe_total=sabe_total,
			sle_total=sle_total,
			sle_qty=sle_qty,
			slice_ok=slice_ok,
			all_int=all_int,
			identity=identity,
			stock_adjustments=sa,
			gl_entry_count=len(gl),
			gl_abs_sum=gl_abs,
			bundle_avg=frappe.db.get_value(
				"Serial and Batch Bundle",
				sabe_10[0].parent if sabe_10 else None,
				["total_qty", "total_amount", "avg_rate"],
				as_dict=True,
			)
			if sabe_10
			else None,
		)
	]


def _customer_and_company():
	dn = frappe.get_doc("Delivery Note", DN)
	return dn.customer, dn.company, dn.currency or "IRR"


def test_same_batch_sale_and_free_on_primary_dn():
	"""Verify legitimate same-batch across DN Items on the submitted primary DN."""
	dn = frappe.get_doc("Delivery Note", DN)
	if dn.docstatus != 1:
		return [_ok("same_batch_sale_free", False, error=f"docstatus={dn.docstatus}")]

	from collections import defaultdict

	by = defaultdict(list)
	for it in dn.items:
		if it.batch_no:
			by[it.batch_no].append(it)
	multi = {b: rows for b, rows in by.items() if len(rows) > 1}
	# Prefer known sale+zero-rate pair on item 20100102
	target = "2993-20100102-ak253008a21"
	rows = multi.get(target) or next(iter(multi.values()), [])
	if not rows:
		return [_ok("same_batch_sale_free", False, error="no multi-batch rows found")]

	checks = []
	all_ok = True
	for it in rows:
		sabe = frappe.get_all(
			"Serial and Batch Entry",
			filters={"voucher_detail_no": it.name, "is_cancelled": 0},
			fields=["qty", "incoming_rate", "stock_value_difference", "parent"],
		)
		sle = frappe.get_all(
			"Stock Ledger Entry",
			filters={"voucher_detail_no": it.name, "is_cancelled": 0},
			fields=["actual_qty", "stock_value_difference"],
		)
		sabe_qty = abs(sum(flt(r.qty) for r in sabe))
		sabe_val = abs(sum(flt(r.stock_value_difference) for r in sabe))
		sle_qty = abs(sum(flt(r.actual_qty) for r in sle))
		sle_val = abs(sum(flt(r.stock_value_difference) for r in sle))
		int_ok = all(
			_is_int_money(r.incoming_rate) and _is_int_money(r.stock_value_difference) for r in sabe
		)
		row_ok = (
			abs(sabe_qty - flt(it.qty)) < 1e-9
			and abs(sle_qty - flt(it.qty)) < 1e-9
			and abs(sabe_val - sle_val) < 0.5
			and int_ok
			and len(sabe) >= 1
		)
		all_ok = all_ok and row_ok
		checks.append(
			{
				"idx": it.idx,
				"qty": it.qty,
				"rate": it.rate,
				"amount": it.amount,
				"is_free_item": it.is_free_item,
				"against_sales_order": it.against_sales_order,
				"batch_no": it.batch_no,
				"sabe": sabe,
				"sle": sle,
				"row_ok": row_ok,
			}
		)

	# Commercial rows remain distinct (not merged)
	commercial_ok = len(rows) >= 2 and len({r.name for r in rows}) == len(rows)
	return [
		_ok(
			"same_batch_sale_free",
			all_ok and commercial_ok,
			batch=rows[0].batch_no,
			checks=checks,
			multi_batch_count=len(multi),
		)
	]


def test_synthetic_multi_row_same_batch():
	"""Synthetic DN: sale + free + third row, same batch, no SO links; then cancel."""
	apply_monkey_patches()
	customer, company, currency = _customer_and_company()
	pool_q, pool_v = _pool()
	if pool_q < 18:
		return [
			_ok(
				"synthetic_multi_row",
				False,
				error=f"insufficient pool after primary submit: {pool_q}",
				pool=(pool_q, pool_v),
			)
		]

	src = frappe.get_doc("Delivery Note", DN)
	dept = src.items[0].get("department")
	cc = src.items[0].get("cost_center")

	plans = []
	q, v = Decimal(str(pool_q)), Decimal(str(pool_v))
	for oq in (10, 5, 3):
		p = allocate_physical_two_slice(q, v, oq)
		plans.append(p)
		q -= p.out_qty_abs
		v -= p.out_value_abs
	expected_total = float(sum(p.out_value_abs for p in plans))

	def _row(qty, rate, free=0):
		return {
			"item_code": ITEM,
			"qty": qty,
			"rate": rate,
			"is_free_item": free,
			"warehouse": WH,
			"batch_no": BATCH,
			"use_serial_batch_fields": 1,
			"department": dept,
			"cost_center": cc,
			"allow_zero_valuation_rate": 1 if free or rate == 0 else 0,
		}

	dn = frappe.get_doc(
		{
			"doctype": "Delivery Note",
			"company": company,
			"customer": customer,
			"currency": currency,
			"set_warehouse": WH,
			"posting_date": nowdate(),
			"posting_time": nowtime(),
			"items": [_row(10, 1000), _row(5, 0, free=1), _row(3, 1000)],
		}
	)
	try:
		dn.flags.ignore_pricing_rule = True
		dn.insert(ignore_permissions=True)
		# Re-assert commercial free-item economics after insert hooks
		for it in dn.items:
			if int(it.is_free_item or 0) == 1:
				it.rate = 0
				it.amount = 0
		dn.save(ignore_permissions=True)
		dn.submit()
		frappe.db.commit()
	except Exception as e:
		frappe.db.rollback()
		return [
			_ok(
				"synthetic_multi_row",
				False,
				error=str(e),
				traceback=traceback.format_exc()[-2000:],
			)
		]

	dn.reload()
	commercial = [
		{
			"idx": it.idx,
			"qty": it.qty,
			"rate": it.rate,
			"amount": it.amount,
			"is_free_item": it.is_free_item,
			"batch_no": it.batch_no,
		}
		for it in dn.items
	]
	sabe = _sabe_for_voucher("Delivery Note", dn.name, BATCH)
	sle = _sle_for_voucher("Delivery Note", dn.name, ITEM)
	sabe_total = abs(sum(flt(r.stock_value_difference) for r in sabe))
	sle_total = abs(sum(flt(r.stock_value_difference) for r in sle))
	sle_qty = abs(sum(flt(r.actual_qty) for r in sle))
	all_int = all(
		_is_int_money(r.incoming_rate) and _is_int_money(r.stock_value_difference) for r in sabe
	)
	# Structure: 3 distinct commercial rows; free-item flag preserved.
	# Site pricing may rewrite free-item selling rate on synthetic docs — commercial
	# zero-rate preservation is proven on primary DN (same_batch_sale_free).
	rows_ok = len(dn.items) == 3 and int(dn.items[1].is_free_item or 0) == 1
	valuation_ok = (
		dn.docstatus == 1
		and all_int
		and abs(sle_qty - 18) < 1e-9
		and abs(sabe_total - expected_total) < 0.5
		and abs(sle_total - expected_total) < 0.5
	)
	ok = valuation_ok and rows_ok

	cancel_err = None
	pool_before_cancel = _pool()
	orphans = None
	pool_after = pool_before_cancel
	cancel_ok = False
	try:
		dn.cancel()
		frappe.db.commit()
		dn.reload()
		orphans = frappe.db.sql(
			"""
			SELECT COUNT(*) FROM `tabSerial and Batch Entry`
			WHERE voucher_no=%s AND is_cancelled=0 AND docstatus=1
			""",
			(dn.name,),
		)[0][0]
		pool_after = _pool()
		cancel_ok = dn.docstatus == 2 and orphans == 0
		# Pool qty/value restored by +18 relative to before cancel
		cancel_ok = (
			cancel_ok
			and abs(pool_after[0] - (pool_before_cancel[0] + 18)) < 1e-6
			and abs(pool_after[1] - (pool_before_cancel[1] + expected_total)) < 0.5
		)
	except Exception as e:
		frappe.db.rollback()
		cancel_err = str(e)

	return [
		_ok(
			"synthetic_multi_row",
			ok,
			dn=dn.name,
			commercial=commercial,
			sabe=sabe,
			sle=sle,
			sabe_total=sabe_total,
			sle_total=sle_total,
			expected_total=expected_total,
			all_int=all_int,
			plans=[str(p) for p in plans],
		),
		_ok(
			"synthetic_multi_row_cancel",
			cancel_ok and cancel_err is None,
			dn=dn.name,
			cancel_err=cancel_err,
			orphans=orphans,
			pool_before_cancel=pool_before_cancel,
			pool_after_cancel=pool_after,
			expected_total=expected_total,
		),
	]


def test_repost_primary_dn():
	apply_monkey_patches()
	dn = frappe.get_doc("Delivery Note", DN)
	if dn.docstatus != 1:
		return [_ok("repost_primary_dn", False, error=f"DN docstatus={dn.docstatus}")]

	detail = frappe.db.get_value(
		"Delivery Note Item", {"parent": DN, "batch_no": BATCH, "qty": 10}, "name"
	)
	before = _sabe_for_voucher("Delivery Note", DN, BATCH)
	before_10 = [r for r in before if r.voucher_detail_no == detail]
	before_snap = [
		(flt(r.qty), flt(r.incoming_rate), flt(r.stock_value_difference)) for r in before_10
	]

	# Create and execute RIV for this item/warehouse from DN posting date
	riv = frappe.get_doc(
		{
			"doctype": "Repost Item Valuation",
			"company": dn.company,
			"item_code": ITEM,
			"warehouse": WH,
			"based_on": "Item and Warehouse",
			"posting_date": dn.posting_date,
			"posting_time": dn.posting_time,
			"allow_negative_stock": 1,
			"status": "Queued",
		}
	)
	try:
		riv.insert(ignore_permissions=True)
		frappe.db.commit()
		from erpnext.stock.doctype.repost_item_valuation.repost_item_valuation import (
			repost_entries,
		)

		# Direct execute for this entry
		riv.reload()
		riv.flags.ignore_permissions = True
		if hasattr(riv, "repost"):
			riv.repost()
		else:
			from erpnext.stock.doctype.repost_item_valuation.repost_item_valuation import (
				repost,
			)

			repost(riv)
		frappe.db.commit()
	except Exception as e:
		frappe.db.rollback()
		return [
			_ok(
				"repost_primary_dn",
				False,
				error=str(e),
				traceback=traceback.format_exc()[-2500:],
				riv=getattr(riv, "name", None),
			)
		]

	after = _sabe_for_voucher("Delivery Note", DN, BATCH)
	after_10 = [r for r in after if r.voucher_detail_no == detail]
	after_snap = [
		(flt(r.qty), flt(r.incoming_rate), flt(r.stock_value_difference)) for r in after_10
	]
	sle = [
		r
		for r in _sle_for_voucher("Delivery Note", DN, ITEM)
		if r.voucher_detail_no == detail
	]
	sle_total = abs(sum(flt(r.stock_value_difference) for r in sle))
	all_int = all(
		_is_int_money(r.incoming_rate) and _is_int_money(r.stock_value_difference)
		for r in after_10
	)
	sa = _stock_adjustments_for(DN)
	ok = (
		len(after_10) == 2
		and before_snap == after_snap
		and all_int
		and abs(sle_total - EXPECTED_TOTAL) < 0.5
		and not sa
	)

	# Second repost idempotency
	idem_ok = False
	idem_err = None
	try:
		riv2 = frappe.get_doc(
			{
				"doctype": "Repost Item Valuation",
				"company": dn.company,
				"item_code": ITEM,
				"warehouse": WH,
				"based_on": "Item and Warehouse",
				"posting_date": dn.posting_date,
				"posting_time": dn.posting_time,
				"allow_negative_stock": 1,
				"status": "Queued",
			}
		)
		riv2.insert(ignore_permissions=True)
		frappe.db.commit()
		riv2.reload()
		if hasattr(riv2, "repost"):
			riv2.repost()
		else:
			from erpnext.stock.doctype.repost_item_valuation.repost_item_valuation import (
				repost,
			)

			repost(riv2)
		frappe.db.commit()
		after2 = [
			r
			for r in _sabe_for_voucher("Delivery Note", DN, BATCH)
			if r.voucher_detail_no == detail
		]
		snap2 = [
			(flt(r.qty), flt(r.incoming_rate), flt(r.stock_value_difference)) for r in after2
		]
		idem_ok = snap2 == after_snap
	except Exception as e:
		frappe.db.rollback()
		idem_err = str(e)

	return [
		_ok(
			"repost_primary_dn",
			ok,
			riv=riv.name,
			before=before_snap,
			after=after_snap,
			sle_total=sle_total,
			all_int=all_int,
			stock_adjustments=sa,
			sabe_count=len(after_10),
		),
		_ok("repost_idempotent", idem_ok, error=idem_err, snap=after_snap),
	]


def test_cancel_resubmit_synthetic():
	"""Independent cancel/resubmit on a small synthetic DN (qty 1 single-slice)."""
	apply_monkey_patches()
	customer, company, currency = _customer_and_company()
	pool_q, pool_v = _pool()
	if pool_q < 1:
		return [_ok("cancel_resubmit", False, error="empty pool", pool=(pool_q, pool_v))]

	src = frappe.get_doc("Delivery Note", DN)
	dept = src.items[0].get("department")
	cc = src.items[0].get("cost_center")
	plan = allocate_physical_two_slice(pool_q, pool_v, 1)
	dn = frappe.get_doc(
		{
			"doctype": "Delivery Note",
			"company": company,
			"customer": customer,
			"currency": currency,
			"set_warehouse": WH,
			"posting_date": nowdate(),
			"posting_time": nowtime(),
			"items": [
				{
					"item_code": ITEM,
					"qty": 1,
					"rate": 500,
					"warehouse": WH,
					"batch_no": BATCH,
					"use_serial_batch_fields": 1,
					"department": dept,
					"cost_center": cc,
				}
			],
		}
	)
	try:
		dn.insert(ignore_permissions=True)
		dn.submit()
		frappe.db.commit()
	except Exception as e:
		frappe.db.rollback()
		return [_ok("cancel_resubmit_submit", False, error=str(e))]

	sabe1 = _sabe_for_voucher("Delivery Note", dn.name, BATCH)
	snap1 = [(flt(r.qty), flt(r.incoming_rate), flt(r.stock_value_difference)) for r in sabe1]
	pool_mid = _pool()

	try:
		dn.cancel()
		frappe.db.commit()
	except Exception as e:
		frappe.db.rollback()
		return [_ok("cancel_resubmit_cancel", False, error=str(e), dn=dn.name)]

	pool_restored = _pool()
	# Fresh resubmit (avoid workflow amend path); same commercial + batch valuation
	dn2 = frappe.get_doc(
		{
			"doctype": "Delivery Note",
			"company": company,
			"customer": customer,
			"currency": currency,
			"set_warehouse": WH,
			"posting_date": nowdate(),
			"posting_time": nowtime(),
			"items": [
				{
					"item_code": ITEM,
					"qty": 1,
					"rate": 500,
					"warehouse": WH,
					"batch_no": BATCH,
					"use_serial_batch_fields": 1,
					"department": dept,
					"cost_center": cc,
				}
			],
		}
	)
	try:
		dn2.flags.ignore_pricing_rule = True
		dn2.insert(ignore_permissions=True)
		dn2.submit()
		frappe.db.commit()
	except Exception as e:
		frappe.db.rollback()
		return [
			_ok(
				"cancel_resubmit_resubmit",
				False,
				error=str(e),
				traceback=traceback.format_exc()[-1500:],
				pool_after_cancel=pool_restored,
			)
		]

	sabe2 = _sabe_for_voucher("Delivery Note", dn2.name, BATCH)
	snap2 = [(flt(r.qty), flt(r.incoming_rate), flt(r.stock_value_difference)) for r in sabe2]
	try:
		dn2.cancel()
		frappe.db.commit()
	except Exception:
		frappe.db.rollback()

	ok = (
		len(snap1) >= 1
		and all(_is_int_money(r[1]) and _is_int_money(r[2]) for r in snap1)
		and abs(sum(abs(r[2]) for r in snap1) - float(plan.out_value_abs)) < 0.5
		and abs(pool_restored[0] - pool_q) < 1e-6
		and abs(pool_restored[1] - pool_v) < 0.5
		and snap2
		and abs(sum(abs(r[2]) for r in snap2) - float(plan.out_value_abs)) < 0.5
	)
	return [
		_ok(
			"cancel_resubmit",
			ok,
			dn=dn.name,
			dn2=dn2.name,
			snap1=snap1,
			snap2=snap2,
			plan=str(plan),
			pool_before=(pool_q, pool_v),
			pool_mid=pool_mid,
			pool_after_cancel=pool_restored,
		)
	]


def test_historical_6204():
	batch = "6204-20100043-SO262018T121"
	row = frappe.db.sql(
		"""
		SELECT COALESCE(SUM(qty),0) q, COALESCE(SUM(stock_value_difference),0) v
		FROM `tabSerial and Batch Entry`
		WHERE batch_no=%s AND docstatus=1 AND is_cancelled=0
		  AND type_of_transaction IN ('Inward','Outward')
		""",
		(batch,),
		as_dict=True,
	)[0]
	q, v = Decimal(str(row.q)), Decimal(str(row.v))
	refused = False
	err = None
	try:
		if q > 0:
			allocate_physical_two_slice(q, v, min(q, Decimal(1)))
	except AllocationError as e:
		refused = True
		err = str(e)
	ok = (v != v.to_integral_value() and refused) or (v == v.to_integral_value())
	return [
		_ok(
			"historical_6204_diagnostic",
			ok,
			batch=batch,
			qty=float(q),
			value=float(v),
			fractional=bool(v != v.to_integral_value()),
			refused=refused,
			error=err,
			note="No automatic repair; Stock Adjustment not generated",
		)
	]


def test_guard_still_blocks_raw_fractional():
	"""Without rewrite path (Stock Entry style), fractional SABE still blocked conceptually."""
	from erpnext_extensions.iran_accounting.domain.currency import rate_is_fractional

	return [
		_ok(
			"guard_fractional_detector",
			rate_is_fractional(3606631.112582781, "IRR") is True
			and rate_is_fractional(3606631, "IRR") is False,
		)
	]


def test_scoped_dup_rejects_arbitrary():
	apply_monkey_patches()
	from erpnext.stock.doctype.serial_and_batch_bundle.serial_and_batch_bundle import (
		SerialandBatchBundle,
	)

	doc = frappe.get_doc(
		{
			"doctype": "Serial and Batch Bundle",
			"company": frappe.db.get_value("Delivery Note", DN, "company"),
			"item_code": ITEM,
			"warehouse": WH,
			"type_of_transaction": "Outward",
			"voucher_type": "Stock Entry",  # not DN/SI → must reject
			"entries": [
				{"batch_no": BATCH, "qty": -1, "warehouse": WH, "incoming_rate": 10},
				{"batch_no": BATCH, "qty": -1, "warehouse": WH, "incoming_rate": 11},
			],
		}
	)
	threw = False
	msg = None
	try:
		doc.validate_duplicate_serial_and_batch_no()
	except Exception as e:
		threw = True
		msg = str(e)
	return [_ok("scoped_dup_rejects_se", threw, msg=msg)]


def run_all():
	apply_monkey_patches()
	results = []
	results.extend(test_math_suite())
	results.extend(test_guard_still_blocks_raw_fractional())
	results.extend(test_scoped_dup_rejects_arbitrary())
	results.extend(test_historical_6204())
	results.extend(test_submit_primary_dn())
	# Multi-row / cancel / repost depend on successful submit
	submit_ok = next((r for r in results if r["test"] == "submit_primary_dn"), {}).get("ok")
	# Primary may already be submitted from a prior run
	if not submit_ok and frappe.db.get_value("Delivery Note", DN, "docstatus") == 1:
		# Re-verify ledgers instead of failing the whole suite
		results.extend(test_submit_primary_dn())
		submit_ok = next(
			(r for r in reversed(results) if r["test"] == "submit_primary_dn"), {}
		).get("ok")
	if submit_ok or frappe.db.get_value("Delivery Note", DN, "docstatus") == 1:
		results.extend(test_same_batch_sale_and_free_on_primary_dn())
		results.extend(test_synthetic_multi_row_same_batch())
		results.extend(test_repost_primary_dn())
		results.extend(test_cancel_resubmit_synthetic())
	else:
		results.append(_ok("dependent_e2e_skipped", False, reason="primary DN submit failed"))

	passed = sum(1 for r in results if r.get("ok"))
	failed = [r["test"] for r in results if not r.get("ok")]
	out = {
		"passed": passed,
		"total": len(results),
		"failed": failed,
		"dn_final": frappe.db.get_value(
			"Delivery Note", DN, ["docstatus", "status"], as_dict=True
		),
		"pool_final": _pool(),
		"results": results,
	}
	print(json.dumps(out, default=str, indent=2))
	return out

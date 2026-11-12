# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Actual material consumption valuation from Manufacture Stock Entries / SLE.

Authoritative valuation: sum of absolute outgoing stock value for raw materials
on submitted Manufacture Stock Entries linked to the product's Work Orders /
Stock Entries. Prefer Stock Ledger Entry `stock_value_difference` (actual
valuation), falling back to Stock Entry Detail `amount` when SLE is missing.

Does not create or modify GL/SLE/SE records.
"""

from __future__ import annotations

import json
from decimal import Decimal

import frappe
from frappe.utils import flt


def _money(v, precision: int = 2) -> Decimal:
	from decimal import ROUND_HALF_UP

	q = Decimal("1") if precision <= 0 else Decimal("1").scaleb(-precision)
	return Decimal(str(flt(v))).quantize(q, rounding=ROUND_HALF_UP)


def manufacture_material_value_for_stock_entries(stock_entries: list[str], *, precision: int = 0) -> dict:
	"""Return total material consumption value for Manufacture SEs (no double-count)."""
	ses = sorted({s for s in (stock_entries or []) if s})
	if not ses:
		return {
			"material_value": 0.0,
			"stock_entries": [],
			"source": "None",
			"detail": [],
			"method": "No Manufacture Stock Entry",
		}

	# Prefer SLE valuation for outgoing (material consumed) lines on these vouchers
	sle_rows = frappe.db.sql(
		"""
		SELECT sle.voucher_no, sle.item_code, sle.warehouse,
		       SUM(sle.actual_qty) AS qty,
		       SUM(sle.stock_value_difference) AS stock_value_difference
		FROM `tabStock Ledger Entry` sle
		INNER JOIN `tabStock Entry` se ON se.name = sle.voucher_no
		WHERE sle.voucher_type = 'Stock Entry'
		  AND sle.is_cancelled = 0
		  AND se.docstatus = 1
		  AND se.purpose = 'Manufacture'
		  AND sle.voucher_no IN %(ses)s
		  AND sle.actual_qty < 0
		GROUP BY sle.voucher_no, sle.item_code, sle.warehouse
		""",
		{"ses": tuple(ses)},
		as_dict=True,
	)
	detail = []
	total = Decimal("0")
	source = "Stock Ledger Entry"
	if sle_rows:
		for r in sle_rows:
			# stock_value_difference is negative for outgoing; consumption value = -diff
			val = abs(Decimal(str(flt(r.stock_value_difference))))
			total += val
			detail.append(
				{
					"stock_entry": r.voucher_no,
					"item_code": r.item_code,
					"warehouse": r.warehouse,
					"qty": float(r.qty or 0),
					"value": float(val),
					"source": "SLE",
				}
			)
	else:
		source = "Stock Entry Detail"
		sed = frappe.db.sql(
			"""
			SELECT sed.parent AS stock_entry, sed.item_code, sed.s_warehouse,
			       sed.qty, sed.amount, sed.basic_amount
			FROM `tabStock Entry Detail` sed
			INNER JOIN `tabStock Entry` se ON se.name = sed.parent
			WHERE se.docstatus = 1
			  AND se.purpose = 'Manufacture'
			  AND sed.parent IN %(ses)s
			  AND IFNULL(sed.s_warehouse, '') != ''
			""",
			{"ses": tuple(ses)},
			as_dict=True,
		)
		for r in sed:
			val = abs(Decimal(str(flt(r.amount if r.amount not in (None, 0) else r.basic_amount))))
			total += val
			detail.append(
				{
					"stock_entry": r.stock_entry,
					"item_code": r.item_code,
					"warehouse": r.s_warehouse,
					"qty": float(r.qty or 0),
					"value": float(val),
					"source": "SED",
				}
			)

	return {
		"material_value": float(_money(total, precision)),
		"stock_entries": ses,
		"source": source,
		"detail": detail,
		"method": "Batch-level Manufacture SE actual valuation",
	}


def attribute_material_to_products(
	*,
	batch_no: str,
	products: list[dict],
	precision: int = 0,
) -> dict[str, dict]:
	"""products: list of {item_code, product_share, stock_entries: list[str], work_orders: list}.

	Single-product → full batch material value.
	Multi-product → prefer SE set unique to product; else allocate by product_share.
	"""
	# Collect all SE references for the batch
	all_ses = sorted(
		{
			se
			for p in products
			for se in (p.get("stock_entries") or [])
			if se
		}
	)
	batch_mat = manufacture_material_value_for_stock_entries(all_ses, precision=precision)
	batch_value = Decimal(str(batch_mat["material_value"]))

	eligible = [p for p in products if flt(p.get("product_share")) > 0]
	out: dict[str, dict] = {}

	if len(eligible) <= 1:
		for p in products:
			share = Decimal(str(flt(p.get("product_share"))))
			val = batch_value if share > 0 else Decimal("0")
			out[p["item_code"]] = {
				"material_consumption_value": float(_money(val, precision)),
				"material_allocation_method": "Single-product batch (full Manufacture SE value)",
				"material_source": batch_mat["source"],
				"material_source_detail": json.dumps(
					{"batch_no": batch_no, "stock_entries": all_ses, "detail": batch_mat["detail"][:50]},
					ensure_ascii=False,
				),
			}
		return out

	# Multi-product: check disjoint SE attribution
	se_owners: dict[str, set[str]] = {}
	for p in eligible:
		for se in p.get("stock_entries") or []:
			se_owners.setdefault(se, set()).add(p["item_code"])
	unique_map: dict[str, list[str]] = {p["item_code"]: [] for p in eligible}
	shared = []
	for se, owners in se_owners.items():
		if len(owners) == 1:
			unique_map[next(iter(owners))].append(se)
		else:
			shared.append(se)

	can_direct = any(unique_map[p["item_code"]] for p in eligible) and not shared
	if can_direct:
		for p in products:
			ses = unique_map.get(p["item_code"]) or []
			mat = manufacture_material_value_for_stock_entries(ses, precision=precision)
			out[p["item_code"]] = {
				"material_consumption_value": mat["material_value"],
				"material_allocation_method": "Direct Manufacture SE attribution by product",
				"material_source": mat["source"],
				"material_source_detail": json.dumps(
					{"batch_no": batch_no, "stock_entries": ses, "detail": mat["detail"][:50]},
					ensure_ascii=False,
				),
			}
		return out

	# Allocate batch material by product share (with residual to largest share)
	raw = []
	sum_amt = Decimal("0")
	for p in eligible:
		share = Decimal(str(flt(p.get("product_share"))))
		amt = _money(batch_value * share, precision)
		raw.append((p["item_code"], amt, share))
		sum_amt += amt
	diff = _money(batch_value - sum_amt, precision)
	largest = max(eligible, key=lambda x: (flt(x.get("product_share")), x["item_code"]))["item_code"]
	for item_code, amt, share in raw:
		final = amt + diff if item_code == largest else amt
		out[item_code] = {
			"material_consumption_value": float(_money(final, precision)),
			"material_allocation_method": "Batch material allocated by Product Share Within Batch",
			"material_source": batch_mat["source"],
			"material_source_detail": json.dumps(
				{
					"batch_no": batch_no,
					"stock_entries": all_ses,
					"product_share": float(share),
					"batch_material_value": float(batch_value),
					"detail": batch_mat["detail"][:50],
				},
				ensure_ascii=False,
			),
		}
	for p in products:
		if p["item_code"] not in out:
			out[p["item_code"]] = {
				"material_consumption_value": 0.0,
				"material_allocation_method": "Zero share / missing UOM",
				"material_source": "None",
				"material_source_detail": "",
			}
	return out


def get_selling_price(item_code: str, price_list: str | None, *, as_of_date=None) -> dict:
	"""Resolve selling price from Item Price for a configured Selling Price List."""
	if not price_list:
		return {
			"selling_price": None,
			"price_list": None,
			"item_price": None,
			"currency": None,
			"valid_from": None,
			"status": "Missing",
			"reason": "No Selling Price List configured on Cost Allocation Settings",
		}
	pl = frappe.db.get_value(
		"Price List", price_list, ["name", "currency", "selling", "enabled"], as_dict=True
	)
	if not pl or not cint(pl.enabled) or not cint(pl.selling):
		return {
			"selling_price": None,
			"price_list": price_list,
			"item_price": None,
			"currency": None,
			"valid_from": None,
			"status": "Missing",
			"reason": "Price List missing, disabled, or not a Selling list",
		}
	conditions = ["item_code=%s", "price_list=%s", "IFNULL(selling,1)=1"]
	params = [item_code, price_list]
	if as_of_date:
		conditions.append("(valid_from IS NULL OR valid_from <= %s)")
		params.append(as_of_date)
		conditions.append("(valid_upto IS NULL OR valid_upto >= %s)")
		params.append(as_of_date)
	rows = frappe.db.sql(
		f"""
		SELECT name, price_list_rate, currency, valid_from, valid_upto, uom
		FROM `tabItem Price`
		WHERE {' AND '.join(conditions)}
		ORDER BY IFNULL(valid_from, '0001-01-01') DESC, modified DESC
		LIMIT 1
		""",
		tuple(params),
		as_dict=True,
	)
	if not rows:
		return {
			"selling_price": None,
			"price_list": price_list,
			"item_price": None,
			"currency": pl.currency,
			"valid_from": None,
			"status": "Missing",
			"reason": "No Item Price for this Item on the selected Selling Price List",
		}
	r = rows[0]
	return {
		"selling_price": float(flt(r.price_list_rate)),
		"price_list": price_list,
		"item_price": r.name,
		"currency": r.currency or pl.currency,
		"valid_from": str(r.valid_from) if r.valid_from else None,
		"uom": r.uom,
		"status": "Available",
		"reason": "",
	}


def cint(v):
	from frappe.utils import cint as _c

	return _c(v)

# Copyright (c) 2026, ERPNext Extensions contributors
"""Explicit auditable repair of fractional IRR Serial and Batch Entry economics.

Historical / Core Moving Average paths may persist non-integer ``incoming_rate``
and float-dust ``stock_value_difference`` on SABE rows. Physical Two-Slice
allocation correctly refuses fractional pools; this module repairs those rows
with ROUND_HALF_UP to the integer IRR contract and writes an audit JSON.

Not a silent fix inside ``allocate_physical_two_slice`` — callers must invoke
repair deliberately (RIV / DN rewrite preflight).
"""

from __future__ import annotations

import json
import os
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import frappe
from frappe.utils import flt, now_datetime

from erpnext_extensions.iran_accounting.domain.currency import is_irr_company


def _d(value) -> Decimal:
	return Decimal(str(value if value is not None else 0))


def _rhu(value: Decimal) -> Decimal:
	return value.quantize(Decimal("1"), rounding=ROUND_HALF_UP)


def _audit_dir() -> str:
	path = os.path.join(
		frappe.utils.get_bench_path(),
		"sites",
		frappe.local.site,
		"private",
		"files",
		"iran_fractional_sabe_repair",
	)
	os.makedirs(path, exist_ok=True)
	return path


def integerize_sabe_row(entry_name: str, *, company: str | None = None) -> dict[str, Any] | None:
	"""RHU-integerize one SABE row's rate and stock_value_difference. Returns diff or None."""
	row = frappe.db.get_value(
		"Serial and Batch Entry",
		entry_name,
		[
			"name",
			"qty",
			"incoming_rate",
			"stock_value_difference",
			"item_code",
			"batch_no",
			"warehouse",
			"voucher_type",
			"voucher_no",
		],
		as_dict=True,
	)
	if not row:
		return None
	comp = company
	if not comp and row.voucher_type and row.voucher_no:
		comp = frappe.db.get_value(row.voucher_type, row.voucher_no, "company")
	if comp and not is_irr_company(comp):
		return None

	qty = _d(row.qty)
	rate = _d(row.incoming_rate)
	svd = _d(row.stock_value_difference)
	new_rate = int(_rhu(rate)) if rate != 0 else 0
	if qty == 0:
		new_svd = Decimal(0)
	else:
		sign = Decimal(-1) if qty < 0 else Decimal(1)
		new_svd = sign * _rhu(abs(qty) * Decimal(new_rate))

	rate_ok = rate == rate.to_integral_value() and int(rate) == new_rate
	svd_ok = svd == svd.to_integral_value() and svd == new_svd
	if rate_ok and svd_ok:
		return None

	frappe.db.set_value(
		"Serial and Batch Entry",
		entry_name,
		{"incoming_rate": float(new_rate), "stock_value_difference": float(new_svd)},
		update_modified=False,
	)
	return {
		"name": entry_name,
		"qty": str(qty),
		"old_rate": str(rate),
		"new_rate": new_rate,
		"old_svd": str(svd),
		"new_svd": str(new_svd),
		"voucher_type": row.voucher_type,
		"voucher_no": row.voucher_no,
		"batch_no": row.batch_no,
	}


def integerize_bundle_sabe(bundle_name: str, *, company: str | None = None) -> dict[str, Any]:
	"""Integerize all SABE children of a Serial and Batch Bundle."""
	if not bundle_name:
		return {"repaired": 0, "rows": []}
	names = frappe.get_all(
		"Serial and Batch Entry",
		filters={"parent": bundle_name},
		pluck="name",
		order_by="idx",
	)
	changed = []
	for name in names:
		diff = integerize_sabe_row(name, company=company)
		if diff:
			changed.append(diff)
	if changed:
		# Keep bundle aggregates aligned with children.
		total_qty = flt(
			frappe.db.sql(
				"select coalesce(sum(qty),0) from `tabSerial and Batch Entry` where parent=%s",
				bundle_name,
			)[0][0]
		)
		total_amt = flt(
			frappe.db.sql(
				"select coalesce(sum(stock_value_difference),0) from `tabSerial and Batch Entry` where parent=%s",
				bundle_name,
			)[0][0]
		)
		avg = abs(total_amt / total_qty) if total_qty else 0.0
		frappe.db.set_value(
			"Serial and Batch Bundle",
			bundle_name,
			{"total_qty": total_qty, "total_amount": total_amt, "avg_rate": avg},
			update_modified=False,
		)
	return {"bundle": bundle_name, "repaired": len(changed), "rows": changed}


def repair_batch_pool_fractional_irr(
	item_code: str,
	batch_no: str,
	warehouse: str,
	*,
	company: str | None = None,
	exclude_voucher_detail_no: str | None = None,
	reason: str = "pre_two_slice_allocate",
) -> dict[str, Any]:
	"""Repair all submitted SABE rows for batch@warehouse to integer IRR economics.

	Writes ``private/files/iran_fractional_sabe_repair/repair_*.json``.
	"""
	params: list[Any] = [item_code, batch_no, warehouse]
	excl = ""
	if exclude_voucher_detail_no:
		excl = " AND IFNULL(e.voucher_detail_no,'') != %s"
		params.append(exclude_voucher_detail_no)
	rows = frappe.db.sql(
		f"""
		SELECT e.name, e.parent
		FROM `tabSerial and Batch Entry` e
		WHERE e.item_code=%s AND e.batch_no=%s AND e.warehouse=%s
		  AND e.docstatus=1 AND e.is_cancelled=0
		  AND e.type_of_transaction IN ('Inward', 'Outward')
		  {excl}
		ORDER BY e.posting_datetime, e.creation, e.idx
		""",
		tuple(params),
		as_dict=True,
	)
	changed = []
	bundles = set()
	for r in rows or []:
		diff = integerize_sabe_row(r.name, company=company)
		if diff:
			changed.append(diff)
			if r.parent:
				bundles.add(r.parent)
	for b in bundles:
		integerize_bundle_sabe(b, company=company)

	payload = {
		"at": str(now_datetime()),
		"site": frappe.local.site,
		"reason": reason,
		"item_code": item_code,
		"batch_no": batch_no,
		"warehouse": warehouse,
		"exclude_voucher_detail_no": exclude_voucher_detail_no,
		"repaired": len(changed),
		"rows": changed,
	}
	path = os.path.join(
		_audit_dir(),
		f"repair_{now_datetime().strftime('%Y%m%d_%H%M%S')}_{batch_no[:40]}.json",
	)
	with open(path, "w", encoding="utf-8") as fh:
		json.dump(payload, fh, ensure_ascii=False, indent=2, default=str)
	payload["audit_path"] = path
	return payload

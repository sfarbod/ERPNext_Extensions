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


def _riv_context() -> dict[str, Any]:
	"""Best-effort RIV / session identifiers for audit payloads."""
	out: dict[str, Any] = {
		"through_repost_item_valuation": bool(
			frappe.flags.get("through_repost_item_valuation")
		),
	}
	for key in ("iran_active_riv", "current_riv", "riv_name"):
		val = frappe.flags.get(key)
		if val:
			out["riv"] = val
			break
	return out


def assert_bundle_integer_irr(bundle_name: str) -> None:
	"""Fail closed if any SABE child still carries a fractional IRR rate/amount."""
	if not bundle_name:
		return
	rows = frappe.get_all(
		"Serial and Batch Entry",
		filters={"parent": bundle_name},
		fields=["name", "incoming_rate", "stock_value_difference", "qty"],
	)
	for r in rows or []:
		rate = _d(r.incoming_rate)
		svd = _d(r.stock_value_difference)
		if rate != rate.to_integral_value() or svd != svd.to_integral_value():
			frappe.throw(
				f"IRR SABE still fractional after integerize: bundle={bundle_name} "
				f"entry={r.name} rate={rate} svd={svd}",
				title="Historical SABE integerize",
			)


def integerize_sabe_row(entry_name: str, *, company: str | None = None) -> dict[str, Any] | None:
	"""RHU-integerize one SABE row's rate and stock_value_difference. Returns diff or None.

	Idempotent: already-integral rows with matching qty×rate identity are no-ops.
	"""
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
		"item_code": row.item_code,
		"warehouse": row.warehouse,
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
	"""Integerize all SABE children of a Serial and Batch Bundle.

	Does not commit — caller owns the transaction. After mutation, asserts no
	residual fractional IRR rates/amounts remain.
	"""
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
	assert_bundle_integer_irr(bundle_name)
	return {"bundle": bundle_name, "repaired": len(changed), "rows": changed}


def log_riv_short_pool_event(
	*,
	item_code: str,
	batch_no: str,
	warehouse: str,
	pool_qty,
	out_qty,
	voucher_type: str | None,
	voucher_no: str | None,
	bundle: str | None = None,
) -> str:
	"""Persist an explicit audit record for RIV short-pool fallthrough.

	Fallthrough is RIV-only: live submit still raises AllocationError. The
	caller must subsequently integerize Core MA output so fractional rates
	cannot persist.
	"""
	payload = {
		"at": str(now_datetime()),
		"site": frappe.local.site,
		"event": "riv_short_pool_fallthrough",
		"item_code": item_code,
		"batch_no": batch_no,
		"warehouse": warehouse,
		"pool_qty": str(pool_qty),
		"out_qty": str(out_qty),
		"voucher_type": voucher_type,
		"voucher_no": voucher_no,
		"bundle": bundle,
		**_riv_context(),
		"policy": (
			"RIV-only: skip Physical Two-Slice when visible batch pool is short; "
			"Core MA + integerize_bundle_sabe must follow; live submit remains fail-closed."
		),
	}
	path = os.path.join(
		_audit_dir(),
		f"short_pool_{now_datetime().strftime('%Y%m%d_%H%M%S')}_{batch_no[:40]}.json",
	)
	with open(path, "w", encoding="utf-8") as fh:
		json.dump(payload, fh, ensure_ascii=False, indent=2, default=str)
	return path


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

	Uses a DB savepoint so a mid-repair failure rolls back only this repair.
	Does not commit — the caller (RIV / submit) owns the outer transaction.
	Audit JSON is written only after the savepoint release succeeds.
	"""
	savepoint = "iran_sabe_" + frappe.generate_hash(length=8)
	frappe.db.savepoint(savepoint)
	try:
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
		frappe.db.release_savepoint(savepoint)
	except Exception:
		frappe.db.rollback(save_point=savepoint)
		raise

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
		**_riv_context(),
		"committed_by_caller": True,
		"note": "Audit written after savepoint success; outer RIV/submit commits or rolls back.",
	}
	path = os.path.join(
		_audit_dir(),
		f"repair_{now_datetime().strftime('%Y%m%d_%H%M%S')}_{batch_no[:40]}.json",
	)
	with open(path, "w", encoding="utf-8") as fh:
		json.dump(payload, fh, ensure_ascii=False, indent=2, default=str)
	payload["audit_path"] = path
	return payload

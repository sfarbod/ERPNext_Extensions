# Copyright (c) 2026, ERPNext Extensions contributors
"""Replay SLE qty_after / stock_value / SVD / valuation_rate and Bin.

Warehouse-level sequence matches ERPNext 16.34.2 Stock Ledger (SABB qty_after
is warehouse identity, not batch). Aborts when the window already contains
5.2.0 valuation poison — posting-order repair must not mix with that repair.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_EVEN

import frappe
from frappe.utils import cint, flt

from erpnext_extensions.iran_accounting.stock_posting_order import STATUS_VALUATION_POISON
from erpnext_extensions.iran_accounting.stock_posting_order.simulation import D

POISON_RATE = Decimal("1000000000000")  # 1e12
VALUE_EPS = Decimal("0.5")
QTY_EPS = Decimal("0.0001")

# Leftover value at qty 0 is a SYMPTOM of inverted IN/OUT, not independent 5.2.0
# poison. Posting-order replay exists to clear it after chronology is fixed.
INVERSION_ARTIFACT_POISONS = frozenset(
	{
		"qty_after_zero_nonzero_value",
		"qty_zero_nonzero_value",
	}
)


def _q(x) -> Decimal:
	return D(x).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_EVEN)


def sle_poison_reason(row) -> str | None:
	qty = D(_g(row, "actual_qty"))
	after = D(_g(row, "qty_after_transaction"))
	value = D(_g(row, "stock_value"))
	svd = D(_g(row, "stock_value_difference"))
	rate = D(_g(row, "valuation_rate"))
	incoming = D(_g(row, "incoming_rate"))
	if qty > 0 and incoming < 0:
		return "negative_incoming_rate"
	if qty > 0 and svd < -VALUE_EPS:
		return "sign_inverted_incoming_svd"
	if qty < 0 and svd > VALUE_EPS:
		return "sign_inverted_outgoing_svd"
	if abs(after) <= QTY_EPS and abs(value) > 1:
		return "qty_after_zero_nonzero_value"
	if abs(qty) <= QTY_EPS and abs(value) > 1:
		return "qty_zero_nonzero_value"
	if abs(rate) > POISON_RATE:
		return "exploded_rate"
	return None


def replay_series(rows: list, opening_qty=0, opening_value=0) -> list[dict]:
	"""Pure moving-average replay. Does not write."""
	running_qty = D(opening_qty)
	running_value = D(opening_value)
	out = []
	for row in rows:
		qty = D(_g(row, "actual_qty"))
		old_svd = D(_g(row, "stock_value_difference"))
		if qty > 0:
			rate = D(_g(row, "incoming_rate"))
			if rate == 0:
				rate = D(_g(row, "valuation_rate"))
			if rate == 0 and running_qty:
				rate = running_value / running_qty
			stored_svd = D(_g(row, "stock_value_difference"))
			purpose = str(_g(row, "purpose") or "")
			# Manufacture/Repack inbound SVD is the 5.2.0 contract (FG residual).
			# Transfer-in must follow the replayed source outgoing rate.
			if stored_svd > 0 and purpose in ("Manufacture", "Repack"):
				svd = stored_svd
			else:
				svd = qty * rate
			running_qty += qty
			running_value += svd
		else:
			if running_qty and abs(running_qty + qty) <= QTY_EPS:
				svd = -running_value
				running_qty = D(0)
				running_value = D(0)
			else:
				rate = (running_value / running_qty) if running_qty else D(_g(row, "valuation_rate"))
				svd = qty * rate
				running_qty += qty
				running_value += svd
				if abs(running_qty) <= QTY_EPS:
					running_qty = D(0)
		val_rate = (running_value / running_qty) if running_qty else D(0)
		out.append(
			{
				"name": _g(row, "name"),
				"voucher_no": _g(row, "voucher_no"),
				"qty_after_transaction": running_qty,
				"stock_value": running_value,
				"stock_value_difference": svd,
				"valuation_rate": val_rate,
				"old_stock_value_difference": old_svd,
				"svd_changed": abs(svd - old_svd) > VALUE_EPS,
			}
		)
	return out


def window_poison_hit(item_code, warehouse, from_dt, *, ignore_inversion_artifacts: bool = False) -> dict | None:
	"""First hard poison in the warehouse window, with voucher identity.

	``ignore_inversion_artifacts`` skips leftover-at-zero (inverted IN/OUT
	symptom). It does **not** skip ``negative_incoming_rate``: that is stored
	economic poison unless proven otherwise by a pair-local simulation.
	"""
	rows = _fetch_sles(item_code, warehouse, from_dt, before=False)
	prev = _fetch_previous(item_code, warehouse, from_dt)
	candidates = ([prev] if prev else []) + list(rows)
	for row in candidates:
		reason = sle_poison_reason(row)
		if not reason:
			continue
		if ignore_inversion_artifacts and reason in INVERSION_ARTIFACT_POISONS:
			continue
		return {
			"reason": reason,
			"voucher": _g(row, "voucher_no"),
			"sle": _g(row, "name"),
			"item_code": item_code,
			"warehouse": warehouse,
			"actual_qty": _g(row, "actual_qty"),
			"incoming_rate": _g(row, "incoming_rate"),
			"stock_value_difference": _g(row, "stock_value_difference"),
			"stock_value": _g(row, "stock_value"),
			"qty_after_transaction": _g(row, "qty_after_transaction"),
			"posting_datetime": str(_g(row, "posting_datetime") or ""),
			"inversion_artifact": reason in INVERSION_ARTIFACT_POISONS,
		}
	return None


def window_poison_reason(item_code, warehouse, from_dt, *, ignore_inversion_artifacts: bool = False) -> str | None:
	hit = window_poison_hit(
		item_code, warehouse, from_dt, ignore_inversion_artifacts=ignore_inversion_artifacts
	)
	return hit["reason"] if hit else None


def transfer_incoming_rate_from_outgoing(actual_qty, stock_value_difference):
	"""Dest transfer-in rate after the source warehouse has been replayed."""
	qty = D(actual_qty)
	if qty == 0:
		return D(0)
	return abs(D(stock_value_difference) / qty)


def sync_transfer_incoming_rates(voucher_no: str) -> list[dict]:
	"""Copy replayed source outgoing rate onto the same-voucher transfer-in SLE.

	Warehouse replay alone cannot make a Material Transfer value-neutral: the
	destination incoming_rate stays at the pre-repair figure until this copy.
	"""
	if not voucher_no:
		return []
	rows = frappe.db.sql(
		"""
		SELECT name, item_code, warehouse, actual_qty, stock_value_difference,
		       incoming_rate, voucher_detail_no, serial_and_batch_bundle
		FROM `tabStock Ledger Entry`
		WHERE voucher_type='Stock Entry' AND voucher_no=%s AND is_cancelled=0
		""",
		voucher_no,
		as_dict=True,
	)
	outs = [r for r in rows if D(r.actual_qty) < 0]
	ins = [r for r in rows if D(r.actual_qty) > 0]
	changed = []
	for inn in ins:
		src = next((o for o in outs if o.item_code == inn.item_code), None)
		if not src:
			continue
		rate = transfer_incoming_rate_from_outgoing(src.actual_qty, src.stock_value_difference)
		src_svd = abs(D(src.stock_value_difference))
		dst_svd = abs(D(inn.stock_value_difference))
		if abs(src_svd - dst_svd) <= VALUE_EPS and abs(rate - D(inn.incoming_rate)) <= VALUE_EPS:
			continue
		frappe.db.set_value(
			"Stock Ledger Entry",
			inn.name,
			"incoming_rate",
			flt(rate),
			update_modified=False,
		)
		frappe.db.set_value(
			"Stock Ledger Entry",
			src.name,
			"outgoing_rate",
			flt(rate),
			update_modified=False,
		)
		if inn.voucher_detail_no and frappe.db.exists("Stock Entry Detail", inn.voucher_detail_no):
			qty = abs(flt(inn.actual_qty))
			detail_update = {
				"basic_rate": flt(rate),
				"valuation_rate": flt(rate),
				"amount": flt(rate) * qty,
				"basic_amount": flt(rate) * qty,
			}
			frappe.db.set_value(
				"Stock Entry Detail",
				inn.voucher_detail_no,
				detail_update,
				update_modified=False,
			)
		if inn.serial_and_batch_bundle and frappe.db.exists(
			"Serial and Batch Bundle", inn.serial_and_batch_bundle
		):
			frappe.db.set_value(
				"Serial and Batch Bundle",
				inn.serial_and_batch_bundle,
				"avg_rate",
				flt(rate),
				update_modified=False,
			)
		if src.serial_and_batch_bundle and frappe.db.exists(
			"Serial and Batch Bundle", src.serial_and_batch_bundle
		):
			frappe.db.set_value(
				"Serial and Batch Bundle",
				src.serial_and_batch_bundle,
				"avg_rate",
				flt(rate),
				update_modified=False,
			)
		changed.append(
			{
				"voucher": voucher_no,
				"item_code": inn.item_code,
				"sle": inn.name,
				"incoming_rate": flt(rate),
			}
		)
	return changed


def replay_item_warehouse(
	item_code,
	warehouse,
	from_dt,
	*,
	ignore_inversion_artifacts: bool = True,
	write_vouchers: set | None = None,
	allow_unrelated_poison: bool = False,
) -> dict:
	hit = window_poison_hit(
		item_code, warehouse, from_dt, ignore_inversion_artifacts=ignore_inversion_artifacts
	)
	if hit:
		on_write_set = write_vouchers is not None and hit.get("voucher") in write_vouchers
		# Default stays fail-closed. READY_BATCH_SCOPED_REPAIR may skip poison that
		# simulation proved is not MA-relevant and is outside the write set.
		if not allow_unrelated_poison or write_vouchers is None or on_write_set:
			return {
				"ok": False,
				"status": STATUS_VALUATION_POISON,
				"reason": hit.get("reason") or window_poison_reason(
					item_code, warehouse, from_dt, ignore_inversion_artifacts=ignore_inversion_artifacts
				),
				"item_code": item_code,
				"warehouse": warehouse,
			}
	prev = _fetch_previous(item_code, warehouse, from_dt)
	opening_qty = D(prev.qty_after_transaction) if prev else D(0)
	opening_value = D(prev.stock_value) if prev else D(0)
	rows = _fetch_sles(item_code, warehouse, from_dt, before=False)
	series = replay_series(rows, opening_qty, opening_value)
	valuation_changed = False
	touched_vouchers = set()
	value_prec = _sle_value_precision()
	for i, row in enumerate(rows):
		if write_vouchers is not None and row.voucher_no not in write_vouchers:
			continue
		step = series[i]
		if step["svd_changed"]:
			valuation_changed = True
			touched_vouchers.add(row.voucher_no)
		rate = abs(flt(step["stock_value_difference"]) / flt(row.actual_qty)) if flt(row.actual_qty) else 0
		payload = {
			"qty_after_transaction": flt(step["qty_after_transaction"]),
			"stock_value": flt(step["stock_value"], value_prec),
			"stock_value_difference": flt(step["stock_value_difference"], value_prec),
			"valuation_rate": flt(step["valuation_rate"]),
		}
		if flt(row.actual_qty) < 0:
			payload["outgoing_rate"] = rate
			payload["incoming_rate"] = 0
		frappe.db.set_value(
			"Stock Ledger Entry",
			row.name,
			payload,
			update_modified=False,
		)
	_bin_from_last_sle(item_code, warehouse)
	final = _fetch_last(item_code, warehouse)
	return {
		"ok": True,
		"status": "REPLAYED",
		"item_code": item_code,
		"warehouse": warehouse,
		"final_qty": D(final.qty_after_transaction) if final else opening_qty,
		"final_value": D(final.stock_value) if final else opening_value,
		"valuation_changed": valuation_changed,
		"touched_vouchers": sorted(touched_vouchers),
		"rows": len(series),
		"written": len(touched_vouchers) if write_vouchers is not None else len(series),
	}


def _g(row, key, default=None):
	if isinstance(row, dict):
		return row.get(key, default)
	return getattr(row, key, default)


def _fetch_previous(item_code, warehouse, from_dt):
	rows = frappe.db.sql(
		"""
		SELECT name, voucher_no, voucher_type, actual_qty, qty_after_transaction,
		       incoming_rate, valuation_rate, stock_value, stock_value_difference,
		       posting_datetime, creation,
		       (SELECT purpose FROM `tabStock Entry` WHERE name=sle.voucher_no) purpose
		FROM `tabStock Ledger Entry` sle
		WHERE sle.item_code=%s AND sle.warehouse=%s AND sle.is_cancelled=0
		  AND sle.posting_datetime < %s
		ORDER BY sle.posting_datetime DESC, sle.creation DESC
		LIMIT 1
		""",
		(item_code, warehouse, from_dt),
		as_dict=True,
	)
	return rows[0] if rows else None


def _fetch_sles(item_code, warehouse, from_dt, *, before=False):
	op = "<" if before else ">="
	return frappe.db.sql(
		f"""
		SELECT name, voucher_no, voucher_type, actual_qty, qty_after_transaction,
		       incoming_rate, valuation_rate, stock_value, stock_value_difference,
		       posting_datetime, creation,
		       (SELECT purpose FROM `tabStock Entry` WHERE name=sle.voucher_no) purpose
		FROM `tabStock Ledger Entry` sle
		WHERE item_code=%s AND warehouse=%s AND is_cancelled=0
		  AND posting_datetime {op} %s
		ORDER BY posting_datetime, creation
		""",
		(item_code, warehouse, from_dt),
		as_dict=True,
	)


def _fetch_last(item_code, warehouse):
	rows = frappe.db.sql(
		"""
		SELECT qty_after_transaction, stock_value, valuation_rate
		FROM `tabStock Ledger Entry`
		WHERE item_code=%s AND warehouse=%s AND is_cancelled=0
		ORDER BY posting_datetime DESC, creation DESC
		LIMIT 1
		""",
		(item_code, warehouse),
		as_dict=True,
	)
	return rows[0] if rows else None


def _sle_value_precision() -> int:
	try:
		from frappe.model.meta import get_field_precision

		return cint(get_field_precision(frappe.get_meta("Stock Ledger Entry").get_field("stock_value")))
	except Exception:
		return 2


def _bin_from_last_sle(item_code, warehouse) -> None:
	last = _fetch_last(item_code, warehouse)
	if not last:
		return
	_update_bin(item_code, warehouse, last.qty_after_transaction, last.stock_value, last.valuation_rate)


def _update_bin(item_code, warehouse, qty, value, rate) -> None:
	if not frappe.db.exists("Bin", {"item_code": item_code, "warehouse": warehouse}):
		return
	frappe.db.sql(
		"""
		UPDATE `tabBin`
		SET actual_qty=%s, stock_value=%s, valuation_rate=%s
		WHERE item_code=%s AND warehouse=%s
		""",
		(flt(qty), flt(value), flt(rate), item_code, warehouse),
	)

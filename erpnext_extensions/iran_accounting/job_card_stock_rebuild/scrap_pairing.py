# Copyright (c) 2026, ERPNext Extensions contributors
"""Pair Manufacture WIP CONSUME rows with COMPONENT_SCRAP outputs.

Physical WIP is drained once by CONSUME. Scrap output is classification /
destination evidence — never a second Golden-Rule WIP deduction.
"""

from __future__ import annotations

from collections import defaultdict

from frappe.utils import flt


def _is_component_scrap_output(row: dict) -> bool:
	out_class = (row.get("custom_output_class") or "").strip()
	if out_class == "COMPONENT_SCRAP":
		return True
	if out_class == "MAIN_PRODUCT_REJECT":
		return False
	sec = (row.get("secondary_item_type") or "").strip()
	return bool(row.get("t_warehouse") and (sec == "Scrap" or row.get("is_scrap_item")))


def pair_scrap_on_manufacture(detail_rows: list[dict]) -> dict:
	"""Pair consume↔scrap on one Manufacture voucher."""
	consume_pool: dict[tuple[str, str], float] = defaultdict(float)
	scrap_rows: list[dict] = []
	for row in detail_rows:
		item = row.get("item_code")
		batch = row.get("batch_no") or ""
		qty = flt(row.get("transfer_qty") or row.get("qty"))
		if qty <= 0 or not item:
			continue
		s_wh = row.get("s_warehouse")
		t_wh = row.get("t_warehouse")
		if s_wh and not t_wh:
			consume_pool[(item, batch)] += qty
		elif _is_component_scrap_output(row):
			scrap_rows.append(row)

	paired: dict[tuple[str, str], float] = defaultdict(float)
	unpaired: list[dict] = []
	pool = dict(consume_pool)
	for row in scrap_rows:
		item = row.get("item_code")
		batch = row.get("batch_no") or ""
		qty = flt(row.get("transfer_qty") or row.get("qty"))
		key = (item, batch)
		avail = flt(pool.get(key))
		if avail + 1e-9 >= qty:
			pool[key] = avail - qty
			paired[key] += qty
		else:
			unpaired.append(
				{
					"item_code": item,
					"batch_no": batch,
					"qty": qty,
					"detail_name": row.get("name"),
					"available_consume": avail,
				}
			)
	return {
		"paired_scrap_qty": dict(paired),
		"unpaired_scrap": unpaired,
		"ok": not unpaired,
	}


def pair_scrap_for_job_card_evidence(movements: list[dict]) -> dict:
	"""Aggregate pairing across all Manufacture movements for a Job Card."""
	by_voucher: dict[str, list[dict]] = defaultdict(list)
	for m in movements:
		if m.get("purpose") == "Manufacture":
			by_voucher[m["voucher"]].append(m)

	paired_total: dict[tuple[str, str], float] = defaultdict(float)
	unpaired_all: list[dict] = []
	for voucher, rows in by_voucher.items():
		# Normalize movement records to detail-like dicts
		details = []
		for r in rows:
			details.append(
				{
					"name": r.get("detail_name"),
					"item_code": r.get("item_code"),
					"batch_no": r.get("batch_no") or "",
					"qty": r.get("qty"),
					"transfer_qty": r.get("qty"),
					"s_warehouse": r.get("s_warehouse"),
					"t_warehouse": r.get("t_warehouse"),
					"custom_output_class": r.get("custom_output_class"),
					"secondary_item_type": r.get("secondary_item_type"),
					"is_scrap_item": 1 if r.get("bucket") in ("COMPONENT_SCRAP", "ORDINARY_SCRAP") else 0,
					"bucket": r.get("bucket"),
				}
			)
		# Prefer bucket classification when present
		consume_pool: dict[tuple[str, str], float] = defaultdict(float)
		scrap_rows = []
		for d in details:
			key = (d["item_code"], d["batch_no"] or "")
			qty = flt(d["qty"])
			bucket = d.get("bucket")
			if bucket == "CONSUME":
				consume_pool[key] += qty
			elif bucket == "COMPONENT_SCRAP":
				scrap_rows.append(d)
		pool = dict(consume_pool)
		for d in scrap_rows:
			key = (d["item_code"], d["batch_no"] or "")
			qty = flt(d["qty"])
			avail = flt(pool.get(key))
			if avail + 1e-9 >= qty:
				pool[key] = avail - qty
				paired_total[key] += qty
			else:
				unpaired_all.append({**d, "voucher": voucher, "available_consume": avail})

	return {
		"paired_scrap_qty": dict(paired_total),
		"unpaired_scrap": unpaired_all,
		"ok": not unpaired_all,
	}


def json_safe_pairing(pairing: dict) -> dict:
	"""Convert tuple keys for API / JSON responses."""
	paired = pairing.get("paired_scrap_qty") or {}
	return {
		**pairing,
		"paired_scrap_qty": {
			(f"{k[0]}|{k[1]}" if isinstance(k, tuple) else str(k)): v for k, v in paired.items()
		},
	}


def golden_remainder(
	issued: float,
	returned: float,
	consumed: float,
	component_scrap: float,
	paired_scrap: float,
	other: float = 0.0,
) -> float:
	"""ISSUED − RETURNED − PHYSICAL_CONSUME − OTHER.

	When scrap is fully paired, PHYSICAL_CONSUME = consumed (scrap-source already inside).
	"""
	scrap = flt(component_scrap)
	paired = flt(paired_scrap)
	phys = flt(consumed)
	if scrap > paired + 1e-9:
		# Caller should treat as SCRAP MISMATCH; do not invent a second drain.
		pass
	return flt(issued) - flt(returned) - phys - flt(other)

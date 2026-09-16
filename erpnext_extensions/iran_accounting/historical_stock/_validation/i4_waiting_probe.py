# Copyright (c) 2026 — I4 waiting promotion analysis (dev)
from __future__ import annotations

import frappe
from frappe.utils import flt, nowdate


def run():
	from erpnext_extensions.iran_accounting.historical_stock.i4_repair import scan_i4_leftover
	from erpnext_extensions.iran_accounting.historical_stock.patient_zero import find_patient_zero_identity

	scan = scan_i4_leftover(company="اسپاد فارمد دارو", from_date="2026-03-21", to_date=nowdate(), limit=5000)
	waiting = [r for r in scan["rows"] if r.get("i4_status") == "WAITING_I4"]
	replay = [r for r in scan["rows"] if r.get("i4_status") == "I4_REPLAY_REQUIRED"]

	def earliest_i4(item, warehouse):
		rows = frappe.db.sql(
			"""
			SELECT name, voucher_no, posting_datetime, qty_after_transaction, stock_value
			FROM `tabStock Ledger Entry`
			WHERE item_code=%s AND warehouse=%s AND is_cancelled=0
			ORDER BY posting_datetime, creation
			""",
			(item, warehouse),
			as_dict=True,
		)
		for r in rows:
			if abs(flt(r.qty_after_transaction)) <= 0.0001 and abs(flt(r.stock_value)) > 1:
				return r
		return None

	promoteable = 0
	for r in waiting:
		e = earliest_i4(r["item"], r["warehouse"])
		is_earliest = bool(e and e.voucher_no == r.get("voucher"))
		pz = find_patient_zero_identity(r["item"], r["warehouse"])
		print(
			"WAIT",
			r.get("voucher"),
			"earliest",
			e.voucher_no if e else None,
			"is_earliest",
			is_earliest,
			"finder",
			(pz or {}).get("voucher_no"),
			(pz or {}).get("reason"),
			"clears",
			r.get("pz_residual_clears_in_sim"),
			"prev_ok",
			r.get("previous_healthy"),
		)
		if is_earliest:
			promoteable += 1
	print("WAITING earliest-remaining", promoteable, "/", len(waiting))

	rep_earliest = 0
	for r in replay:
		e = earliest_i4(r["item"], r["warehouse"])
		is_earliest = bool(e and e.voucher_no == r.get("voucher"))
		if is_earliest:
			rep_earliest += 1
	print("REPLAY_REQUIRED earliest-remaining", rep_earliest, "/", len(replay))
	# unique identities among waiting+replay that are earliest
	idents = set()
	for r in waiting + replay:
		e = earliest_i4(r["item"], r["warehouse"])
		if e and e.voucher_no == r.get("voucher"):
			idents.add((r["item"], r["warehouse"], r.get("voucher")))
	print("unique earliest I4 candidates", len(idents))
	return {"waiting_earliest": promoteable, "replay_earliest": rep_earliest, "candidates": len(idents)}

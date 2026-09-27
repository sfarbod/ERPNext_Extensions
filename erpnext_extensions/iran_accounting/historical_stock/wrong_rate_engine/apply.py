# Copyright (c) 2026, ERPNext Extensions contributors
"""Wrong Rate controlled apply with residual verify + identity replay."""

from __future__ import annotations

from time import perf_counter

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.historical_stock import HISTORICAL_REPAIR_FLAG, RATE_EPS
from erpnext_extensions.iran_accounting.historical_stock.reconstruct import repair_wrong_rate_selected
from erpnext_extensions.iran_accounting.historical_stock.wrong_rate_engine import (
	RATE_REPAIR_COMPLETE,
	READY_WRONG_RATE,
)
from erpnext_extensions.iran_accounting.historical_stock.wrong_rate_engine.classifier import (
	classify_wrong_rate_row,
)
from erpnext_extensions.iran_accounting.stock_posting_order.replay import replay_item_warehouse


def apply_wrong_rate_root(row: dict, *, dry_run=True) -> dict:
	"""Apply one EXACT wrong-rate root; verify outgoing/incoming matches expected."""
	t0 = perf_counter()
	classified = classify_wrong_rate_row(row)
	if classified.get("rate_status") != READY_WRONG_RATE and not (
		classified.get("eligible") and classified.get("confidence") == "EXACT"
	):
		return {
			"ok": False,
			"aborted": True,
			"dry_run": dry_run,
			"reason": f"not READY_WRONG_RATE ({classified.get('rate_status')})",
			"classified": {k: classified.get(k) for k in ("rate_status", "rate_bucket", "expected_source")},
		}

	expected = flt(classified.get("expected_value") or classified.get("proposed_rate"))
	if abs(expected) <= RATE_EPS:
		return {"ok": False, "aborted": True, "dry_run": dry_run, "reason": "expected rate is zero — refuse"}

	# Ensure proposed_rate present for writers
	classified["proposed_rate"] = expected
	classified.setdefault("topic", "WRONG_RATE")
	# Carry planner circular-root election into writers (re-classify alone loses it).
	if str(row.get("dependency") or row.get("blocked_because") or "") == "circular_earliest_root":
		classified["dependency"] = "circular_earliest_root"
		classified["blocked_because"] = "circular_earliest_root"
		pz = row.get("patient_zero") if isinstance(row.get("patient_zero"), dict) else {}
		classified["patient_zero"] = {
			"voucher_no": classified.get("voucher") or row.get("voucher"),
			"posting_datetime": (
				(pz or {}).get("posting_datetime")
				or classified.get("posting_datetime")
				or row.get("posting_datetime")
				or f"{row.get('posting_date') or ''} {row.get('posting_time') or ''}".strip()
			),
			"item_code": classified.get("item") or row.get("item"),
			"warehouse": classified.get("warehouse") or row.get("warehouse"),
			"batch": classified.get("batch") or row.get("batch"),
			"reason": "circular_earliest_root",
		}
		classified["status"] = "RECONSTRUCTABLE"
		classified["eligible"] = True
	# Prefer scan-stamped EXACT expected when present.
	if row.get("proposed_rate") and abs(flt(row.get("proposed_rate"))) > RATE_EPS:
		classified["proposed_rate"] = flt(row.get("proposed_rate"))
		expected = classified["proposed_rate"]

	if dry_run:
		preview = repair_wrong_rate_selected([classified], dry_run=True)
		return {
			"ok": True,
			"dry_run": True,
			"voucher": classified.get("voucher"),
			"item": classified.get("item"),
			"warehouse": classified.get("warehouse"),
			"current": classified.get("current_value"),
			"expected": expected,
			"source": classified.get("expected_source"),
			"surface": classified.get("surface"),
			"preview": {"aborted": preview.get("aborted"), "blocked": preview.get("blocked"), "sle_preview": bool(preview.get("sle_preview"))},
			"elapsed_seconds": round(perf_counter() - t0, 3),
		}

	frappe.flags[HISTORICAL_REPAIR_FLAG] = True
	sp = f"wr_{frappe.generate_hash(length=8)}"
	frappe.db.savepoint(sp)
	try:
		out = repair_wrong_rate_selected([classified], dry_run=False)
		if out.get("aborted") or out.get("blocked"):
			frappe.db.rollback(save_point=sp)
			return {"ok": False, "aborted": True, "dry_run": False, "out": out}

		# Always stamp SE+SLE+SABB for the full voucher/item at expected —
		# multi-row transfers need every detail aligned before any RIV.
		_reassert_expected_on_surface(classified, expected)

		# Explicit SLE rate write when surface=SLE (implied SVD → txn rate)
		if classified.get("surface") == "SLE" or classified.get("sle"):
			_write_sle_expected(classified, expected)

		# Downstream: prefer official ERPNext RIV over custom warehouse replay.
		# Custom replay can rewrite the repaired SLE to a different MA and must
		# not be treated as success unless residual still matches expected.
		replay = {"path": "deferred_official_riv", "ok": True}
		item, wh = classified.get("item"), classified.get("warehouse")
		# For SLE-surface rows, keep an identity replay but re-assert expected.
		if (classified.get("surface") == "SLE" or classified.get("sle")) and item and wh:
			from_dt = classified.get("posting_datetime") or _voucher_dt(classified.get("voucher"))
			if from_dt:
				replay = replay_item_warehouse(
					item,
					wh,
					from_dt,
					ignore_inversion_artifacts=True,
					write_vouchers=None,
					allow_unrelated_poison=False,
				)
				if not replay.get("ok"):
					replay = {**replay, "soft_fail": True}
				_write_sle_expected(classified, expected)
		elif classified.get("surface") == "SE" or classified.get("voucher_detail"):
			# Sync SLE from repaired SE detail; official RIV rebuilds descendants.
			from erpnext_extensions.iran_accounting.historical_stock.valuation_rebuild import (
				sync_sle_from_stock_entry_detail,
			)

			try:
				sync_sle_from_stock_entry_detail(classified.get("voucher"), classified.get("item"))
			except Exception:
				pass
			_reassert_expected_on_surface(classified, expected)

		# Residual verify — after_rate MUST match authoritative expected.
		# Never treat "was zero, now nonzero" as success (that accepted MA drift).
		after_rate = _read_current_rate(classified)
		cleared = abs(after_rate - expected) <= 1.0
		if not cleared:
			_reassert_expected_on_surface(classified, expected)
			after_rate = _read_current_rate(classified)
			cleared = abs(after_rate - expected) <= 1.0
		if cleared and not _svd_residual_ok(classified, expected):
			_write_sle_expected(classified, expected)
			if not _svd_residual_ok(classified, expected):
				frappe.db.rollback(save_point=sp)
				return {
					"ok": False,
					"aborted": True,
					"dry_run": False,
					"reason": "svd_residual_not_cleared",
					"expected": expected,
					"after_rate": after_rate,
				}

		if not cleared:
			frappe.db.rollback(save_point=sp)
			return {
				"ok": False,
				"aborted": True,
				"dry_run": False,
				"reason": "residual_not_cleared",
				"expected": expected,
				"after_rate": after_rate,
			}

		# Selective GL — deferred for SE Wrong Rate until official RIV proves the
		# stock residual is stable. Immediate GL rebuild on a mid-chain identity
		# previously inflated Patient Zero / Wrong Rate READY on Development.
		gl = {"deferred": True, "reason": "await_official_riv"}
		if classified.get("surface") == "SLE" or classified.get("sle"):
			try:
				from erpnext_extensions.iran_accounting.historical_stock.gl_integrity import (
					rebuild_gl_for_voucher,
				)

				gl = rebuild_gl_for_voucher(classified.get("voucher"), dry_run=False)
			except Exception as exc:
				gl = {"skipped": True, "error": str(exc)}

		frappe.db.commit()
		return {
			"ok": True,
			"dry_run": False,
			"voucher": classified.get("voucher"),
			"item": item,
			"warehouse": wh,
			"expected": expected,
			"after_rate": after_rate,
			"cleared": True,
			"status": RATE_REPAIR_COMPLETE,
			"economic_writes": 1,
			"replay": {k: (replay or {}).get(k) for k in ("ok", "status", "written", "touched_vouchers", "reason", "path")},
			"gl": gl,
			"elapsed_seconds": round(perf_counter() - t0, 3),
			"riv": "NOT_INVOKED",
		}
	except Exception as exc:
		frappe.db.rollback(save_point=sp)
		return {"ok": False, "aborted": True, "error": str(exc)}
	finally:
		frappe.flags[HISTORICAL_REPAIR_FLAG] = False


def _reassert_expected_on_surface(row: dict, expected: float) -> None:
	"""Force SE detail and/or SLE back to the planned authoritative rate.

	For multi-row Material Transfers, write *every* SE detail for the
	voucher+item (not only the scanned detail). Batch-wise RIV reads SABB
	rates; SE→SLE→SABB must all agree before official RIV or later rows
	collapse back to zero SVD (28696 canary).
	"""
	rate = abs(expected)
	voucher = row.get("voucher") or row.get("voucher_no")
	item = row.get("item") or row.get("item_code")
	vd = row.get("voucher_detail")

	details = []
	if voucher and item:
		details = frappe.db.sql(
			"""
			SELECT name, qty FROM `tabStock Entry Detail`
			WHERE parent=%s AND item_code=%s
			""",
			(voucher, item),
			as_dict=True,
		)
	elif vd:
		qty = flt(frappe.db.get_value("Stock Entry Detail", vd, "qty") or 0)
		details = [frappe._dict(name=vd, qty=qty)]

	for d in details:
		qty = abs(flt(d.qty))
		frappe.db.set_value(
			"Stock Entry Detail",
			d.name,
			{
				"basic_rate": rate,
				"valuation_rate": rate,
				"amount": rate * qty,
				"basic_amount": rate * qty,
			},
			update_modified=False,
		)

	if row.get("surface") == "SLE" or row.get("sle"):
		_write_sle_expected(row, expected)
	elif voucher and item:
		from erpnext_extensions.iran_accounting.historical_stock.valuation_rebuild import (
			sync_sabb_from_sle,
			sync_sle_from_stock_entry_detail,
		)

		try:
			sync_sle_from_stock_entry_detail(voucher, item)
		except Exception:
			pass
		for sle in frappe.db.sql(
			"""
			SELECT name, actual_qty, incoming_rate, outgoing_rate
			FROM `tabStock Ledger Entry`
			WHERE voucher_no=%s AND item_code=%s AND IFNULL(is_cancelled,0)=0
			""",
			(voucher, item),
			as_dict=True,
		):
			qty = flt(sle.actual_qty)
			cur = flt(sle.outgoing_rate if qty < 0 else sle.incoming_rate)
			if abs(cur - rate) > 1.0:
				_write_sle_expected({**row, "sle": sle.name}, expected)
		try:
			sync_sabb_from_sle(voucher, item)
		except Exception:
			pass


def _write_sle_expected(row: dict, expected: float) -> None:
	"""Write txn rate and preserve/restore SVD from expected when replay zeros it."""
	from erpnext_extensions.iran_accounting.historical_stock.valuation_rebuild import (
		sync_sabb_from_sle,
		write_sle_transaction_rates,
	)

	# Primary: derive from SVD (idempotent with implied_svd) when SVD still healthy
	write_sle_transaction_rates(row.get("voucher"), row.get("item"))
	sle_name = row.get("sle")
	if sle_name:
		sle = frappe.db.get_value(
			"Stock Ledger Entry",
			sle_name,
			["actual_qty", "stock_value_difference", "incoming_rate", "outgoing_rate"],
			as_dict=True,
		)
		if sle:
			qty = flt(sle.actual_qty)
			payload = {}
			if qty < 0:
				payload["outgoing_rate"] = abs(expected)
				payload["incoming_rate"] = 0
				# Restore SVD when missing/zeroed by MA replay (implied_svd authority)
				expected_svd = -abs(expected) * abs(qty)
				if abs(flt(sle.stock_value_difference) - expected_svd) > 0.5:
					payload["stock_value_difference"] = expected_svd
			elif qty > 0:
				payload["incoming_rate"] = abs(expected)
				payload["outgoing_rate"] = 0
				expected_svd = abs(expected) * abs(qty)
				if abs(flt(sle.stock_value_difference) - expected_svd) > 0.5:
					payload["stock_value_difference"] = expected_svd
			if payload:
				frappe.db.set_value("Stock Ledger Entry", sle_name, payload, update_modified=False)
	sync_sabb_from_sle(row.get("voucher"), row.get("item"))


def _svd_residual_ok(row: dict, expected: float) -> bool:
	sle_name = row.get("sle")
	if not sle_name:
		return True
	sle = frappe.db.get_value(
		"Stock Ledger Entry",
		sle_name,
		["actual_qty", "stock_value_difference"],
		as_dict=True,
	)
	if not sle:
		return True
	qty = flt(sle.actual_qty)
	if abs(qty) <= 0.0001:
		return True
	want = (-1 if qty < 0 else 1) * abs(expected) * abs(qty)
	return abs(flt(sle.stock_value_difference) - want) <= 1.0


def _read_current_rate(row: dict) -> float:
	sle_name = row.get("sle")
	if sle_name:
		sle = frappe.db.get_value(
			"Stock Ledger Entry",
			sle_name,
			["actual_qty", "incoming_rate", "outgoing_rate"],
			as_dict=True,
		)
		if sle:
			return flt(sle.outgoing_rate if flt(sle.actual_qty) < 0 else sle.incoming_rate)
	# SE detail
	vd = row.get("voucher_detail")
	if vd:
		return flt(frappe.db.get_value("Stock Entry Detail", vd, "basic_rate") or 0)
	return 0.0


def _voucher_dt(voucher):
	if not voucher:
		return None
	from frappe.utils import get_datetime

	row = frappe.db.get_value("Stock Entry", voucher, ["posting_date", "posting_time"], as_dict=True)
	if not row:
		return None
	return get_datetime(f"{row.posting_date} {row.posting_time or '00:00:00'}")

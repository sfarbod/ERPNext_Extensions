# Copyright (c) 2026, ERPNext Extensions contributors
"""Shortage classifier — opening-state / qty_after alignment (v5.3.0).

Regression for false-positive REAL_STOCK_SHORTAGE when Opening Stock
Reconciliation stores qty_after without matching actual_qty (e.g. item
10510117 / MAT-STE-2026-24520).
"""

from __future__ import annotations

import unittest
from datetime import datetime

from erpnext_extensions.iran_accounting.stock_posting_order.negative_interval import (
	find_negative_intervals,
	scan_series,
)
from erpnext_extensions.iran_accounting.stock_posting_order.optimizer import (
	optimize_group,
	simulate_running,
)
from erpnext_extensions.iran_accounting.stock_posting_order.scanner import _opening_before
from erpnext_extensions.iran_accounting.stock_posting_order.simulation import (
	align_movements_to_qty_after,
	is_authoritative_shortage,
	opening_state_before,
)


def _sle(name, voucher, qty, dt, creation="2026-08-01 10:00:00", **kw):
	qa = kw.pop("qty_after_transaction", None)
	batch = kw.pop("batch_no", "")
	canonical = kw.pop("canonical_batch", batch or "")
	row = {
		"name": name,
		"voucher_no": voucher,
		"voucher_type": kw.pop("voucher_type", "Stock Entry"),
		"actual_qty": qty,
		"posting_datetime": dt,
		"posting_date": str(dt)[:10],
		"creation": creation,
		"item_code": kw.pop("item_code", "10510117"),
		"warehouse": kw.pop("warehouse", "WH-CONSUMABLE"),
		"batch_no": batch,
		"canonical_batch": canonical,
		"serial_and_batch_bundle": kw.pop("serial_and_batch_bundle", None),
		"purpose": kw.pop("purpose", "Material Issue"),
		"company": "TEST-CO",
		"modified": creation,
		"work_order": kw.pop("work_order", None),
		"job_card": kw.pop("job_card", None),
	}
	if qa is not None:
		row["qty_after_transaction"] = qa
	row.update(kw)
	return row


class TestAlignMovementsToQtyAfter(unittest.TestCase):
	def test_opening_reco_zero_actual_qty_patches_to_balance(self):
		reco = _sle(
			"sle-reco",
			"MAT-RECO-OPEN",
			0,
			"2026-03-21 14:04:35",
			voucher_type="Stock Reconciliation",
			purpose="Opening Stock",
			qty_after_transaction=4,
		)
		out = _sle(
			"sle-out",
			"MAT-STE-OUT",
			-1,
			"2026-07-25 23:59:00",
			qty_after_transaction=3,
		)
		aligned = align_movements_to_qty_after([reco, out])
		self.assertEqual(aligned[0]["actual_qty"], 4)
		self.assertTrue(aligned[0].get("_effective_qty_patched"))
		self.assertEqual(aligned[1]["actual_qty"], -1)
		sim = simulate_running(aligned, 0)
		self.assertGreaterEqual(sim["min_qty"], 0)
		self.assertEqual(sim["final_qty"], 3)
		self.assertEqual(sim["series"][-1]["running_qty_after"], 3)


class TestConfirmedHealthyCase(unittest.TestCase):
	"""opening/inbound 4 → outbound 1 → balance 3 must NOT be shortage."""

	def _pair(self):
		reco = _sle(
			"sle-reco",
			"MAT-RECO-2026-02784",
			0,
			"2026-03-21 14:04:35",
			creation="2026-07-01 00:00:06",
			voucher_type="Stock Reconciliation",
			purpose="Opening Stock",
			qty_after_transaction=4,
		)
		out = _sle(
			"sle-out",
			"MAT-STE-2026-24520",
			-1,
			"2026-07-25 23:59:00",
			creation="2026-08-22 18:12:23",
			purpose="Material Issue",
			qty_after_transaction=3,
		)
		return reco, out

	def test_find_negative_intervals_empty(self):
		reco, out = self._pair()
		self.assertEqual(find_negative_intervals([reco, out], 0), [])

	def test_scan_series_no_shortage(self):
		reco, out = self._pair()
		rows = scan_series([reco, out], skip_same_second=True)
		shortage = [r for r in rows if (r.get("optimizer_status") or r.get("status")) == "REAL_STOCK_SHORTAGE"]
		self.assertEqual(shortage, [])
		self.assertEqual(rows, [])

	def test_authoritative_qty_after_gate(self):
		self.assertFalse(is_authoritative_shortage(qty_after=3))
		self.assertTrue(is_authoritative_shortage(qty_after=-1))


class TestTrueShortagePreserved(unittest.TestCase):
	"""opening 4 → outbound 5 → qty_after -1 MUST remain REAL_STOCK_SHORTAGE."""

	def test_true_shortage(self):
		reco = _sle(
			"sle-reco",
			"MAT-RECO-OPEN",
			0,
			"2026-03-21 14:04:35",
			voucher_type="Stock Reconciliation",
			purpose="Opening Stock",
			qty_after_transaction=4,
		)
		out = _sle(
			"sle-out",
			"MAT-STE-OVER",
			-5,
			"2026-07-25 23:59:00",
			purpose="Material Issue",
			qty_after_transaction=-1,
		)
		intervals = find_negative_intervals([reco, out], 0)
		self.assertEqual(len(intervals), 1)
		self.assertEqual(intervals[0]["previous_qty"], 4)
		self.assertEqual(intervals[0]["negative_amount"], -1)
		rows = scan_series([reco, out], skip_same_second=True)
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["optimizer_status"], "REAL_STOCK_SHORTAGE")
		self.assertEqual(rows[0]["qty_before"], "4")
		self.assertEqual(rows[0]["movement_qty"], "-5")
		self.assertEqual(rows[0]["qty_after"], "-1")


class TestWindowedOpeningState(unittest.TestCase):
	"""Scanner window starting after inbound must init from prior qty_after."""

	def test_opening_before_uses_qty_after_not_sum_actual(self):
		reco = _sle(
			"sle-reco",
			"MAT-RECO-OPEN",
			0,
			"2026-03-21 14:04:35",
			voucher_type="Stock Reconciliation",
			purpose="Opening Stock",
			qty_after_transaction=4,
		)
		out = _sle(
			"sle-out",
			"MAT-STE-OUT",
			-1,
			"2026-07-25 23:59:00",
			qty_after_transaction=3,
		)
		state = opening_state_before([reco, out], "2026-07-25 23:59:00", out["creation"])
		self.assertEqual(state["opening_qty"], 4)
		self.assertEqual(state["opening_source_voucher"], "MAT-RECO-OPEN")
		opening = _opening_before([reco, out], datetime(2026, 7, 25, 23, 59, 0), out["creation"])
		self.assertEqual(opening, 4)
		# Same-time optimizer group with only the outbound must not be shortage.
		r = optimize_group(sles=[out], opening=opening, base_t=datetime(2026, 7, 25, 23, 59, 0))
		self.assertEqual(r["status"], "NO_REPAIR_NEEDED")
		self.assertGreaterEqual(r["current"]["min_qty"], 0)


class TestBatchedShortage(unittest.TestCase):
	def test_batch_opening_healthy(self):
		batch = "BATCH-A"
		reco = _sle(
			"sle-reco",
			"MAT-RECO-B",
			0,
			"2026-01-01 10:00:00",
			voucher_type="Stock Reconciliation",
			purpose="Opening Stock",
			qty_after_transaction=4,
			batch_no=batch,
			canonical_batch=batch,
		)
		out = _sle(
			"sle-out",
			"MAT-STE-B-OUT",
			-1,
			"2026-02-01 10:00:00",
			qty_after_transaction=3,
			batch_no=batch,
			canonical_batch=batch,
		)
		self.assertEqual(find_negative_intervals([reco, out], 0), [])
		self.assertEqual(scan_series([reco, out], skip_same_second=True), [])

	def test_batch_true_shortage(self):
		batch = "BATCH-A"
		reco = _sle(
			"sle-reco",
			"MAT-RECO-B",
			0,
			"2026-01-01 10:00:00",
			voucher_type="Stock Reconciliation",
			purpose="Opening Stock",
			qty_after_transaction=4,
			batch_no=batch,
			canonical_batch=batch,
		)
		out = _sle(
			"sle-out",
			"MAT-STE-B-OVER",
			-5,
			"2026-02-01 10:00:00",
			qty_after_transaction=-1,
			batch_no=batch,
			canonical_batch=batch,
		)
		rows = scan_series([reco, out], skip_same_second=True)
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["optimizer_status"], "REAL_STOCK_SHORTAGE")
		self.assertEqual(rows[0]["batch"], batch)


if __name__ == "__main__":
	unittest.main()

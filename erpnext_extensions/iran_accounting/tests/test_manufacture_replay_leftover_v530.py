# Copyright (c) 2026, ERPNext Extensions contributors
"""Regression: Manufacture replay must not seed from leftover-at-zero opening (30300042)."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from erpnext_extensions.iran_accounting.stock_posting_order.replay import replay_series
from erpnext_extensions.iran_accounting.stock_posting_order.simulation import D


def _row(**kw):
	r = MagicMock()
	for k, v in kw.items():
		setattr(r, k, v)
	# also support dict-style for replay_series _g
	r.get = lambda key, default=None, _kw=kw: _kw.get(key, default)
	return r


class TestManufactureReplayLeftoverOpeningV530(unittest.TestCase):
	def test_leftover_zero_qty_opening_makes_neg_valuation_then_inverted_svd(self):
		"""OLD arithmetic: qty≈0 with huge negative stock_value + inbound + outbound."""
		# Opening: qty 0, value -8e9 (I4 leftover) — the poison seed.
		opening_qty = D(0)
		opening_value = D("-8000000000")
		rows = [
			_row(
				name="SLE-IN",
				voucher_no="MFG-1",
				actual_qty=D(2850),
				incoming_rate=D(372526),
				valuation_rate=D(372526),
				stock_value_difference=D("1061699102"),
				purpose="Manufacture",
			),
			_row(
				name="SLE-OUT",
				voucher_no="TR-1",
				actual_qty=D(-2850),
				outgoing_rate=D(372526),
				valuation_rate=D(0),
				stock_value_difference=D("-1061699102"),
				purpose="Material Transfer for Manufacture",
			),
		]
		series = replay_series(rows, opening_qty, opening_value)
		# After inbound: value still deeply negative → neg valuation_rate
		self.assertLess(float(series[0]["valuation_rate"]), 0)
		# Outbound MA rate negative → svd = qty*rate becomes POSITIVE (inverted)
		self.assertGreater(float(series[1]["stock_value_difference"]), 0)

	def test_zeroed_opening_value_when_qty_zero_stays_healthy(self):
		"""NEW policy: when qty≈0, force opening_value=0 before replay."""
		opening_qty = D(0)
		opening_value = D(0)  # corrected
		rows = [
			_row(
				name="SLE-IN",
				voucher_no="MFG-1",
				actual_qty=D(2850),
				incoming_rate=D(372526),
				valuation_rate=D(372526),
				stock_value_difference=D("1061699102"),
				purpose="Manufacture",
			),
			_row(
				name="SLE-OUT",
				voucher_no="TR-1",
				actual_qty=D(-2850),
				outgoing_rate=D(372526),
				valuation_rate=D(0),
				stock_value_difference=D("-1061699102"),
				purpose="Material Transfer for Manufacture",
			),
		]
		series = replay_series(rows, opening_qty, opening_value)
		self.assertGreaterEqual(float(series[0]["valuation_rate"]), 0)
		self.assertLessEqual(float(series[1]["stock_value_difference"]), 0)
		self.assertGreaterEqual(float(series[0]["stock_value"]), 0)

	def test_replay_from_patient_zero_refuses_leftover_opening(self):
		from erpnext_extensions.iran_accounting.historical_stock.replay import (
			replay_from_patient_zero,
		)

		prev = MagicMock(
			voucher_no="STE-PREV",
			qty_after_transaction=0,
			stock_value=-8_000_000_000,
			valuation_rate=-1,
			actual_qty=0,
			stock_value_difference=0,
			incoming_rate=0,
		)
		with (
			patch(
				"erpnext_extensions.iran_accounting.historical_stock.replay._fetch_previous",
				return_value=prev,
			),
			patch(
				"erpnext_extensions.iran_accounting.historical_stock.replay.sle_poison_reason",
				return_value="qty_after_zero_nonzero_value",
			),
		):
			res = replay_from_patient_zero("30300042", "WH-Q", from_dt="2026-05-20 18:03:55")
		self.assertFalse(res.get("ok"))
		self.assertEqual(res.get("reason"), "qty_after_zero_nonzero_value_opening")


if __name__ == "__main__":
	unittest.main()

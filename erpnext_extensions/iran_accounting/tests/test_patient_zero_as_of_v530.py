# Copyright (c) 2026, ERPNext Extensions contributors
"""Patient-zero as-of clipping — later zero inbound must not block earlier roots."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from erpnext_extensions.iran_accounting.historical_stock.patient_zero import find_patient_zero


def _sle(**kw):
	base = dict(
		name="SLE",
		voucher_no="V",
		item_code="I",
		warehouse="W",
		actual_qty=1,
		incoming_rate=0,
		outgoing_rate=0,
		stock_value_difference=0,
		stock_value=0,
		valuation_rate=0,
		qty_after_transaction=1,
		posting_datetime="2026-09-03 12:00:00",
		batch_no=None,
	)
	base.update(kw)
	return SimpleNamespace(**base)


class TestPatientZeroAsOf(unittest.TestCase):
	def test_as_of_ignores_later_zero_inbound(self):
		rows = [
			_sle(
				name="1",
				voucher_no="EARLY",
				actual_qty=10,
				incoming_rate=100,
				valuation_rate=100,
				stock_value_difference=1000,
				stock_value=1000,
				qty_after_transaction=10,
				posting_datetime="2026-09-03 11:00:00",
			),
			_sle(
				name="2",
				voucher_no="ISSUE",
				actual_qty=-5,
				incoming_rate=0,
				outgoing_rate=100,
				valuation_rate=100,
				stock_value_difference=-500,
				stock_value=500,
				qty_after_transaction=5,
				posting_datetime="2026-09-03 12:00:00",
			),
			_sle(
				name="3",
				voucher_no="LATER_ZERO",
				actual_qty=7,
				incoming_rate=0,
				valuation_rate=0,
				stock_value_difference=0,
				stock_value=500,
				qty_after_transaction=12,
				posting_datetime="2026-09-05 12:00:00",
			),
		]
		# Without as_of: first invalid is LATER_ZERO (zero incoming after healthy prev)
		full = find_patient_zero(rows)
		self.assertEqual(full["voucher_no"], "LATER_ZERO")
		# With as_of at ISSUE time: no patient zero yet (EARLY healthy, ISSUE is outbound)
		clipped = find_patient_zero(rows, as_of="2026-09-03 12:00:00")
		self.assertIsNone(clipped)


if __name__ == "__main__":
	unittest.main()

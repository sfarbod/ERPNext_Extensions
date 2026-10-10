# Copyright (c) 2026, ERPNext Extensions contributors
"""5.5.30: dynamic Manufacture RIV status map, zero-rate verify, double-pool guard."""

from __future__ import annotations

import unittest

from erpnext_extensions.iran_accounting.job_card_stock_rebuild import queued_repair as qr
from erpnext_extensions.iran_accounting.job_card_stock_rebuild.valuation_verify import (
	assert_multi_fg_pool_closed,
)


class TestDynamicManufactureRivV5530(unittest.TestCase):
	def test_map_apply_valuation_pending(self):
		mapped = qr._map_final_status(
			{
				"ok": True,
				"committed": True,
				"status": "APPLY_PASS_VALUATION_PENDING",
				"valuation_status": "VALUATION_PENDING",
			},
			qr.MODE_APPLY,
		)
		self.assertEqual(mapped, "COMMITTED_VALUATION_PENDING")

	def test_map_apply_valuation_failed(self):
		mapped = qr._map_final_status(
			{
				"ok": True,
				"committed": True,
				"status": "APPLY_PASS_VALUATION_FAILED",
				"valuation_status": "VALUATION_FAILED",
			},
			qr.MODE_APPLY,
		)
		self.assertEqual(mapped, "COMMITTED_VALUATION_FAILED")

	def test_map_apply_pass_still_committed(self):
		mapped = qr._map_final_status(
			{"ok": True, "committed": True, "status": "APPLY_PASS"},
			qr.MODE_APPLY,
		)
		self.assertEqual(mapped, "COMMITTED")

	def test_map_does_not_treat_pending_as_failed(self):
		"""Regression: valuation-pending Apply must not collapse to FAILED."""
		mapped = qr._map_final_status(
			{"ok": True, "committed": True, "status": "APPLY_PASS_VALUATION_PENDING"},
			qr.MODE_APPLY,
		)
		self.assertNotEqual(mapped, "FAILED")
		self.assertNotEqual(mapped, "COMMITTED")

	def test_zero_fg_rate_allowed_in_pool_assert(self):
		snap = {
			"material_sum": 0.0,
			"fg_sum": 0.0,
			"fg": [{"idx": 1, "basic_rate": 0.0}],
			"gl_debit": 100.0,
			"gl_credit": 100.0,
		}
		self.assertEqual(assert_multi_fg_pool_closed(snap), [])

	def test_nonzero_non_integer_fg_rate_rejected(self):
		snap = {
			"material_sum": 100.0,
			"fg_sum": 100.0,
			"fg": [{"idx": 1, "basic_rate": 10.5}],
			"gl_debit": 100.0,
			"gl_credit": 100.0,
		}
		errs = assert_multi_fg_pool_closed(snap)
		self.assertTrue(any("non-integer" in e for e in errs))

	def test_double_pool_signal(self):
		mat = 1_000_000.0
		snap = {
			"material_sum": mat,
			"fg_sum": mat * 2.0,
			"fg": [{"idx": 1, "basic_rate": 100.0}],
			"gl_debit": mat * 2.0,
			"gl_credit": mat * 2.0,
		}
		errs = assert_multi_fg_pool_closed(snap)
		self.assertTrue(any("double-pool" in e for e in errs))


if __name__ == "__main__":
	unittest.main()

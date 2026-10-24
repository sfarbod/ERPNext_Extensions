# Copyright (c) 2026, ERPNext Extensions contributors
"""Auditable fractional IRR SABE repair + two-slice preflight (v5.6.11)."""

from __future__ import annotations

import unittest
from decimal import Decimal
from unittest import mock

from erpnext_extensions.iran_accounting.domain.historical_fractional_sabe_repair import (
	integerize_sabe_row,
)
from erpnext_extensions.iran_accounting.domain.physical_two_slice import (
	AllocationError,
	allocate_physical_two_slice,
)


class TestFractionalSabeRepairUnitV5611(unittest.TestCase):
	def test_rhu_rate_matches_sed_integer_for_mt_float_ma(self):
		"""MAT-STE-2026-33527 style: Core float rate RHU → SED integer 250654."""
		rate = Decimal("250653.505702364")
		qty = Decimal("32355")
		from erpnext_extensions.iran_accounting.domain.historical_fractional_sabe_repair import (
			_rhu,
		)

		self.assertEqual(int(_rhu(rate)), 250654)
		self.assertEqual(_rhu(qty * Decimal(250654)), Decimal("8109910170"))

	def test_allocate_refuses_fractional_without_repair(self):
		with self.assertRaises(AllocationError):
			allocate_physical_two_slice(10, Decimal("3904078944.0016685"), 1)

	def test_allocate_ok_after_rhu_pool(self):
		plan = allocate_physical_two_slice(10, Decimal("3904078944"), 1)
		self.assertEqual(plan.out_value_abs, Decimal("390407894"))
		self.assertTrue(all(s.rate == int(s.rate) for s in plan.slices))

	def test_integerize_row_noop_when_already_integer(self):
		from types import SimpleNamespace

		with mock.patch(
			"erpnext_extensions.iran_accounting.domain.historical_fractional_sabe_repair.frappe"
		) as f:
			f.db.get_value.return_value = SimpleNamespace(
				name="x",
				qty=10,
				incoming_rate=23209,
				stock_value_difference=232090,
				item_code="13100012",
				batch_no="b",
				warehouse="w",
				voucher_type="Stock Entry",
				voucher_no="SE",
				company="C",
			)
			with mock.patch(
				"erpnext_extensions.iran_accounting.domain.historical_fractional_sabe_repair.is_irr_company",
				return_value=True,
			):
				out = integerize_sabe_row("x", company="C")
			self.assertIsNone(out)
			f.db.set_value.assert_not_called()

	def test_integerize_row_repairs_float_ma(self):
		from types import SimpleNamespace

		with mock.patch(
			"erpnext_extensions.iran_accounting.domain.historical_fractional_sabe_repair.frappe"
		) as f:
			f.db.get_value.return_value = SimpleNamespace(
				name="x",
				qty=32355.0,
				incoming_rate=250653.505702364,
				stock_value_difference=8109894176.999987,
				item_code="20100006",
				batch_no="7060-20100006-MR265001D01",
				warehouse="wh",
				voucher_type="Stock Entry",
				voucher_no="MAT-STE-2026-33527",
				company="C",
			)
			with mock.patch(
				"erpnext_extensions.iran_accounting.domain.historical_fractional_sabe_repair.is_irr_company",
				return_value=True,
			):
				out = integerize_sabe_row("x", company="C")
			self.assertIsNotNone(out)
			self.assertEqual(out["new_rate"], 250654)
			self.assertEqual(out["new_svd"], "8109910170")
			f.db.set_value.assert_called_once()


if __name__ == "__main__":
	unittest.main()

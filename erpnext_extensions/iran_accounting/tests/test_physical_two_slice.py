# Copyright (c) 2026, ERPNext Extensions contributors

from decimal import Decimal
import unittest

from erpnext_extensions.iran_accounting.domain.physical_two_slice import (
	AllocationError,
	allocate_physical_two_slice,
	simulate_sequential,
)


class TestPhysicalTwoSliceAllocator(unittest.TestCase):
	def test_primary_qty_10(self):
		plan = allocate_physical_two_slice(151, 544601298, 10)
		self.assertEqual(plan.out_value_abs, Decimal("36066311"))
		self.assertEqual(len(plan.slices), 2)
		self.assertEqual([int(abs(s.qty)) for s in plan.slices], [9, 1])
		self.assertEqual([s.rate for s in plan.slices], [3606631, 3606632])
		self.assertEqual(
			[int(abs(s.amount)) for s in plan.slices], [32459679, 3606632]
		)

	def test_sequential_full_deplete(self):
		steps = simulate_sequential(151, 544601298, [10, 24, 1, 5, 111])
		total = sum((p.out_value_abs for p, _, _ in steps), Decimal(0))
		self.assertEqual(total, Decimal("544601298"))
		self.assertEqual(steps[-1][1], Decimal(0))
		self.assertEqual(steps[-1][2], Decimal(0))

	def test_fractional_qty_ok(self):
		for q in ("0.25", "1.1", "1.5"):
			plan = allocate_physical_two_slice(151, 544601298, q)
			self.assertEqual(len(plan.slices), 1)
			self.assertEqual(plan.slices[0].rate, int(plan.slices[0].rate))

	def test_historical_fractional_pool_refused(self):
		with self.assertRaises(AllocationError):
			allocate_physical_two_slice(10, Decimal("100.5"), 1)

	def test_hrc00015_negative_pool_decimal_floor(self):
		"""HRC-00015: Decimal // truncates toward 0; true floor is required.

		pool_qty=2, pool_value=-3894157, out_qty=1 previously raised
		``n_high out of range: out_v=-1947079 R=-1947078 oq=1 n=-1``.
		"""
		plan = allocate_physical_two_slice(2, Decimal("-3894157"), 1)
		self.assertEqual(plan.base_rate, -1947079)
		self.assertEqual(plan.out_value_abs, Decimal("1947079"))
		self.assertEqual(len(plan.slices), 1)
		self.assertEqual(plan.slices[0].rate, -1947079)
		self.assertEqual(plan.slices[0].qty, Decimal("-1"))
		self.assertEqual(plan.slices[0].amount, Decimal("1947079"))

	def test_negative_pool_two_slice_r_and_r_plus_1(self):
		# avg = -100.5 → floor -101; out for qty=1 via RHU = -101 → single at -101
		plan = allocate_physical_two_slice(2, Decimal("-201"), 1)
		self.assertEqual(plan.base_rate, -101)
		self.assertEqual(plan.out_value_abs, Decimal("101"))

	def test_decimal_floordiv_is_not_mathematical_floor(self):
		"""Guard: document why we must not use Decimal // for R."""
		from decimal import Decimal as D

		self.assertEqual(D("-3894157") // D("2"), D("-1947078"))  # toward 0
		self.assertEqual((-3894157) // 2, -1947079)  # int floor toward -inf
		from erpnext_extensions.iran_accounting.domain.physical_two_slice import _floor_div

		self.assertEqual(_floor_div(D("-3894157"), D("2")), -1947079)

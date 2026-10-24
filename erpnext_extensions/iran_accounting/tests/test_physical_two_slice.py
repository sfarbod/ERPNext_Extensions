# Copyright (c) 2026, ERPNext Extensions contributors

from decimal import Decimal

import frappe
from frappe.tests.utils import FrappeTestCase

from erpnext_extensions.iran_accounting.domain.physical_two_slice import (
	AllocationError,
	allocate_physical_two_slice,
	simulate_sequential,
)


class TestPhysicalTwoSliceAllocator(FrappeTestCase):
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

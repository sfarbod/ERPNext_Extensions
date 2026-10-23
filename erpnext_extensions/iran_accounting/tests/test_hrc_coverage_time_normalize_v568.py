# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.6.8: Coverage chronology must not lexicographically compare unpadded times.

HRC-00003 left 57 Pending vouchers because RIV posting_time ``7:54:56``
compared greater than SLE ``17:17:48`` as strings (``'7' > '1'``).
"""

from __future__ import annotations

import unittest
from datetime import timedelta

from erpnext_extensions.iran_accounting.historical_rebuild.sequential_rebuild import (
	normalize_posting_time,
	posting_sort_key,
)


class TestHrcCoverageTimeNormalize(unittest.TestCase):
	def test_normalize_pads_single_digit_hour(self):
		self.assertEqual(normalize_posting_time("7:54:56"), "07:54:56")
		self.assertEqual(normalize_posting_time("17:17:48"), "17:17:48")

	def test_normalize_timedelta(self):
		self.assertEqual(normalize_posting_time(timedelta(hours=7, minutes=54, seconds=56)), "07:54:56")

	def test_normalize_fractional_seconds(self):
		self.assertEqual(normalize_posting_time("7:54:56.000000"), "07:54:56")

	def test_morning_riv_before_afternoon_sle(self):
		# Bug: bare string compare made this False
		self.assertLess(
			posting_sort_key("2026-04-04", "7:54:56"),
			posting_sort_key("2026-04-04", "17:17:48"),
		)

	def test_same_moment_equal(self):
		self.assertEqual(
			posting_sort_key("2026-04-04", "07:54:56"),
			posting_sort_key("2026-04-04", "7:54:56"),
		)

	def test_lexicographic_trap_documented(self):
		# Guard: raw string compare is wrong — keep the trap visible in tests.
		self.assertGreater("7:54:56", "17:17:48")
		self.assertLess(normalize_posting_time("7:54:56"), normalize_posting_time("17:17:48"))

# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.6.1 — sequential single-RIV chronological rebuild contracts."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from erpnext_extensions.iran_accounting.domain.historical_valuation_residual import (
	ACTIVE_HISTORICAL_VALUATION_GUARD,
	TRANSITIONAL_NON_BLOCKING,
)
from erpnext_extensions.iran_accounting.historical_rebuild import sequential_rebuild as sr
from erpnext_extensions.iran_accounting.historical_rebuild.integrity_types import (
	COVERED_BY_REPOSTED_AND_VERIFIED,
	DIRECT_REPOSTED_AND_VERIFIED,
	FINAL_SUCCESS_CLASSES,
	VERIFIED_CLEAN,
)


class TestSequentialPolicy(unittest.TestCase):
	def test_max_active_riv_is_one(self):
		self.assertEqual(sr.MAX_ACTIVE_RIV, 1)

	def test_verified_clean_not_final(self):
		self.assertNotIn(VERIFIED_CLEAN, FINAL_SUCCESS_CLASSES)
		self.assertIn(DIRECT_REPOSTED_AND_VERIFIED, FINAL_SUCCESS_CLASSES)
		self.assertIn(COVERED_BY_REPOSTED_AND_VERIFIED, FINAL_SUCCESS_CLASSES)

	def test_execute_uses_continue_reposting_false(self):
		src = Path(sr.__file__).read_text()
		self.assertIn("continue_reposting=False", src)
		self.assertIn("MAX_ACTIVE_RIV = 1", src)
		self.assertIn("_assert_no_active_riv", src)

	def test_assert_blocks_second_active(self):
		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_rebuild.frappe.db.count",
			return_value=1,
		):
			with self.assertRaises(Exception):
				sr._assert_no_active_riv()

	def test_transitional_guard(self):
		self.assertEqual(ACTIVE_HISTORICAL_VALUATION_GUARD, TRANSITIONAL_NON_BLOCKING)

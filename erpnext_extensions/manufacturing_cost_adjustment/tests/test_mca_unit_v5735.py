# Copyright (c) 2026, ERPNext Extensions contributors
"""Unit tests for MCA validation, state, locks, idempotency — v5.7.35."""

from __future__ import annotations

import unittest
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from erpnext_extensions.manufacturing_cost_adjustment import constants as C
from erpnext_extensions.manufacturing_cost_adjustment.locks import (
	acquire_se_lock,
	release_se_lock,
)
from erpnext_extensions.manufacturing_cost_adjustment.repost_engine import (
	_cp_before,
	_is_transient,
	_parse_riv_list,
)


class TestMCAUnitV5735(FrappeTestCase):
	def test_checkpoint_order(self):
		self.assertTrue(_cp_before(C.CP_NONE, C.CP_COSTS_APPENDED))
		self.assertTrue(_cp_before(C.CP_COSTS_APPENDED, C.CP_ALLOCATED))
		self.assertFalse(_cp_before(C.CP_COMPLETED, C.CP_RIV_QUEUED))

	def test_transient_classification(self):
		self.assertTrue(_is_transient("Deadlock found when trying to get lock"))
		self.assertTrue(_is_transient("JobTimeoutException"))
		self.assertFalse(_is_transient("Expense Account is required"))

	def test_parse_riv_list(self):
		self.assertEqual(_parse_riv_list("A\nB\n"), ["A", "B"])
		self.assertEqual(_parse_riv_list(None), [])

	def test_lock_acquire_release(self):
		key_owner = "test-owner"
		se = "TEST-SE-LOCK"
		release_se_lock(se)
		self.assertTrue(acquire_se_lock(se, key_owner))
		self.assertFalse(acquire_se_lock(se, "other"))
		self.assertTrue(acquire_se_lock(se, key_owner))
		release_se_lock(se, key_owner)
		self.assertTrue(acquire_se_lock(se, "other"))
		release_se_lock(se, "other")

	def test_mca_cancel_blocked(self):
		doc = frappe.new_doc(C.MCA_DOCTYPE)
		doc.docstatus = 1
		with self.assertRaises(frappe.ValidationError):
			doc.run_method("on_cancel")

	def test_constants_no_lcv_flag_name_in_engine_import(self):
		import inspect

		from erpnext_extensions.manufacturing_cost_adjustment import repost_engine as re

		src = inspect.getsource(re)
		self.assertNotIn("update_landed_cost", src)
		self.assertNotIn("landed_cost_voucher.py", src)
		# via_landed_cost_voucher must only appear as explicit 0 / False
		self.assertIn('"via_landed_cost_voucher": 0', src)


def run():
	suite = unittest.TestLoader().loadTestsFromTestCase(TestMCAUnitV5735)
	unittest.TextTestRunner(verbosity=2).run(suite)

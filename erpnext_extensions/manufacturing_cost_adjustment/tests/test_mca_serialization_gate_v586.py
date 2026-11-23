# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""MCA company-wide serialization gate — blocks concurrent/unresolved submits."""

from __future__ import annotations

import unittest
from unittest import mock

import frappe

from erpnext_extensions.manufacturing_cost_adjustment import constants as C
from erpnext_extensions.manufacturing_cost_adjustment import serialization as ser


class TestMCASerializationGateV586(unittest.TestCase):
	def test_blocking_statuses_include_failed_and_pending_riv(self):
		self.assertIn(C.PS_FAILED, ser.BLOCKING_PROCESSING_STATUSES)
		self.assertIn(C.PS_PENDING_RIV, ser.BLOCKING_PROCESSING_STATUSES)
		self.assertIn(C.PS_QUEUED, ser.BLOCKING_PROCESSING_STATUSES)
		self.assertNotIn(C.PS_COMPLETED, ser.BLOCKING_PROCESSING_STATUSES)
		self.assertNotIn(C.PS_VOIDED, ser.BLOCKING_PROCESSING_STATUSES)

	def test_gate_clear_when_no_blockers(self):
		with mock.patch.object(ser, "find_blocking_mcas", return_value=[]), mock.patch.object(
			ser, "_acquire_db_lock"
		):
			ser.assert_mca_serialization_gate("Company A")

	def test_gate_blocks_while_prior_pending_riv(self):
		blockers = [
			{
				"mca": "MCA-PRIOR",
				"processing_status": C.PS_PENDING_RIV,
				"stock_entry": "STE-1",
				"checkpoint": "riv_queued",
				"rivs": [{"riv": "riv1", "exists": True, "status": "In Progress"}],
			}
		]
		with mock.patch.object(ser, "find_blocking_mcas", return_value=blockers), mock.patch.object(
			ser, "_acquire_db_lock"
		):
			with self.assertRaises(Exception) as ctx:
				ser.assert_mca_serialization_gate("Company A")
			msg = str(ctx.exception)
			self.assertIn("MCA-PRIOR", msg)
			self.assertIn("Pending RIV", msg)
			self.assertIn("riv1", msg)

	def test_gate_blocks_while_prior_failed(self):
		blockers = [
			{
				"mca": "MCA-FAIL",
				"processing_status": C.PS_FAILED,
				"stock_entry": "STE-2",
				"checkpoint": "allocated",
				"rivs": [{"riv": "rivf", "exists": True, "status": "Failed"}],
			}
		]
		with mock.patch.object(ser, "find_blocking_mcas", return_value=blockers), mock.patch.object(
			ser, "_acquire_db_lock"
		):
			with self.assertRaises(Exception) as ctx:
				ser.assert_mca_serialization_gate("Company A")
			self.assertIn("MCA-FAIL", str(ctx.exception))

	def test_status_probe_reports_architecture(self):
		with mock.patch.object(ser, "find_blocking_mcas", return_value=[]):
			out = ser.get_serialization_status("Company A")
		self.assertTrue(out["gate_clear"])
		self.assertEqual(out["architecture"], "option_a_serialized_transaction_riv")

	def test_gate_blocks_while_queued(self):
		blockers = [
			{
				"mca": "MCA-Q",
				"processing_status": C.PS_QUEUED,
				"stock_entry": "STE-Q",
				"checkpoint": "queued",
				"rivs": [],
			}
		]
		with mock.patch.object(ser, "find_blocking_mcas", return_value=blockers), mock.patch.object(
			ser, "_acquire_db_lock"
		):
			with self.assertRaises(Exception) as ctx:
				ser.assert_mca_serialization_gate("Company A")
			self.assertIn("MCA-Q", str(ctx.exception))

	def test_before_submit_calls_serialization_gate(self):
		import inspect

		from erpnext_extensions.manufacturing_cost_adjustment.doctype.manufacturing_cost_adjustment import (
			manufacturing_cost_adjustment as mca_mod,
		)

		src = inspect.getsource(mca_mod.ManufacturingCostAdjustment.before_submit)
		self.assertIn("assert_mca_serialization_gate", src)
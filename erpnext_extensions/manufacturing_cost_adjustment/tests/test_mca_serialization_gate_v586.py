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

	def test_submit_mca_api_uses_document_before_submit_gate(self):
		"""API submit goes through doc.submit() → before_submit (no docstatus bypass)."""
		import inspect

		from erpnext_extensions.manufacturing_cost_adjustment import api as mca_api

		src = inspect.getsource(mca_api.submit_mca)
		self.assertIn("doc.submit()", src)
		self.assertIn("Does not set docstatus manually", src)
		self.assertNotIn("frappe.db.set_value", src)

	def test_company_named_lock_exclusive_across_db_connections(self):
		"""Essential concurrency: second independent DB connection cannot take mca_serial lock."""
		import pymysql
		from frappe.utils import cint

		company = "MCA-SERIAL-LOCK-AUDIT-CO"
		lock = ser._company_lock_name(company)
		self.assertEqual(lock, f"mca_serial:{company}")

		frappe.db.sql("SELECT RELEASE_LOCK(%s)", lock)
		self.assertEqual(cint(frappe.db.sql("SELECT GET_LOCK(%s, 0)", lock)[0][0]), 1)

		db_host = frappe.conf.db_host or "127.0.0.1"
		db_port = int(frappe.conf.db_port or 3306)
		db_name = frappe.conf.db_name
		db_user = frappe.conf.get("db_user") or db_name
		db_password = frappe.conf.get("db_password")
		conn2 = pymysql.connect(
			host=db_host,
			user=db_user,
			password=db_password,
			database=db_name,
			port=db_port,
		)
		try:
			cur = conn2.cursor()
			cur.execute("SELECT GET_LOCK(%s, 0)", (lock,))
			got2 = cint(cur.fetchone()[0])
			self.assertEqual(
				got2,
				0,
				"second connection must not acquire company serialization lock held by first",
			)
		finally:
			conn2.close()

		frappe.db.sql("SELECT RELEASE_LOCK(%s)", lock)

	def test_lock_released_after_commit_hook_registration(self):
		"""Gate registers RELEASE_LOCK on after_commit/after_rollback (no permanent deadlock)."""
		import inspect

		src = inspect.getsource(ser._acquire_db_lock)
		self.assertIn("RELEASE_LOCK", src)
		self.assertIn("after_commit", src)
		self.assertIn("after_rollback", src)

	def test_completed_not_in_blocking_set(self):
		self.assertNotIn(C.PS_COMPLETED, ser.BLOCKING_PROCESSING_STATUSES)
		self.assertNotIn(C.PS_VOIDED, ser.BLOCKING_PROCESSING_STATUSES)

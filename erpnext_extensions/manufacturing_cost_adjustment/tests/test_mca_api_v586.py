# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""MCA Web Service API surface — generate/submit/status + confirm gates."""

from __future__ import annotations

import unittest
from unittest import mock

import frappe

from erpnext_extensions.manufacturing_cost_adjustment import api as mca_api


class TestMCAApiSurfaceV586(unittest.TestCase):
	def test_status_endpoint_shape(self):
		frappe.set_user("Administrator")
		out = mca_api.get_mca_api_status()
		self.assertTrue(out.get("ok"))
		caps = out.get("capabilities") or {}
		self.assertTrue(caps.get("generate_mcas_from_persian_period"))
		self.assertTrue(caps.get("submit_mca"))
		self.assertTrue(caps.get("get_mca_status"))

	def test_generate_requires_confirm(self):
		frappe.set_user("Administrator")
		with self.assertRaises(Exception) as ctx:
			mca_api.generate_mcas_from_persian_period(persian_period="1405-01", confirm="NOPE")
		self.assertIn("GENERATE_MCA_FROM_PERIOD", str(ctx.exception))

	def test_submit_requires_confirm(self):
		frappe.set_user("Administrator")
		with self.assertRaises(Exception) as ctx:
			mca_api.submit_mca(name="MCA-NOPE", confirm="NOPE")
		self.assertIn("SUBMIT_MCA", str(ctx.exception))

	def test_submit_idempotent_when_already_submitted(self):
		frappe.set_user("Administrator")
		doc = mock.Mock()
		doc.docstatus = 1
		doc.name = "MCA-X"
		doc.processing_status = "Completed"
		doc.check_permission = mock.Mock()
		with mock.patch(
			"erpnext_extensions.manufacturing_cost_adjustment.api.frappe.db.exists",
			return_value=1,
		), mock.patch(
			"erpnext_extensions.manufacturing_cost_adjustment.api.frappe.get_doc",
			return_value=doc,
		), mock.patch(
			"erpnext_extensions.manufacturing_cost_adjustment.api._require_mca_write",
		):
			out = mca_api.submit_mca(name="MCA-X", confirm="SUBMIT_MCA")
		self.assertTrue(out.get("ok"))
		self.assertTrue(out.get("idempotent"))
		doc.submit.assert_not_called()

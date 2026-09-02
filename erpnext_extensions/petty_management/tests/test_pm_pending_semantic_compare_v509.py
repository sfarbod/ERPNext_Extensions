# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.0.9 — semantic pending-save field comparison (Desk type coercion)."""

from __future__ import annotations

import datetime
import unittest

import frappe
from frappe.utils import today

from erpnext_extensions.petty_management.services.draft_approval_guards import (
	normalize_pending_field_value,
	only_remark_changed_while_pending,
	pending_field_values_semantically_equal,
)
from erpnext_extensions.petty_management.tests import test_pm_pending_remark_edit_v506 as base


class TestPendingSemanticCompareV509(unittest.TestCase):
	def test_date_object_vs_string(self):
		d = datetime.date(2026, 9, 2)
		self.assertTrue(pending_field_values_semantically_equal(d, "2026-09-02", "Date"))
		self.assertFalse(pending_field_values_semantically_equal(d, "2026-09-03", "Date"))

	def test_datetime_vs_iso_string(self):
		dt = datetime.datetime(2026, 9, 2, 14, 30, 0)
		self.assertTrue(
			pending_field_values_semantically_equal(dt, "2026-09-02 14:30:00", "Datetime")
		)
		self.assertFalse(
			pending_field_values_semantically_equal(dt, "2026-09-02 15:30:00", "Datetime")
		)

	def test_int_vs_string(self):
		self.assertTrue(pending_field_values_semantically_equal(1, "1", "Int"))
		self.assertFalse(pending_field_values_semantically_equal(1, "2", "Int"))

	def test_float_currency_precision(self):
		self.assertTrue(pending_field_values_semantically_equal(100, 100.0, "Currency"))
		self.assertTrue(pending_field_values_semantically_equal(100, "100.000", "Currency"))
		self.assertFalse(pending_field_values_semantically_equal(100, 101.0, "Currency"))

	def test_check_bool_int_string(self):
		self.assertTrue(pending_field_values_semantically_equal(1, "1", "Check"))
		self.assertTrue(pending_field_values_semantically_equal(True, 1, "Check"))
		self.assertTrue(pending_field_values_semantically_equal(0, False, "Check"))
		self.assertTrue(pending_field_values_semantically_equal(0, "0", "Check"))

	def test_empty_values(self):
		self.assertTrue(pending_field_values_semantically_equal(None, "", "Data"))
		self.assertTrue(pending_field_values_semantically_equal(None, "", "Date"))
		self.assertIsNone(normalize_pending_field_value("", "Date"))
		self.assertIsNone(normalize_pending_field_value(None, "Link"))

	def test_link_select_string_normalization(self):
		self.assertTrue(pending_field_values_semantically_equal("HR-EMP-1", "HR-EMP-1", "Link"))
		self.assertTrue(pending_field_values_semantically_equal("Draft", "Draft", "Select"))

	def test_child_row_currency_normalization(self):
		self.assertTrue(pending_field_values_semantically_equal(1000, 1000.0, "Currency"))
		self.assertFalse(pending_field_values_semantically_equal(1000, 1001, "Currency"))

	def _mock_pending_doc(self, before_data: dict, current_data: dict, doctype: str = "PM Request"):
		before = frappe.get_doc({"doctype": doctype, **before_data})
		current = frappe.get_doc({"doctype": doctype, **current_data})
		current._doc_before_save = before
		return current

	def test_remark_plus_semantic_equal_date(self):
		td = today()
		doc = self._mock_pending_doc(
			{"transaction_date": td, "remark": "old", "details": []},
			{"transaction_date": str(td), "remark": "new", "details": []},
		)
		self.assertTrue(only_remark_changed_while_pending(doc))

	def test_remark_plus_real_date_change_blocked(self):
		td = today()
		doc = self._mock_pending_doc(
			{"transaction_date": td, "remark": "old", "details": []},
			{"transaction_date": "2099-01-01", "remark": "new", "details": []},
		)
		self.assertFalse(only_remark_changed_while_pending(doc))


class TestPMPendingSemanticIntegrationV509(base.TestPMPendingRemarkEditV506):
	def test_clearance_remark_save_desk_date_string_payload(self):
		name = self._make_clearance_pending("Pending Manager Approval")
		doc = frappe.get_doc("PM Clearance", name)
		doc.load_doc_before_save(raise_exception=True)
		doc.remark = "v509 desk date string"
		# Simulate Desk sending transaction_date as string while DB holds date object.
		doc.transaction_date = str(doc.transaction_date)
		doc.save()
		out = doc.reload()
		self.assertEqual(out.remark, "v509 desk date string")

	def test_clearance_blocks_real_transaction_date_change(self):
		name = self._make_clearance_pending("Pending Manager Approval")
		doc = frappe.get_doc("PM Clearance", name)

		def mutate(d):
			d.remark = "blocked date"
			d.transaction_date = "2099-12-31"

		self._assert_blocked(doc, mutate)

	def test_request_remark_save_desk_date_string_payload(self):
		doc = self._pending_request("Pending Manager Approval")
		doc.load_doc_before_save(raise_exception=True)
		doc.remark = "v509 request date string"
		doc.transaction_date = str(doc.transaction_date)
		doc.save()
		out = doc.reload()
		self.assertEqual(out.remark, "v509 request date string")

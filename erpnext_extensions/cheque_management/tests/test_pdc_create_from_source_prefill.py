# Copyright (c) 2026, Farbod Siyahpoosh and contributors
# For license information, please see license.txt

from __future__ import annotations

import unittest
from unittest.mock import patch

import frappe
from frappe.exceptions import ValidationError

import erpnext_extensions.cheque_management.pdc_create_from_source as pdc_src
from erpnext_extensions.cheque_management.pdc_allocation import ALLOCATION_MODE_DIRECT


class _ThrowCtx:
	"""Patch frappe.throw to raise ValidationError in bare unittest."""

	def __enter__(self):
		self._p = patch.object(
			frappe,
			"throw",
			side_effect=lambda msg, *a, **k: (_ for _ in ()).throw(ValidationError(msg)),
		)
		self._p.start()
		return self

	def __exit__(self, exc_type, exc, tb):
		self._p.stop()
		return False


class TestPdcCreateFromSourcePrefill(unittest.TestCase):
	def test_create_from_sales_invoice_prefill_has_positive_amount_and_customer_party(self):
		fake_db = type(
			"DB",
			(),
			{
				"exists": staticmethod(lambda dt, nm: True),
				"get_value": staticmethod(
					lambda dt, nm, field, **kw: (
						1
						if field == "docstatus"
						else "CUST-1"
						if (dt, field) == ("Sales Invoice", "customer")
						else None
					)
				),
			},
		)()
		fake_frappe = type("F", (), {"db": fake_db, "has_permission": staticmethod(lambda *a, **k: True)})()

		with (
			_ThrowCtx(),
			patch.object(pdc_src, "frappe", fake_frappe),
			patch(
				"erpnext_extensions.cheque_management.pdc_create_from_source.get_settlement_summary_for_reference",
				return_value={"company": "_TC", "currency": "INR", "remaining_balance": 250.0},
			),
		):
			out = pdc_src.prepare_post_dated_cheque_prefill_from_source("Sales Invoice", "SINV-1")

		self.assertTrue(out.get("can_create"))
		prefill = out.get("prefill") or {}
		self.assertEqual(prefill.get("allocation_mode"), ALLOCATION_MODE_DIRECT)
		self.assertEqual(prefill.get("party_type"), "Customer")
		self.assertEqual(prefill.get("party"), "CUST-1")
		self.assertEqual(len(prefill.get("allocations") or []), 1)
		row = (prefill.get("allocations") or [None])[0] or {}
		self.assertGreater(float(row.get("amount") or 0), 0)
		self.assertEqual(row.get("party_type"), "Customer")
		self.assertEqual(row.get("party"), "CUST-1")
		self.assertEqual(row.get("company"), "_TC")
		self.assertEqual(row.get("reference_doctype"), "Sales Invoice")
		self.assertEqual(row.get("reference_name"), "SINV-1")

	def test_create_from_purchase_invoice_prefill_has_positive_amount_and_supplier_party(self):
		fake_db = type(
			"DB",
			(),
			{
				"exists": staticmethod(lambda dt, nm: True),
				"get_value": staticmethod(
					lambda dt, nm, field, **kw: (
						1
						if field == "docstatus"
						else "SUP-1"
						if (dt, field) == ("Purchase Invoice", "supplier")
						else None
					)
				),
			},
		)()
		fake_frappe = type("F", (), {"db": fake_db, "has_permission": staticmethod(lambda *a, **k: True)})()

		with (
			_ThrowCtx(),
			patch.object(pdc_src, "frappe", fake_frappe),
			patch(
				"erpnext_extensions.cheque_management.pdc_create_from_source.get_settlement_summary_for_reference",
				return_value={"company": "_TC", "currency": "INR", "remaining_balance": 500.0},
			),
		):
			out = pdc_src.prepare_post_dated_cheque_prefill_from_source("Purchase Invoice", "PINV-1")

		self.assertTrue(out.get("can_create"))
		prefill = out.get("prefill") or {}
		self.assertEqual(prefill.get("allocation_mode"), ALLOCATION_MODE_DIRECT)
		self.assertEqual(prefill.get("party_type"), "Supplier")
		self.assertEqual(prefill.get("party"), "SUP-1")
		self.assertEqual(len(prefill.get("allocations") or []), 1)
		row = (prefill.get("allocations") or [None])[0] or {}
		self.assertGreater(float(row.get("amount") or 0), 0)
		self.assertEqual(row.get("party_type"), "Supplier")
		self.assertEqual(row.get("party"), "SUP-1")
		self.assertEqual(row.get("company"), "_TC")
		self.assertEqual(row.get("reference_doctype"), "Purchase Invoice")
		self.assertEqual(row.get("reference_name"), "PINV-1")

	def test_create_from_payment_request_to_invoice_allocates_to_payment_request(self):
		"""Invoice-based PR: allocate to the PR itself (capacity key); do not unwrap to PI."""

		def _get_value(dt, nm, fields, **kw):
			if dt == "Payment Request" and fields == ["docstatus", "workflow_state"]:
				return {"docstatus": 1, "workflow_state": "Approved"}
			if dt == "Payment Request" and fields == ["payment_request_type", "party_type", "party"]:
				return {"payment_request_type": "Outward", "party_type": "Supplier", "party": "SUP-1"}
			return None

		fake_db = type(
			"DB", (), {"exists": staticmethod(lambda dt, nm: True), "get_value": staticmethod(_get_value)}
		)()
		fake_frappe = type("F", (), {"db": fake_db, "has_permission": staticmethod(lambda *a, **k: True)})()

		with (
			_ThrowCtx(),
			patch.object(pdc_src, "frappe", fake_frappe),
			patch(
				"erpnext_extensions.cheque_management.pdc_create_from_source.get_settlement_summary_for_reference",
				return_value={"company": "_TC", "currency": "INR", "remaining_balance": 300.0},
			),
			patch(
				"erpnext_extensions.cheque_management.pdc_create_from_source.is_payment_request_settlement_eligible",
				return_value=True,
			),
		):
			out = pdc_src.prepare_post_dated_cheque_prefill_from_source("Payment Request", "PR-1")

		self.assertTrue(out.get("can_create"))
		prefill = out.get("prefill") or {}
		self.assertEqual(prefill.get("reference_doctype"), "Payment Request")
		self.assertEqual(prefill.get("reference_name"), "PR-1")
		self.assertEqual(len(prefill.get("allocations") or []), 1)
		row = (prefill.get("allocations") or [None])[0] or {}
		self.assertGreater(float(row.get("amount") or 0), 0)
		self.assertEqual(row.get("reference_doctype"), "Payment Request")
		self.assertEqual(row.get("reference_name"), "PR-1")
		self.assertFalse(row.get("source_doctype"))
		self.assertFalse(row.get("source_name"))

	def test_create_from_payment_request_purchase_order_allows_pdc_without_fake_pi(self):
		"""PO-based Payment Request must be creatable; allocation stays on PR (no fabricated PI)."""

		def _get_value(dt, nm, fields, **kw):
			if dt == "Payment Request" and fields == ["docstatus", "workflow_state"]:
				return {"docstatus": 1, "workflow_state": "Approved"}
			if dt == "Payment Request" and fields == ["payment_request_type", "party_type", "party"]:
				return {"payment_request_type": "Outward", "party_type": "Supplier", "party": "SUP-1"}
			return None

		fake_db = type(
			"DB", (), {"exists": staticmethod(lambda dt, nm: True), "get_value": staticmethod(_get_value)}
		)()
		fake_frappe = type("F", (), {"db": fake_db, "has_permission": staticmethod(lambda *a, **k: True)})()

		with (
			_ThrowCtx(),
			patch.object(pdc_src, "frappe", fake_frappe),
			patch(
				"erpnext_extensions.cheque_management.pdc_create_from_source.get_settlement_summary_for_reference",
				return_value={"company": "_TC", "currency": "IRR", "remaining_balance": 2640000000.0},
			),
			patch(
				"erpnext_extensions.cheque_management.pdc_create_from_source.is_payment_request_settlement_eligible",
				return_value=True,
			),
		):
			out = pdc_src.prepare_post_dated_cheque_prefill_from_source(
				"Payment Request", "ACC-PRQ-2026-00412"
			)

		self.assertTrue(out.get("can_create"), out.get("message"))
		row = ((out.get("prefill") or {}).get("allocations") or [None])[0] or {}
		self.assertEqual(row.get("reference_doctype"), "Payment Request")
		self.assertEqual(row.get("reference_name"), "ACC-PRQ-2026-00412")
		self.assertNotEqual(row.get("reference_doctype"), "Purchase Invoice")

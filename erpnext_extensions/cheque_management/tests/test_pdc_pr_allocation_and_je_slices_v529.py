# Copyright (c) 2026, Farbod Siyahpoosh and contributors
# For license information, please see license.txt

"""v5.2.9: Payment Request → PDC allocation + JE invoice-slice behaviour."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.exceptions import ValidationError

from erpnext_extensions.cheque_management.pdc_payable_purchase_invoice_je_refs import (
	payable_purchase_invoice_settlement_slices,
)
from erpnext_extensions.cheque_management.pdc_receivable_sales_invoice_je_refs import (
	receivable_sales_invoice_settlement_slices,
)


class _ThrowCtx:
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


def _pdc(*, direction: str, amount: float, rows: list[dict]):
	return SimpleNamespace(
		cheque_direction=direction,
		cheque_amount=amount,
		allocations=[SimpleNamespace(**r) for r in rows],
	)


class TestPayablePrJeSlicesV529(unittest.TestCase):
	def test_pr_linked_to_pi_resolves_invoice_slice(self):
		doc = _pdc(
			direction="Payable",
			amount=100.0,
			rows=[
				{
					"amount": 100.0,
					"reference_doctype": "Payment Request",
					"reference_name": "PR-PI",
				}
			],
		)

		def _get_value(dt, nm, fields, **kw):
			if dt == "Payment Request" and nm == "PR-PI":
				return {"reference_doctype": "Purchase Invoice", "reference_name": "PINV-1"}
			return None

		with patch.object(frappe.db, "get_value", side_effect=_get_value), patch.object(
			frappe, "get_precision", return_value=2
		):
			slices = payable_purchase_invoice_settlement_slices(doc)
		self.assertEqual(slices, [("PINV-1", 100.0)])

	def test_pr_linked_to_po_returns_none_no_fake_pi(self):
		doc = _pdc(
			direction="Payable",
			amount=100.0,
			rows=[
				{
					"amount": 100.0,
					"reference_doctype": "Payment Request",
					"reference_name": "PR-PO",
				}
			],
		)

		def _get_value(dt, nm, fields, **kw):
			if dt == "Payment Request" and nm == "PR-PO":
				return {"reference_doctype": "Purchase Order", "reference_name": "PO-1"}
			return None

		with patch.object(frappe.db, "get_value", side_effect=_get_value), patch.object(
			frappe, "get_precision", return_value=2
		):
			slices = payable_purchase_invoice_settlement_slices(doc)
		self.assertIsNone(slices)

	def test_mixed_pi_and_po_pr_raises(self):
		doc = _pdc(
			direction="Payable",
			amount=200.0,
			rows=[
				{"amount": 100.0, "reference_doctype": "Purchase Invoice", "reference_name": "PINV-1"},
				{"amount": 100.0, "reference_doctype": "Payment Request", "reference_name": "PR-PO"},
			],
		)

		def _get_value(dt, nm, fields, **kw):
			if dt == "Payment Request" and nm == "PR-PO":
				return {"reference_doctype": "Purchase Order", "reference_name": "PO-1"}
			return None

		with (
			_ThrowCtx(),
			patch.object(frappe.db, "get_value", side_effect=_get_value),
			patch.object(frappe, "get_precision", return_value=2),
			self.assertRaises(ValidationError),
		):
			payable_purchase_invoice_settlement_slices(doc)


class TestReceivablePrJeSlicesV529(unittest.TestCase):
	def test_pr_linked_to_si_resolves_invoice_slice(self):
		doc = _pdc(
			direction="Receivable",
			amount=50.0,
			rows=[
				{
					"amount": 50.0,
					"reference_doctype": "Payment Request",
					"reference_name": "PR-SI",
				}
			],
		)

		def _get_value(dt, nm, fields, **kw):
			if dt == "Payment Request" and nm == "PR-SI":
				return {"reference_doctype": "Sales Invoice", "reference_name": "SINV-1"}
			return None

		with patch.object(frappe.db, "get_value", side_effect=_get_value), patch.object(
			frappe, "get_precision", return_value=2
		):
			slices = receivable_sales_invoice_settlement_slices(doc)
		self.assertEqual(slices, [("SINV-1", 50.0)])

	def test_pr_linked_to_so_returns_none(self):
		doc = _pdc(
			direction="Receivable",
			amount=50.0,
			rows=[
				{
					"amount": 50.0,
					"reference_doctype": "Payment Request",
					"reference_name": "PR-SO",
				}
			],
		)

		def _get_value(dt, nm, fields, **kw):
			if dt == "Payment Request" and nm == "PR-SO":
				return {"reference_doctype": "Sales Order", "reference_name": "SO-1"}
			return None

		with patch.object(frappe.db, "get_value", side_effect=_get_value), patch.object(
			frappe, "get_precision", return_value=2
		):
			slices = receivable_sales_invoice_settlement_slices(doc)
		self.assertIsNone(slices)


if __name__ == "__main__":
	unittest.main()

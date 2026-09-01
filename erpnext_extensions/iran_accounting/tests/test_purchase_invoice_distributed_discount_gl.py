# Copyright (c) 2026, ERPNext Extensions contributors
"""Site integration: Purchase Invoice distributed-discount GL balance (ACC-PINV-2026-00327)."""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt


class TestPurchaseInvoiceDistributedDiscountGL(FrappeTestCase):
	def test_acc_pinv_2026_00327_validate_gl_balances(self):
		"""Exact live pattern: discount 105 must survive Iran align and keep GL balanced."""
		name = "ACC-PINV-2026-00327"
		if not frappe.db.exists("Purchase Invoice", name):
			self.skipTest(f"{name} not on this site")

		doc = frappe.get_doc("Purchase Invoice", name)
		self.assertEqual(doc.docstatus, 0)
		self.assertEqual(flt(doc.discount_amount), 105.0)

		doc.run_method("validate")
		row = doc.items[0]
		self.assertEqual(flt(row.distributed_discount_amount), 105.0)
		self.assertEqual(flt(row.net_amount), 241500000.0)
		self.assertNotEqual(flt(row.qty) * flt(row.net_rate), flt(row.net_amount))

		gl_entries = doc.get_gl_entries()
		debit = sum(flt(g.get("debit")) for g in gl_entries)
		credit = sum(flt(g.get("credit")) for g in gl_entries)
		self.assertEqual(debit, credit)
		self.assertEqual(debit, 265650000.0)
		frappe.db.rollback()

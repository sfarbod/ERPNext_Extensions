# Copyright (c) 2026, ERPNext Extensions contributors
"""Site integration: distributed-discount alignment must keep invoice GL balanced."""

from __future__ import annotations

import unittest

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.accounts_invoice import round_irr_invoice_totals
from erpnext_extensions.iran_accounting.domain.qty_rate_amount import align_purchase_order_item_amounts


def _gl_sums(doc):
	gl_entries = doc.get_gl_entries()
	debit = sum(flt(g.get("debit")) for g in gl_entries)
	credit = sum(flt(g.get("credit")) for g in gl_entries)
	return gl_entries, debit, credit


class TestPurchaseInvoiceDistributedDiscountGL(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		if not getattr(frappe.local, "site", None):
			raise unittest.SkipTest("frappe site not initialized")
		frappe.set_user("Administrator")

	def test_acc_pinv_2026_00327_validate_before_submit_gl_balanced(self):
		"""Exact live pattern: discount 105 must survive Iran align; GL balanced; no Round Off."""
		name = "ACC-PINV-2026-00327"
		if not frappe.db.exists("Purchase Invoice", name):
			self.skipTest(f"{name} not on this site")

		doc = frappe.get_doc("Purchase Invoice", name)
		if doc.docstatus != 0:
			# Fixture was submitted after 5.0.1 proof; verify posted GL still balanced.
			self.assertEqual(doc.docstatus, 1)
			self.assertEqual(flt(doc.discount_amount), 105.0)
			self.assertEqual(flt(doc.items[0].distributed_discount_amount), 105.0)
			self.assertEqual(flt(doc.items[0].net_amount), 241500000.0)
			gl = frappe.get_all(
				"GL Entry",
				filters={"voucher_type": "Purchase Invoice", "voucher_no": name, "is_cancelled": 0},
				fields=["account", "debit", "credit"],
			)
			debit = sum(flt(g.debit) for g in gl)
			credit = sum(flt(g.credit) for g in gl)
			self.assertEqual(debit, credit)
			self.assertEqual(debit, 265650000.0)
			return

		self.assertEqual(flt(doc.discount_amount), 105.0)

		company = doc.company
		round_off_account = frappe.get_cached_value("Company", company, "round_off_account")
		stock_adj_account = frappe.get_cached_value("Company", company, "stock_adjustment_account")

		doc.run_method("validate")
		row = doc.items[0]
		self.assertEqual(flt(row.distributed_discount_amount), 105.0)
		self.assertEqual(flt(row.net_amount), 241500000.0)
		self.assertEqual(flt(row.base_net_amount), 241500000.0)

		round_irr_invoice_totals(doc, "before_submit")
		self.assertEqual(flt(doc.items[0].net_amount), 241500000.0)

		gl_entries, debit, credit = _gl_sums(doc)
		self.assertEqual(debit, credit)
		self.assertEqual(debit, 265650000.0)
		self.assertTrue(any(flt(g.get("debit")) == 241500000.0 for g in gl_entries))
		self.assertTrue(any(flt(g.get("debit")) == 24150000.0 for g in gl_entries))
		self.assertTrue(any(flt(g.get("credit")) == 265650000.0 for g in gl_entries))

		if round_off_account:
			self.assertFalse(
				any(g.get("account") == round_off_account for g in gl_entries),
				"105 must not post to Round Off",
			)
		if stock_adj_account:
			self.assertFalse(
				any(g.get("account") == stock_adj_account for g in gl_entries),
				"105 must not post to Stock Adjustment",
			)
		frappe.db.rollback()

	def test_true_imbalance_still_fails(self):
		name = "ACC-PINV-2026-00327"
		if not frappe.db.exists("Purchase Invoice", name):
			self.skipTest(f"{name} not on this site")

		from erpnext.accounts.general_ledger import make_gl_entries

		doc = frappe.get_doc("Purchase Invoice", name)
		with self.assertRaises(frappe.ValidationError) as ctx:
			make_gl_entries(
				[
					frappe._dict(
						account=doc.credit_to,
						debit=0,
						credit=100,
						debit_in_account_currency=0,
						credit_in_account_currency=100,
						company=doc.company,
						voucher_type="Purchase Invoice",
						voucher_no=name,
						posting_date=doc.posting_date,
					),
					frappe._dict(
						account=doc.against_expense_account
						or frappe.db.get_value(
							"Purchase Invoice Item", {"parent": name}, "expense_account"
						),
						debit=105,
						credit=0,
						debit_in_account_currency=105,
						credit_in_account_currency=0,
						company=doc.company,
						voucher_type="Purchase Invoice",
						voucher_no=name,
						posting_date=doc.posting_date,
					),
				],
				update_outstanding="No",
			)
		self.assertIn("Debit and Credit not equal", str(ctx.exception))
		frappe.db.rollback()


class TestSalesInvoiceDistributedDiscountAlign(unittest.TestCase):
	def test_sales_invoice_align_preserves_distributed_net(self):
		from types import SimpleNamespace
		from unittest.mock import patch

		from erpnext_extensions.iran_accounting.domain.qty_rate_amount import (
			align_sales_invoice_item_amounts,
		)

		row = SimpleNamespace(
			qty=10,
			rate=100000,
			amount=1000000,
			net_rate=100000,
			net_amount=900000,
			base_rate=100000,
			base_amount=1000000,
			base_net_rate=100000,
			base_net_amount=900000,
			distributed_discount_amount=100000,
		)
		row.get = lambda k, d=None: getattr(row, k, d)
		row.set = lambda k, v: setattr(row, k, v)

		class _Doc:
			doctype = "Sales Invoice"
			company = "x"
			currency = "IRR"

			def get(self, key, default=None):
				return [row] if key == "items" else getattr(self, key, default)

		with patch(
			"erpnext_extensions.iran_accounting.domain.qty_rate_amount.rounding.is_irr_company",
			return_value=True,
		), patch(
			"erpnext_extensions.iran_accounting.domain.qty_rate_amount.rounding.get_company_currency",
			return_value="IRR",
		):
			align_sales_invoice_item_amounts(_Doc())
		self.assertEqual(flt(row.net_amount), 900000)
		self.assertEqual(flt(row.base_net_amount), 900000)


class TestPurchaseOrderDistributedDiscountAlign(unittest.TestCase):
	def test_purchase_order_align_preserves_distributed_net(self):
		from types import SimpleNamespace
		from unittest.mock import patch

		row = SimpleNamespace(
			qty=5,
			rate=2000,
			amount=10000,
			net_rate=2000,
			net_amount=9700,
			base_rate=2000,
			base_amount=10000,
			base_net_rate=2000,
			base_net_amount=9700,
			distributed_discount_amount=300,
		)
		row.get = lambda k, d=None: getattr(row, k, d)
		row.set = lambda k, v: setattr(row, k, v)

		class _Doc:
			doctype = "Purchase Order"
			company = "x"
			currency = "IRR"

			def get(self, key, default=None):
				return [row] if key == "items" else getattr(self, key, default)

		with patch(
			"erpnext_extensions.iran_accounting.domain.qty_rate_amount.rounding.is_irr_company",
			return_value=True,
		), patch(
			"erpnext_extensions.iran_accounting.domain.qty_rate_amount.rounding.get_company_currency",
			return_value="IRR",
		):
			align_purchase_order_item_amounts(_Doc())
		self.assertEqual(flt(row.net_amount), 9700)
		self.assertEqual(flt(row.base_net_amount), 9700)
		self.assertEqual(flt(row.amount), 10000)


class TestDistributedDiscountTaxMatrixAlign(unittest.TestCase):
	def _assert_preserve(self, *, net_amount, distributed, amount, rate=1000, qty=10):
		from types import SimpleNamespace

		from erpnext_extensions.iran_accounting.domain.qty_rate_amount import _align_po_pi_si_row

		row = SimpleNamespace(
			qty=qty,
			rate=rate,
			amount=amount,
			net_rate=rate,
			net_amount=net_amount,
			base_rate=rate,
			base_amount=amount,
			base_net_rate=rate,
			base_net_amount=net_amount,
			distributed_discount_amount=distributed,
		)
		row.get = lambda k, d=None: getattr(row, k, d)
		row.set = lambda k, v: setattr(row, k, v)
		_align_po_pi_si_row(row, "IRR", "IRR")
		self.assertEqual(flt(row.net_amount), net_amount)
		self.assertEqual(flt(row.base_net_amount), net_amount)

	def test_exclusive_tax_pattern(self):
		self._assert_preserve(net_amount=9000, distributed=1000, amount=10000)

	def test_inclusive_tax_pattern(self):
		self._assert_preserve(net_amount=8182, distributed=100, amount=9000, rate=900)

	def test_no_tax_with_distributed_discount(self):
		self._assert_preserve(net_amount=9500, distributed=500, amount=10000)

	def test_valuation_tax_irrelevant_to_net_preserve(self):
		self._assert_preserve(net_amount=9900, distributed=100, amount=10000)

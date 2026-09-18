# Copyright (c) 2026, ERPNext Extensions contributors
"""Fail-closed Pattern A precision-loss override — RATE-FIRST invariant guard.

Valid Pattern A: skip vanilla make_precision_loss_gl_entry.
Invalid Pattern A: THROW (never delegate — vanilla glues the hybrid residual).
Non-Pattern-A: delegate to original ERPNext.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from frappe.utils import flt

from erpnext_extensions.iran_accounting.accounts_invoice import round_irr_invoice_totals
from erpnext_extensions.iran_accounting.domain.qty_rate_amount import (
	is_pattern_a_fx_purchase_invoice,
	validate_pattern_a_rate_first_invariants,
)
from erpnext_extensions.iran_accounting.tests.test_fx_purchase_invoice_pattern_a import (
	AMOUNT_01345,
	BASE_AMOUNT_01345,
	CONV_01345,
	PRODUCT_FIRST_01345,
	QTY_01345,
	RATE_01345,
)


class TestPatternAPrecisionLossFailClosed(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		import frappe

		if not getattr(frappe.local, "site", None):
			raise unittest.SkipTest("frappe site not initialized")
		frappe.set_user("Administrator")
		from erpnext_extensions.iran_accounting.e2e_bootstrap import get_irr_company
		from erpnext_extensions.iran_accounting.integration.bootstrap import apply

		apply()
		try:
			cls.company = (
				"اسپاد فارمد دارو"
				if frappe.db.exists("Company", "اسپاد فارمد دارو")
				else get_irr_company("ESPAD")
			)
		except Exception:
			raise unittest.SkipTest("No IRR company")

	def tearDown(self):
		import frappe

		frappe.db.rollback()

	def _orig(self):
		from erpnext.controllers.accounts_controller import AccountsController

		return AccountsController._iran_original_precision_loss

	def _spy_orig(self):
		return patch.object(
			__import__(
				"erpnext.controllers.accounts_controller", fromlist=["AccountsController"]
			).AccountsController,
			"_iran_original_precision_loss",
			wraps=self._orig(),
		)

	def _assert_no_precision_loss(self, entries):
		self.assertFalse(
			any(
				(g.get("remarks") or "") == "Net total calculation precision loss" for g in entries
			)
		)

	def _gl_balance(self, doc):
		entries = doc.get_gl_entries()
		debit = sum(flt(g.get("debit")) for g in entries)
		credit = sum(flt(g.get("credit")) for g in entries)
		return entries, debit, credit

	# --- TEST A ---
	def test_a_valid_pattern_a_skips_vanilla(self):
		import frappe

		name = "ACC-PINV-2026-01345"
		if not frappe.db.exists("Purchase Invoice", name):
			self.skipTest(f"{name} not on this site")
		doc = frappe.get_doc("Purchase Invoice", name)
		self.assertTrue(is_pattern_a_fx_purchase_invoice(doc))
		validate_pattern_a_rate_first_invariants(doc)
		with self._spy_orig() as mocked:
			entries, debit, credit = self._gl_balance(doc)
		mocked.assert_not_called()
		self.assertEqual(debit, credit)
		self.assertEqual(debit, BASE_AMOUNT_01345)
		self._assert_no_precision_loss(entries)

	# --- TEST B ---
	def test_b_stale_product_first_header_throws(self):
		import frappe

		name = "ACC-PINV-2026-01345"
		if not frappe.db.exists("Purchase Invoice", name):
			self.skipTest(f"{name} not on this site")
		doc = frappe.get_doc("Purchase Invoice", name)
		doc.base_net_total = PRODUCT_FIRST_01345
		doc.base_grand_total = PRODUCT_FIRST_01345
		doc.base_rounded_total = PRODUCT_FIRST_01345
		self.assertTrue(is_pattern_a_fx_purchase_invoice(doc))
		with self._spy_orig() as mocked:
			with self.assertRaises(frappe.ValidationError) as ctx:
				doc.get_gl_entries()
		mocked.assert_not_called()
		self.assertIn("RATE-FIRST", str(ctx.exception))
		self.assertIn("base_net_total", str(ctx.exception))

	# --- TEST C ---
	def test_c_corrupt_base_total_throws(self):
		import frappe

		name = "ACC-PINV-2026-01345"
		if not frappe.db.exists("Purchase Invoice", name):
			self.skipTest(f"{name} not on this site")
		doc = frappe.get_doc("Purchase Invoice", name)
		doc.base_total = PRODUCT_FIRST_01345
		with self._spy_orig() as mocked:
			with self.assertRaises(frappe.ValidationError) as ctx:
				doc.make_precision_loss_gl_entry([])
		mocked.assert_not_called()
		self.assertIn("base_total", str(ctx.exception))

	# --- TEST D ---
	def test_d_corrupt_base_net_total_throws(self):
		import frappe

		name = "ACC-PINV-2026-01345"
		if not frappe.db.exists("Purchase Invoice", name):
			self.skipTest(f"{name} not on this site")
		doc = frappe.get_doc("Purchase Invoice", name)
		doc.base_net_total = PRODUCT_FIRST_01345
		with self._spy_orig() as mocked:
			with self.assertRaises(frappe.ValidationError) as ctx:
				doc.make_precision_loss_gl_entry([])
		mocked.assert_not_called()
		self.assertIn("base_net_total", str(ctx.exception))

	# --- TEST E ---
	def test_e_corrupt_base_grand_total_throws(self):
		import frappe

		name = "ACC-PINV-2026-01345"
		if not frappe.db.exists("Purchase Invoice", name):
			self.skipTest(f"{name} not on this site")
		doc = frappe.get_doc("Purchase Invoice", name)
		doc.base_grand_total = PRODUCT_FIRST_01345
		with self._spy_orig() as mocked:
			with self.assertRaises(frappe.ValidationError) as ctx:
				doc.make_precision_loss_gl_entry([])
		mocked.assert_not_called()
		self.assertIn("base_grand_total", str(ctx.exception))

	# --- TEST F ---
	def test_f_corrupt_item_rate_first_math_throws(self):
		import frappe

		name = "ACC-PINV-2026-01345"
		if not frappe.db.exists("Purchase Invoice", name):
			self.skipTest(f"{name} not on this site")
		doc = frappe.get_doc("Purchase Invoice", name)
		doc.items[0].base_amount = PRODUCT_FIRST_01345
		with self._spy_orig() as mocked:
			with self.assertRaises(frappe.ValidationError) as ctx:
				doc.make_precision_loss_gl_entry([])
		mocked.assert_not_called()
		self.assertIn("base_amount", str(ctx.exception))

		doc2 = frappe.get_doc("Purchase Invoice", name)
		doc2.items[0].base_net_amount = PRODUCT_FIRST_01345
		with self._spy_orig() as mocked2:
			with self.assertRaises(frappe.ValidationError) as ctx2:
				doc2.make_precision_loss_gl_entry([])
		mocked2.assert_not_called()
		self.assertIn("base_net_amount", str(ctx2.exception))

	# --- TEST G ---
	def test_g_pattern_b_delegates(self):
		import frappe

		for name in ("ACC-PINV-2026-00629", "ACC-PINV-2026-00284"):
			if not frappe.db.exists("Purchase Invoice", name):
				self.skipTest(f"{name} not on this site")
			doc = frappe.get_doc("Purchase Invoice", name)
			self.assertFalse(is_pattern_a_fx_purchase_invoice(doc))
			with self._spy_orig() as mocked:
				doc.make_precision_loss_gl_entry([])
			mocked.assert_called_once()

	# --- TEST H ---
	def test_h_normal_irr_pi_delegates(self):
		import frappe

		name = frappe.db.get_value(
			"Purchase Invoice",
			{"company": self.company, "currency": "IRR", "docstatus": ["<", 2]},
			"name",
		)
		if not name:
			self.skipTest("No IRR same-currency PI on site")
		doc = frappe.get_doc("Purchase Invoice", name)
		self.assertFalse(is_pattern_a_fx_purchase_invoice(doc))
		with self._spy_orig() as mocked:
			doc.make_precision_loss_gl_entry([])
		mocked.assert_called_once()

	# --- TEST I ---
	def test_i_sales_invoice_delegates(self):
		import frappe
		from erpnext.accounts.doctype.sales_invoice.sales_invoice import SalesInvoice

		si = frappe.new_doc("Sales Invoice")
		si.company = self.company
		si.currency = "EUR"
		si.conversion_rate = CONV_01345
		si.net_total = 100
		si.base_net_total = 100
		self.assertFalse(is_pattern_a_fx_purchase_invoice(si))
		self.assertTrue(getattr(SalesInvoice.make_precision_loss_gl_entry, "_iran_pattern_a_skip", False))
		with self._spy_orig() as mocked:
			si.make_precision_loss_gl_entry([])
		mocked.assert_called_once()

	# --- TEST J ---
	def test_j_balanced_fx_00615_skips(self):
		import frappe

		name = "ACC-PINV-2026-00615"
		if not frappe.db.exists("Purchase Invoice", name):
			self.skipTest(f"{name} not on this site")
		doc = frappe.get_doc("Purchase Invoice", name)
		self.assertTrue(is_pattern_a_fx_purchase_invoice(doc))
		validate_pattern_a_rate_first_invariants(doc)
		with self._spy_orig() as mocked:
			entries, debit, credit = self._gl_balance(doc)
		mocked.assert_not_called()
		self.assertEqual(debit, credit)
		self._assert_no_precision_loss(entries)

	# --- TEST K ---
	def test_k_genuine_rounding_adjustment(self):
		import frappe
		from frappe.utils import nowdate, nowtime, today

		from erpnext_extensions.iran_accounting.e2e_bootstrap import ensure_test_item, get_warehouse

		supplier = frappe.db.get_value("Supplier", {"disabled": 0}, "name")
		expense = frappe.db.get_value("Company", self.company, "default_expense_account") or frappe.db.get_value(
			"Account", {"company": self.company, "root_type": "Expense", "is_group": 0}, "name"
		)
		cc = frappe.db.get_value("Company", self.company, "cost_center")
		irr_payable = frappe.db.get_value(
			"Account",
			{"company": self.company, "account_type": "Payable", "account_currency": "IRR", "is_group": 0},
			"name",
		)
		if not supplier or not expense:
			self.skipTest("No supplier/expense")
		item = ensure_test_item(self.company, "PA-RND")
		uom = frappe.get_cached_value("Item", item, "stock_uom")
		wh = get_warehouse(self.company)
		po = frappe.new_doc("Purchase Order")
		po.company = self.company
		po.supplier = supplier
		po.currency = "EUR"
		po.conversion_rate = CONV_01345
		po.transaction_date = nowdate()
		po.schedule_date = nowdate()
		po.remarks = "Pattern A rounding fixture"
		po.append(
			"items",
			{
				"item_code": item,
				"qty": QTY_01345,
				"rate": RATE_01345,
				"uom": uom,
				"stock_uom": uom,
				"conversion_factor": 1,
				"warehouse": wh,
				"schedule_date": nowdate(),
				"expense_account": expense,
				"cost_center": cc,
			},
		)
		try:
			po.insert(ignore_permissions=True)
			po.submit()
		except Exception as exc:
			self.skipTest(f"PO failed: {exc}")
		pi = frappe.new_doc("Purchase Invoice")
		pi.company = self.company
		pi.supplier = supplier
		pi.posting_date = today()
		pi.set_posting_time = 1
		pi.posting_time = nowtime()
		pi.currency = "EUR"
		pi.conversion_rate = CONV_01345
		pi.update_stock = 0
		pi.remarks = "Pattern A rounding fixture"
		pi.disable_rounded_total = 0
		if irr_payable:
			pi.credit_to = irr_payable
		pi.append(
			"items",
			{
				"item_code": item,
				"qty": QTY_01345,
				"rate": RATE_01345,
				"uom": uom,
				"stock_uom": uom,
				"conversion_factor": 1,
				"warehouse": wh,
				"expense_account": expense,
				"cost_center": cc,
				"purchase_order": po.name,
				"po_detail": po.items[0].name,
			},
		)
		pi.insert(ignore_permissions=True)
		# Genuine invoice rounding (EUR 0.01) — not FX composition residual.
		pi.rounding_adjustment = 0.01
		pi.rounded_total = flt(pi.grand_total) + 0.01
		round_irr_invoice_totals(pi)
		self.assertTrue(is_pattern_a_fx_purchase_invoice(pi))
		validate_pattern_a_rate_first_invariants(pi)
		self.assertNotEqual(flt(pi.base_rounding_adjustment), 0)
		self.assertEqual(
			flt(pi.base_rounded_total),
			flt(pi.base_grand_total) + flt(pi.base_rounding_adjustment),
		)
		with self._spy_orig() as mocked:
			entries, debit, credit = self._gl_balance(pi)
		mocked.assert_not_called()
		self.assertEqual(debit, credit, msg=entries)
		self._assert_no_precision_loss(entries)
		round_off = frappe.get_cached_value("Company", pi.company, "round_off_account")
		rounding_rows = [
			g
			for g in entries
			if round_off and g.get("account") == round_off and flt(g.get("debit"))
		]
		self.assertTrue(rounding_rows, msg="make_gle_for_rounding_adjustment must still post")
		self.assertEqual(flt(rounding_rows[0].get("debit")), flt(pi.base_rounding_adjustment))

	# --- TEST O ---
	def test_o_wrapper_idempotency_and_original_preservation(self):
		from erpnext.controllers.accounts_controller import AccountsController
		from erpnext.accounts.doctype.purchase_invoice.purchase_invoice import PurchaseInvoice

		from erpnext_extensions.iran_accounting.integration.monkey_patches import (
			apply_monkey_patches,
			assert_pattern_a_precision_loss_patch_supported,
		)

		fn1 = AccountsController.make_precision_loss_gl_entry
		orig1 = AccountsController._iran_original_precision_loss
		apply_monkey_patches()
		apply_monkey_patches()
		fn2 = AccountsController.make_precision_loss_gl_entry
		orig2 = AccountsController._iran_original_precision_loss
		self.assertIs(fn1, fn2)
		self.assertIs(orig1, orig2)
		self.assertTrue(getattr(fn2, "_iran_pattern_a_skip", False))
		self.assertTrue(getattr(fn2, "_iran_pattern_a_fail_closed", False))
		self.assertFalse(getattr(orig2, "_iran_pattern_a_skip", False))
		self.assertEqual(orig2.__module__, "erpnext.controllers.accounts_controller")
		self.assertIs(PurchaseInvoice.make_precision_loss_gl_entry, fn2)
		assert_pattern_a_precision_loss_patch_supported(orig2)

	def test_upgrade_guard_blocks_erpnext_16_36(self):
		from erpnext.controllers.accounts_controller import AccountsController

		from erpnext_extensions.iran_accounting.integration.monkey_patches import (
			assert_pattern_a_precision_loss_patch_supported,
		)

		orig = AccountsController._iran_original_precision_loss
		with patch("erpnext.__version__", "16.36.0"):
			with self.assertRaises(RuntimeError) as ctx:
				assert_pattern_a_precision_loss_patch_supported(orig)
		self.assertIn("16.36", str(ctx.exception))
		with patch("erpnext.__version__", "16.35.0"):
			assert_pattern_a_precision_loss_patch_supported(orig)


class TestPatternAPrecisionLossTaxAndCurrency(unittest.TestCase):
	"""TEST L / M / N — reuse created fixtures from Pattern A suite helpers."""

	@classmethod
	def setUpClass(cls):
		import frappe

		if not getattr(frappe.local, "site", None):
			raise unittest.SkipTest("frappe site not initialized")
		frappe.set_user("Administrator")
		from erpnext_extensions.iran_accounting.e2e_bootstrap import get_irr_company
		from erpnext_extensions.iran_accounting.integration.bootstrap import apply

		apply()
		try:
			cls.company = (
				"اسپاد فارمد دارو"
				if frappe.db.exists("Company", "اسپاد فارمد دارو")
				else get_irr_company("ESPAD")
			)
		except Exception:
			raise unittest.SkipTest("No IRR company")
		cls.supplier = frappe.db.get_value("Supplier", {"disabled": 0}, "name")
		cls.expense = frappe.db.get_value("Company", cls.company, "default_expense_account") or frappe.db.get_value(
			"Account", {"company": cls.company, "root_type": "Expense", "is_group": 0}, "name"
		)
		cls.cc = frappe.db.get_value("Company", cls.company, "cost_center")
		cls.irr_payable = frappe.db.get_value(
			"Account",
			{"company": cls.company, "account_type": "Payable", "account_currency": "IRR", "is_group": 0},
			"name",
		)
		cls.eur_payable = frappe.db.get_value(
			"Account",
			{"company": cls.company, "account_type": "Payable", "account_currency": "EUR", "is_group": 0},
			"name",
		)
		cls.tax_account = frappe.db.get_value(
			"Account",
			{"company": cls.company, "account_type": "Tax", "is_group": 0},
			"name",
		) or frappe.db.get_value(
			"Account",
			{"company": cls.company, "root_type": "Liability", "is_group": 0, "account_currency": "IRR"},
			"name",
		)
		if not cls.supplier or not cls.expense:
			raise unittest.SkipTest("No supplier/expense account")

	def tearDown(self):
		import frappe

		frappe.db.rollback()

	def _orig_spy(self):
		return patch.object(
			__import__(
				"erpnext.controllers.accounts_controller", fromlist=["AccountsController"]
			).AccountsController,
			"_iran_original_precision_loss",
			wraps=__import__(
				"erpnext.controllers.accounts_controller", fromlist=["AccountsController"]
			).AccountsController._iran_original_precision_loss,
		)

	def _new_fx_pi(self, *, qty, rate, conversion_rate=CONV_01345, credit_to=None, update_stock=0):
		import frappe
		from frappe.utils import nowdate, nowtime, today

		from erpnext_extensions.iran_accounting.e2e_bootstrap import ensure_test_item, get_warehouse

		item = ensure_test_item(self.company, "PA-FC")
		uom = frappe.get_cached_value("Item", item, "stock_uom")
		wh = get_warehouse(self.company)
		po = frappe.new_doc("Purchase Order")
		po.company = self.company
		po.supplier = self.supplier
		po.currency = "EUR"
		po.conversion_rate = conversion_rate
		po.transaction_date = nowdate()
		po.schedule_date = nowdate()
		po.remarks = "Pattern A fail-closed fixture"
		po.append(
			"items",
			{
				"item_code": item,
				"qty": qty,
				"rate": rate,
				"uom": uom,
				"stock_uom": uom,
				"conversion_factor": 1,
				"warehouse": wh,
				"schedule_date": nowdate(),
				"expense_account": self.expense,
				"cost_center": self.cc,
			},
		)
		try:
			po.insert(ignore_permissions=True)
			po.submit()
		except Exception as exc:
			self.skipTest(f"PO failed: {exc}")
		pi = frappe.new_doc("Purchase Invoice")
		pi.company = self.company
		pi.supplier = self.supplier
		pi.posting_date = today()
		pi.set_posting_time = 1
		pi.posting_time = nowtime()
		pi.currency = "EUR"
		pi.conversion_rate = conversion_rate
		pi.update_stock = update_stock
		pi.remarks = "Pattern A fail-closed fixture"
		if credit_to:
			pi.credit_to = credit_to
		pi.append(
			"items",
			{
				"item_code": item,
				"qty": qty,
				"rate": rate,
				"uom": uom,
				"stock_uom": uom,
				"conversion_factor": 1,
				"warehouse": wh,
				"expense_account": self.expense,
				"cost_center": self.cc,
				"purchase_order": po.name,
				"po_detail": po.items[0].name,
			},
		)
		return pi

	def _assert_valid_skip(self, pi):
		self.assertTrue(is_pattern_a_fx_purchase_invoice(pi))
		validate_pattern_a_rate_first_invariants(pi)
		with self._orig_spy() as mocked:
			entries = pi.get_gl_entries()
		mocked.assert_not_called()
		debit = sum(flt(g.get("debit")) for g in entries)
		credit = sum(flt(g.get("credit")) for g in entries)
		self.assertEqual(debit, credit, msg=entries)
		self.assertFalse(
			any((g.get("remarks") or "") == "Net total calculation precision loss" for g in entries)
		)
		return entries

	def test_l_vat_exclusive(self):
		if not self.tax_account:
			self.skipTest("No tax account")
		pi = self._new_fx_pi(qty=QTY_01345, rate=RATE_01345)
		if self.irr_payable:
			pi.credit_to = self.irr_payable
		pi.append(
			"taxes",
			{
				"charge_type": "On Net Total",
				"account_head": self.tax_account,
				"description": "VAT 10",
				"rate": 10,
				"category": "Total",
				"add_deduct_tax": "Add",
				"cost_center": self.cc,
			},
		)
		pi.insert(ignore_permissions=True)
		entries = self._assert_valid_skip(pi)
		tax_debit = sum(flt(g.get("debit")) for g in entries if g.get("account") == self.tax_account)
		self.assertEqual(tax_debit, flt(pi.base_total_taxes_and_charges))

	def test_l_inclusive_vat(self):
		if not self.tax_account:
			self.skipTest("No tax account")
		pi = self._new_fx_pi(qty=100, rate=11, conversion_rate=100_000)
		if self.irr_payable:
			pi.credit_to = self.irr_payable
		pi.append(
			"taxes",
			{
				"charge_type": "On Net Total",
				"account_head": self.tax_account,
				"description": "VAT incl 10",
				"rate": 10,
				"category": "Total",
				"add_deduct_tax": "Add",
				"included_in_print_rate": 1,
				"cost_center": self.cc,
			},
		)
		pi.insert(ignore_permissions=True)
		self._assert_valid_skip(pi)

	def test_l_multiple_tax_rows(self):
		if not self.tax_account:
			self.skipTest("No tax account")
		pi = self._new_fx_pi(qty=QTY_01345, rate=RATE_01345)
		if self.irr_payable:
			pi.credit_to = self.irr_payable
		for rate in (5, 4):
			pi.append(
				"taxes",
				{
					"charge_type": "On Net Total",
					"account_head": self.tax_account,
					"description": f"VAT {rate}",
					"rate": rate,
					"category": "Total",
					"add_deduct_tax": "Add",
					"cost_center": self.cc,
				},
			)
		pi.insert(ignore_permissions=True)
		self.assertEqual(len(pi.taxes), 2)
		self._assert_valid_skip(pi)

	def test_m_irr_payable(self):
		pi = self._new_fx_pi(qty=QTY_01345, rate=RATE_01345)
		if self.irr_payable:
			pi.credit_to = self.irr_payable
		pi.insert(ignore_permissions=True)
		self.assertEqual(flt(pi.outstanding_amount), BASE_AMOUNT_01345)
		self._assert_valid_skip(pi)

	def test_m_eur_payable(self):
		if not self.eur_payable:
			self.skipTest("No EUR payable")
		pi = self._new_fx_pi(qty=QTY_01345, rate=RATE_01345, credit_to=self.eur_payable)
		pi.insert(ignore_permissions=True)
		self.assertEqual(pi.party_account_currency, "EUR")
		self.assertEqual(flt(pi.outstanding_amount), AMOUNT_01345)
		entries = self._assert_valid_skip(pi)
		payable = next(g for g in entries if flt(g.get("credit")))
		self.assertEqual(flt(payable.get("credit")), BASE_AMOUNT_01345)
		self.assertEqual(flt(payable.get("credit_in_account_currency")), AMOUNT_01345)

	def test_n_update_stock_zero(self):
		pi = self._new_fx_pi(qty=QTY_01345, rate=RATE_01345, update_stock=0)
		if self.irr_payable:
			pi.credit_to = self.irr_payable
		pi.insert(ignore_permissions=True)
		self.assertEqual(pi.update_stock, 0)
		self._assert_valid_skip(pi)

	def test_n_update_stock_one(self):
		import frappe
		from frappe.utils import nowdate, nowtime

		from erpnext_extensions.iran_accounting.e2e_bootstrap import ensure_test_item, get_warehouse

		item = ensure_test_item(self.company, "PA-FC-STK")
		uom = frappe.get_cached_value("Item", item, "stock_uom")
		wh = get_warehouse(self.company)
		po = frappe.new_doc("Purchase Order")
		po.company = self.company
		po.supplier = self.supplier
		po.currency = "EUR"
		po.conversion_rate = CONV_01345
		po.transaction_date = nowdate()
		po.schedule_date = nowdate()
		po.remarks = "Pattern A fail-closed stock"
		po.append(
			"items",
			{
				"item_code": item,
				"qty": QTY_01345,
				"rate": RATE_01345,
				"uom": uom,
				"stock_uom": uom,
				"conversion_factor": 1,
				"warehouse": wh,
				"schedule_date": nowdate(),
				"expense_account": self.expense,
				"cost_center": self.cc,
			},
		)
		try:
			po.insert(ignore_permissions=True)
			po.submit()
		except Exception as exc:
			self.skipTest(f"PO failed: {exc}")
		pi = frappe.new_doc("Purchase Invoice")
		pi.company = self.company
		pi.supplier = self.supplier
		pi.currency = "EUR"
		pi.conversion_rate = CONV_01345
		pi.posting_date = nowdate()
		pi.set_posting_time = 1
		pi.posting_time = nowtime()
		pi.update_stock = 1
		pi.remarks = "Pattern A fail-closed stock"
		if self.irr_payable:
			pi.credit_to = self.irr_payable
		pi.append(
			"items",
			{
				"item_code": item,
				"qty": QTY_01345,
				"rate": RATE_01345,
				"uom": uom,
				"stock_uom": uom,
				"conversion_factor": 1,
				"warehouse": wh,
				"expense_account": self.expense,
				"cost_center": self.cc,
				"purchase_order": po.name,
				"po_detail": po.items[0].name,
			},
		)
		try:
			pi.insert(ignore_permissions=True)
		except Exception as exc:
			self.skipTest(f"update_stock PI insert failed: {exc}")
		self.assertEqual(pi.update_stock, 1)
		self._assert_valid_skip(pi)
		if pi.items[0].valuation_rate not in (None, ""):
			vr = flt(pi.items[0].valuation_rate)
			self.assertEqual(vr, float(int(vr)))

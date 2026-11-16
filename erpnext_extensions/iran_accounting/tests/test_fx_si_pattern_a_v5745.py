# Copyright (c) 2026, ERPNext Extensions contributors
"""FX Sales Invoice Pattern A — RATE-FIRST item bases drive IRR headers (v5.7.45).

Mirrors Purchase Invoice Pattern A. Anchor case: Sales Invoice 1405-00269
  EUR grand_total=48087, conversion_rate=1712842
  PRODUCT-FIRST header = 82_365_433_254
  RATE-FIRST Σ item base = 82_365_433_140
  residual = 114 IRR (must not appear as Round Off / must not block submit)
"""

from __future__ import annotations

import unittest
from contextlib import contextmanager
from decimal import Decimal, ROUND_HALF_UP
from types import SimpleNamespace
from unittest.mock import patch

from frappe.utils import flt

from erpnext_extensions.iran_accounting.accounts_invoice import round_irr_invoice_totals
from erpnext_extensions.iran_accounting.domain.qty_rate_amount import (
	invoice_has_distributed_discount,
	is_pattern_a_fx_invoice,
	is_pattern_a_fx_purchase_invoice,
	is_pattern_a_fx_sales_invoice,
	reaggregate_fx_invoice_base_totals,
	validate_pattern_a_rate_first_invariants,
)

# --- 1405-00269 constants ---------------------------------------------------------
CONV_00269 = 1_712_842.0
EUR_TOTAL_00269 = 48_087.0
PRODUCT_FIRST_00269 = 82_365_433_254  # 48087 × 1712842
RATE_FIRST_00269 = 82_365_433_140
GAP_00269 = 114

# Multi-line fixture matching production line composition (rate-first IRR).
LINES_00269 = (
	{"qty": 1393.0, "rate": 17.55, "amount": 24447.15},
	{"qty": 107.0, "rate": 17.55, "amount": 1877.85},
	{"qty": 300.0, "rate": 16.65, "amount": 4995.0},
	{"qty": 500.0, "rate": 18.45, "amount": 9225.0},
	{"qty": 300.0, "rate": 18.9, "amount": 5670.0},
	{"qty": 80.0, "rate": 23.4, "amount": 1872.0},
)


def _rh(x) -> float:
	return float(Decimal(str(x)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _rate_first_bases(qty, rate, conversion_rate):
	base_rate = _rh(Decimal(str(rate)) * Decimal(str(conversion_rate)))
	base_amount = _rh(Decimal(str(qty)) * Decimal(str(base_rate)))
	return base_rate, base_amount


def _row_from_line(line, conversion_rate=CONV_00269, product_first_header=False):
	qty = line["qty"]
	rate = line["rate"]
	amount = line["amount"]
	base_rate, base_amount = _rate_first_bases(qty, rate, conversion_rate)
	if product_first_header:
		# Simulate ERPNext product-first float bases before Iran align.
		base_rate = flt(rate) * flt(conversion_rate)
		base_amount = flt(amount) * flt(conversion_rate)

	def _get(key, default=None):
		return getattr(ns, key, default)

	def _set(key, value):
		setattr(ns, key, value)

	ns = SimpleNamespace(
		qty=qty,
		rate=rate,
		amount=amount,
		net_rate=rate,
		net_amount=amount,
		base_rate=base_rate,
		base_amount=base_amount,
		base_net_rate=base_rate,
		base_net_amount=base_amount,
		distributed_discount_amount=0.0,
		discount_amount=0.0,
		idx=0,
	)
	ns.get = _get
	ns.set = _set
	return ns


class _Meta:
	def has_field(self, _name):
		return True


class _Doc:
	def __init__(self, items, **kwargs):
		self.doctype = kwargs.get("doctype", "Sales Invoice")
		self.name = kwargs.get("name", "SI-FX-TEST")
		self.company = kwargs.get("company", "IRR-CO")
		self.currency = kwargs.get("currency", "EUR")
		self.conversion_rate = kwargs.get("conversion_rate", CONV_00269)
		self.docstatus = kwargs.get("docstatus", 0)
		self.items = items
		self.taxes = kwargs.get("taxes") or []
		self.payment_schedule = kwargs.get("payment_schedule") or []
		self.discount_amount = kwargs.get("discount_amount", 0.0)
		self.additional_discount_percentage = kwargs.get("additional_discount_percentage", 0.0)
		sum_base = sum(flt(r.base_amount) for r in items) if items else 0.0
		eur_total = sum(flt(r.amount) for r in items) if items else EUR_TOTAL_00269
		self.base_total = kwargs.get("base_total", PRODUCT_FIRST_00269)
		self.base_net_total = kwargs.get("base_net_total", PRODUCT_FIRST_00269)
		self.base_grand_total = kwargs.get("base_grand_total", PRODUCT_FIRST_00269)
		self.base_rounded_total = kwargs.get("base_rounded_total", PRODUCT_FIRST_00269)
		self.base_rounding_adjustment = kwargs.get("base_rounding_adjustment", 0.0)
		self.base_total_taxes_and_charges = kwargs.get("base_total_taxes_and_charges", 0.0)
		self.base_discount_amount = kwargs.get("base_discount_amount", 0.0)
		self.total = kwargs.get("total", eur_total)
		self.net_total = kwargs.get("net_total", eur_total)
		self.grand_total = kwargs.get("grand_total", eur_total)
		self.rounded_total = kwargs.get("rounded_total", eur_total)
		self.rounding_adjustment = kwargs.get("rounding_adjustment", 0.0)
		self.total_taxes_and_charges = kwargs.get("total_taxes_and_charges", 0.0)
		self.outstanding_amount = kwargs.get("outstanding_amount", PRODUCT_FIRST_00269)
		self.party_account_currency = kwargs.get("party_account_currency", "IRR")
		self.company_currency = kwargs.get("company_currency", "IRR")
		self.total_advance = 0.0
		self.write_off_amount = 0.0
		self.base_write_off_amount = 0.0
		self.paid_amount = 0.0
		self.base_paid_amount = 0.0
		self.meta = _Meta()
		self._sum_base_hint = sum_base

	def get(self, key, default=None):
		if key == "items":
			return self.items
		if key == "taxes":
			return self.taxes
		if key == "payment_schedule":
			return self.payment_schedule
		return getattr(self, key, default)

	def set(self, key, value):
		setattr(self, key, value)


@contextmanager
def _patches():
	with (
		patch(
			"erpnext_extensions.iran_accounting.domain.qty_rate_amount.rounding.is_irr_company",
			return_value=True,
		),
		patch(
			"erpnext_extensions.iran_accounting.domain.qty_rate_amount.rounding.get_company_currency",
			return_value="IRR",
		),
		patch(
			"erpnext_extensions.iran_accounting.accounts_invoice.is_irr_company",
			return_value=True,
		),
		patch(
			"erpnext_extensions.iran_accounting.accounts_invoice.get_company_currency",
			return_value="IRR",
		),
	):
		yield


def _doc_00269(**kwargs):
	items = [_row_from_line(line) for line in LINES_00269]
	for i, row in enumerate(items, start=1):
		row.idx = i
	ps = kwargs.pop("payment_schedule", None)
	if ps is None:
		ps = [
			SimpleNamespace(
				invoice_portion=100.0,
				base_payment_amount=PRODUCT_FIRST_00269,
				base_outstanding=PRODUCT_FIRST_00269,
				get=lambda k, d=None, _s=None: getattr(_s or ps[0], k, d),
			)
		]
		# fix closure
		def _get(key, default=None, row=ps[0]):
			return getattr(row, key, default)

		ps[0].get = _get
	return _Doc(items, payment_schedule=ps, **kwargs)


class TestFxSiPatternAUnit(unittest.TestCase):
	def test_detection_si_vs_pi_vs_pattern_b_vs_domestic(self):
		with _patches():
			si = _doc_00269()
			self.assertTrue(is_pattern_a_fx_sales_invoice(si))
			self.assertTrue(is_pattern_a_fx_invoice(si))
			self.assertFalse(is_pattern_a_fx_purchase_invoice(si))

			pi = _Doc([_row_from_line(LINES_00269[0])], doctype="Purchase Invoice")
			self.assertTrue(is_pattern_a_fx_purchase_invoice(pi))
			self.assertTrue(is_pattern_a_fx_invoice(pi))
			self.assertFalse(is_pattern_a_fx_sales_invoice(pi))

			si_b = _doc_00269(discount_amount=10.0)
			si_b.items[0].distributed_discount_amount = 10.0
			self.assertTrue(invoice_has_distributed_discount(si_b))
			self.assertFalse(is_pattern_a_fx_sales_invoice(si_b))
			self.assertFalse(is_pattern_a_fx_invoice(si_b))

			si_irr = _doc_00269(currency="IRR", conversion_rate=1.0)
			self.assertFalse(is_pattern_a_fx_sales_invoice(si_irr))

	def test_positive_1405_00269_reaggregates_headers(self):
		doc = _doc_00269()
		eur_grand = flt(doc.grand_total)
		eur_amounts = [flt(r.amount) for r in doc.items]
		with _patches():
			round_irr_invoice_totals(doc)
		self.assertEqual(flt(doc.base_grand_total), RATE_FIRST_00269)
		self.assertEqual(flt(doc.base_net_total), RATE_FIRST_00269)
		self.assertEqual(flt(doc.base_total), RATE_FIRST_00269)
		self.assertEqual(flt(doc.base_rounded_total), RATE_FIRST_00269)
		self.assertEqual(flt(doc.outstanding_amount), RATE_FIRST_00269)
		self.assertEqual(sum(flt(r.base_amount) for r in doc.items), RATE_FIRST_00269)
		self.assertEqual(flt(doc.grand_total), eur_grand)
		self.assertEqual([flt(r.amount) for r in doc.items], eur_amounts)
		self.assertEqual(PRODUCT_FIRST_00269 - RATE_FIRST_00269, GAP_00269)

	def test_positive_fractional_rates_multi_line(self):
		lines = (
			{"qty": 11.0, "rate": 0.09, "amount": 0.99},
			{"qty": 7.5, "rate": 12.34, "amount": 92.55},
			{"qty": 100.0, "rate": 1.15, "amount": 115.0},
		)
		items = [_row_from_line(line, conversion_rate=2_830_792.0) for line in lines]
		eur = sum(flt(r.amount) for r in items)
		product = _rh(Decimal(str(eur)) * Decimal("2830792"))
		doc = _Doc(
			items,
			conversion_rate=2_830_792.0,
			base_total=product,
			base_net_total=product,
			base_grand_total=product,
			base_rounded_total=product,
			outstanding_amount=product,
			total=eur,
			net_total=eur,
			grand_total=eur,
			rounded_total=eur,
		)
		with _patches():
			round_irr_invoice_totals(doc)
		rate_first = sum(flt(r.base_amount) for r in doc.items)
		self.assertEqual(flt(doc.base_grand_total), rate_first)
		self.assertEqual(flt(doc.grand_total), eur)
		validate_pattern_a_rate_first_invariants(doc)

	def test_positive_payment_schedule_base_follows_header(self):
		ps_row = SimpleNamespace(
			invoice_portion=100.0,
			base_payment_amount=PRODUCT_FIRST_00269,
			base_outstanding=PRODUCT_FIRST_00269,
		)
		ps_row.get = lambda k, d=None: getattr(ps_row, k, d)
		doc = _doc_00269(payment_schedule=[ps_row])
		with _patches():
			round_irr_invoice_totals(doc)
		self.assertEqual(flt(ps_row.base_payment_amount), RATE_FIRST_00269)
		self.assertEqual(flt(doc.base_grand_total), RATE_FIRST_00269)

	def test_idempotent_round_irr_invoice_totals(self):
		doc = _doc_00269()
		with _patches():
			round_irr_invoice_totals(doc)
			snap = (
				flt(doc.base_grand_total),
				flt(doc.outstanding_amount),
				tuple(flt(r.base_amount) for r in doc.items),
				flt(doc.grand_total),
			)
			round_irr_invoice_totals(doc)
			round_irr_invoice_totals(doc)
		self.assertEqual(
			(
				flt(doc.base_grand_total),
				flt(doc.outstanding_amount),
				tuple(flt(r.base_amount) for r in doc.items),
				flt(doc.grand_total),
			),
			snap,
		)

	def test_negative_pattern_b_does_not_reaggregate(self):
		doc = _doc_00269(discount_amount=50.0)
		doc.items[0].distributed_discount_amount = 50.0
		# Keep hybrid product-first header; Pattern B must leave it alone.
		with _patches():
			round_irr_invoice_totals(doc)
		self.assertFalse(is_pattern_a_fx_sales_invoice(doc))
		self.assertEqual(flt(doc.base_grand_total), PRODUCT_FIRST_00269)

	def test_negative_domestic_irr_unchanged_path(self):
		items = [_row_from_line({"qty": 10, "rate": 1000, "amount": 10000}, conversion_rate=1.0)]
		for r in items:
			r.base_rate = 1000
			r.base_amount = 10000
			r.base_net_rate = 1000
			r.base_net_amount = 10000
		doc = _Doc(
			items,
			currency="IRR",
			conversion_rate=1.0,
			base_total=10000,
			base_net_total=10000,
			base_grand_total=10000,
			base_rounded_total=10000,
			outstanding_amount=10000,
			total=10000,
			net_total=10000,
			grand_total=10000,
			rounded_total=10000,
		)
		self.assertFalse(is_pattern_a_fx_invoice(doc))
		with _patches():
			reaggregate_fx_invoice_base_totals(doc)
		self.assertEqual(flt(doc.base_grand_total), 10000)

	def test_negative_inconsistent_headers_fail_closed(self):
		doc = _doc_00269()
		with _patches():
			round_irr_invoice_totals(doc)
		# Corrupt after alignment — invariants must throw.
		doc.base_net_total = PRODUCT_FIRST_00269
		with _patches():
			with self.assertRaises(Exception) as ctx:
				validate_pattern_a_rate_first_invariants(doc)
		self.assertIn("base_net_total", str(ctx.exception))


class TestFxSiPatternALive140500269(unittest.TestCase):
	"""Live verification against restored Sales Invoice 1405-00269."""

	@classmethod
	def setUpClass(cls):
		import frappe

		if not getattr(frappe.local, "site", None):
			raise unittest.SkipTest("frappe site not initialized")
		frappe.set_user("Administrator")
		from erpnext_extensions.iran_accounting.integration.bootstrap import apply

		apply()
		if not frappe.db.exists("Sales Invoice", "1405-00269"):
			raise unittest.SkipTest("1405-00269 not on this site")

	def tearDown(self):
		import frappe

		frappe.db.rollback()

	def test_live_align_and_gl_balance_without_precision_loss(self):
		import frappe
		from frappe.utils import cint
		from erpnext.controllers.accounts_controller import AccountsController

		doc = frappe.get_doc("Sales Invoice", "1405-00269")
		if cint(doc.docstatus) == 1:
			self.skipTest("1405-00269 already submitted on this site")

		eur_grand = flt(doc.grand_total)
		eur_net = flt(doc.net_total)
		eur_item_amounts = [flt(r.amount) for r in doc.items]
		eur_rates = [flt(r.rate) for r in doc.items]

		self.assertTrue(is_pattern_a_fx_sales_invoice(doc))
		doc.validate()
		round_irr_invoice_totals(doc, "validate")
		self.assertEqual(flt(doc.base_grand_total), RATE_FIRST_00269)
		self.assertEqual(sum(flt(r.base_net_amount) for r in doc.items), RATE_FIRST_00269)
		validate_pattern_a_rate_first_invariants(doc)

		with patch.object(
			AccountsController,
			"_iran_original_precision_loss",
			wraps=AccountsController._iran_original_precision_loss,
		) as mocked:
			entries = doc.get_gl_entries()
		mocked.assert_not_called()

		debit = sum(flt(g.get("debit")) for g in entries)
		credit = sum(flt(g.get("credit")) for g in entries)
		self.assertEqual(debit, credit)
		self.assertEqual(debit, RATE_FIRST_00269)
		self.assertFalse(
			any((g.get("remarks") or "") == "Net total calculation precision loss" for g in entries)
		)
		self.assertEqual(flt(doc.grand_total), eur_grand)
		self.assertEqual(flt(doc.net_total), eur_net)
		self.assertEqual([flt(r.amount) for r in doc.items], eur_item_amounts)
		self.assertEqual([flt(r.rate) for r in doc.items], eur_rates)

	def test_live_submit_balances_gl(self):
		import frappe
		from frappe.utils import cint
		from frappe.model.workflow import apply_workflow, get_workflow_name

		doc = frappe.get_doc("Sales Invoice", "1405-00269")
		if cint(doc.docstatus) == 1:
			self.skipTest("1405-00269 already submitted on this site")

		doc.flags.ignore_permissions = True
		submitted = False
		if get_workflow_name("Sales Invoice"):
			try:
				apply_workflow(frappe.get_doc("Sales Invoice", "1405-00269"), "Approved by CEO")
				submitted = True
			except Exception:
				frappe.db.rollback()
		if not submitted:
			doc = frappe.get_doc("Sales Invoice", "1405-00269")
			doc.flags.ignore_permissions = True
			doc.submit()

		doc = frappe.get_doc("Sales Invoice", "1405-00269")
		self.assertEqual(cint(doc.docstatus), 1)
		self.assertEqual(flt(doc.grand_total), EUR_TOTAL_00269)
		self.assertEqual(flt(doc.base_grand_total), RATE_FIRST_00269)

		rows = frappe.db.sql(
			"""
			SELECT account, SUM(debit) debit, SUM(credit) credit
			FROM `tabGL Entry`
			WHERE voucher_type='Sales Invoice' AND voucher_no=%s AND is_cancelled=0
			GROUP BY account
			""",
			doc.name,
			as_dict=True,
		)
		self.assertTrue(rows)
		total_debit = sum(flt(r.debit) for r in rows)
		total_credit = sum(flt(r.credit) for r in rows)
		self.assertEqual(total_debit, total_credit)
		self.assertEqual(total_debit, RATE_FIRST_00269)
		pl = frappe.db.sql(
			"""
			SELECT name FROM `tabGL Entry`
			WHERE voucher_type='Sales Invoice' AND voucher_no=%s
			  AND is_cancelled=0 AND remarks=%s
			""",
			(doc.name, "Net total calculation precision loss"),
		)
		self.assertFalse(pl)
		cnt = frappe.db.count(
			"GL Entry",
			{"voucher_type": "Sales Invoice", "voucher_no": doc.name, "is_cancelled": 0},
		)
		self.assertGreaterEqual(cnt, 2)
		frappe.db.rollback()

class TestFxSiPatternALiveNegatives(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		import frappe

		if not getattr(frappe.local, "site", None):
			raise unittest.SkipTest("frappe site not initialized")
		frappe.set_user("Administrator")
		from erpnext_extensions.iran_accounting.integration.bootstrap import apply

		apply()
		from erpnext_extensions.iran_accounting.e2e_bootstrap import get_irr_company

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

	def test_pi_pattern_a_still_skips_precision_loss(self):
		import frappe

		from erpnext_extensions.iran_accounting.tests.test_fx_purchase_invoice_pattern_a import (
			BASE_AMOUNT_01345,
		)

		name = "ACC-PINV-2026-01345"
		if not frappe.db.exists("Purchase Invoice", name):
			self.skipTest(f"{name} not on this site")
		doc = frappe.get_doc("Purchase Invoice", name)
		self.assertTrue(is_pattern_a_fx_purchase_invoice(doc))
		validate_pattern_a_rate_first_invariants(doc)
		with patch.object(
			__import__(
				"erpnext.controllers.accounts_controller", fromlist=["AccountsController"]
			).AccountsController,
			"_iran_original_precision_loss",
			wraps=__import__(
				"erpnext.controllers.accounts_controller", fromlist=["AccountsController"]
			).AccountsController._iran_original_precision_loss,
		) as mocked:
			entries = doc.get_gl_entries()
		mocked.assert_not_called()
		debit = sum(flt(g.get("debit")) for g in entries)
		credit = sum(flt(g.get("credit")) for g in entries)
		self.assertEqual(debit, credit)
		self.assertEqual(debit, BASE_AMOUNT_01345)

	def test_unbalanced_non_pattern_a_still_rejected(self):
		"""Genuine debit/credit imbalance outside Pattern A must still throw."""
		import frappe
		from erpnext.accounts import general_ledger as gl

		company = self.company
		# Build a tiny fake gl_map with 114 IRR imbalance — SI voucher type, not Pattern A path.
		# Use process_debit_credit_difference directly (Iran patched).
		from frappe.utils import nowdate

		account = frappe.db.get_value(
			"Account",
			{"company": company, "is_group": 0, "account_type": "Expense Account"},
			"name",
		) or frappe.db.get_value("Account", {"company": company, "is_group": 0}, "name")
		if not account:
			self.skipTest("No account")
		cost_center = frappe.db.get_value("Company", company, "cost_center")
		gl_map = [
			frappe._dict(
				company=company,
				account=account,
				debit=114,
				credit=0,
				debit_in_account_currency=114,
				credit_in_account_currency=0,
				debit_in_transaction_currency=114,
				credit_in_transaction_currency=0,
				voucher_type="Journal Entry",
				voucher_no="JE-FAKE-UNBAL",
				posting_date=nowdate(),
				cost_center=cost_center,
				account_currency="IRR",
				is_opening="No",
			),
			frappe._dict(
				company=company,
				account=account,
				debit=0,
				credit=0,
				debit_in_account_currency=0,
				credit_in_account_currency=0,
				debit_in_transaction_currency=0,
				credit_in_transaction_currency=0,
				voucher_type="Journal Entry",
				voucher_no="JE-FAKE-UNBAL",
				posting_date=nowdate(),
				cost_center=cost_center,
				account_currency="IRR",
				is_opening="No",
			),
		]
		with self.assertRaises(frappe.ValidationError) as ctx:
			gl.process_debit_credit_difference(gl_map)
		self.assertIn("Debit and Credit not equal", str(ctx.exception))

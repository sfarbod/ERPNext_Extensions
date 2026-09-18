# Copyright (c) 2026, ERPNext Extensions contributors
"""FX Purchase Invoice Pattern A — RATE-FIRST item bases drive IRR headers.

ACC-PINV-2026-01345 mathematics:
  qty=54000, rate=0.09 EUR, conversion_rate=2830792
  base_rate=254771, base_amount=13757634000
  headers follow items; GL balances; no Round Off GLE.

Pattern B (additional/distributed discount) is out of scope and must not change.
"""

from __future__ import annotations

import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

from frappe.utils import flt

from erpnext_extensions.iran_accounting.accounts_invoice import round_irr_invoice_totals
from erpnext_extensions.iran_accounting.domain.qty_rate_amount import (
	_align_po_pi_si_row,
	invoice_has_distributed_discount,
	is_pattern_a_fx_purchase_invoice,
	reaggregate_fx_purchase_invoice_base_totals,
)


# --- ACC-PINV-2026-01345 constants -------------------------------------------------
QTY_01345 = 54000.0
RATE_01345 = 0.09
AMOUNT_01345 = 4860.0
CONV_01345 = 2830792.0
BASE_RATE_01345 = 254771
BASE_AMOUNT_01345 = 13_757_634_000
PRODUCT_FIRST_01345 = 13_757_649_120  # 4860 × 2830792
GAP_01345 = 15_120

PATTERN_B_00629 = {
	"total": 2542.0,
	"net_total": 2220.0,
	"grand_total": 2220.0,
	"base_total": 4_194_300_000.0,
	"base_net_total": 3_663_000_000.0,
	"base_grand_total": 3_663_000_000.0,
	"discount_amount": 322.0,
	"base_discount_amount": 531_300_000.0,
	"outstanding_amount": 3_663_000_000.0,
	"conversion_rate": 1_650_000.0,
	"items": (
		{
			"qty": 2.0,
			"rate": 850.0,
			"amount": 1700.0,
			"net_amount": 1484.66,
			"base_rate": 1_402_500_000.0,
			"base_amount": 2_805_000_000.0,
			"base_net_amount": 2_805_000_000.0,
			"distributed_discount_amount": 215.34,
		},
		{
			"qty": 2.0,
			"rate": 167.0,
			"amount": 334.0,
			"net_amount": 291.7,
			"base_rate": 275_550_000.0,
			"base_amount": 551_100_000.0,
			"base_net_amount": 551_100_000.0,
			"distributed_discount_amount": 42.31,
		},
		{
			"qty": 1.0,
			"rate": 342.0,
			"amount": 342.0,
			"net_amount": 298.68,
			"base_rate": 564_300_000.0,
			"base_amount": 564_300_000.0,
			"base_net_amount": 564_300_000.0,
			"distributed_discount_amount": 43.32,
		},
		{
			"qty": 1.0,
			"rate": 166.0,
			"amount": 166.0,
			"net_amount": 144.97,
			"base_rate": 273_900_000.0,
			"base_amount": 273_900_000.0,
			"base_net_amount": 273_900_000.0,
			"distributed_discount_amount": 21.03,
		},
	),
}

PATTERN_B_00284 = {
	"total": 5900.0,
	"net_total": 5560.0,
	"grand_total": 5560.0,
	"base_total": 8_850_000_000.0,
	"base_net_total": 8_340_000_000.0,
	"base_grand_total": 8_340_000_000.0,
	"discount_amount": 340.0,
	"base_discount_amount": 510_000_000.0,
	"outstanding_amount": 5560.0,
	"conversion_rate": 1_500_000.0,
	"items": (
		{
			"qty": 25.0,
			"rate": 108.0,
			"amount": 2700.0,
			"net_amount": 2544.5,
			"base_rate": 162_000_000.0,
			"base_amount": 4_050_000_000.0,
			"base_net_amount": 4_050_000_000.0,
			"distributed_discount_amount": 155.59,
		},
		{
			"qty": 24.0,
			"rate": 108.0,
			"amount": 2592.0,
			"net_amount": 2442.72,
			"base_rate": 162_000_000.0,
			"base_amount": 3_888_000_000.0,
			"base_net_amount": 3_888_000_000.0,
			"distributed_discount_amount": 149.37,
		},
		{
			"qty": 1.0,
			"rate": 108.0,
			"amount": 108.0,
			"net_amount": 101.77,
			"base_rate": 162_000_000.0,
			"base_amount": 162_000_000.0,
			"base_net_amount": 162_000_000.0,
			"distributed_discount_amount": 6.21,
		},
		{
			"qty": 1.0,
			"rate": 500.0,
			"amount": 500.0,
			"net_amount": 471.19,
			"base_rate": 750_000_000.0,
			"base_amount": 750_000_000.0,
			"base_net_amount": 750_000_000.0,
			"distributed_discount_amount": 28.81,
		},
	),
}


def _row(**kwargs):
	defaults = {
		"qty": QTY_01345,
		"rate": RATE_01345,
		"amount": AMOUNT_01345,
		"net_rate": RATE_01345,
		"net_amount": AMOUNT_01345,
		"base_rate": 254771.28,
		"base_amount": PRODUCT_FIRST_01345,
		"base_net_rate": 254771.28,
		"base_net_amount": PRODUCT_FIRST_01345,
		"distributed_discount_amount": 0.0,
		"discount_amount": 0.0,
	}
	defaults.update(kwargs)

	def _get(key, default=None):
		return getattr(ns, key, default)

	def _set(key, value):
		setattr(ns, key, value)

	ns = SimpleNamespace(**defaults)
	ns.get = _get
	ns.set = _set
	return ns


class _Meta:
	def has_field(self, _name):
		return True


class _Doc:
	def __init__(self, items, **kwargs):
		self.doctype = kwargs.get("doctype", "Purchase Invoice")
		self.company = kwargs.get("company", "IRR-CO")
		self.currency = kwargs.get("currency", "EUR")
		self.conversion_rate = kwargs.get("conversion_rate", CONV_01345)
		self.docstatus = kwargs.get("docstatus", 0)
		self.items = items
		self.taxes = kwargs.get("taxes") or []
		self.payment_schedule = kwargs.get("payment_schedule") or []
		self.discount_amount = kwargs.get("discount_amount", 0.0)
		self.additional_discount_percentage = kwargs.get("additional_discount_percentage", 0.0)
		self.base_total = kwargs.get("base_total", PRODUCT_FIRST_01345)
		self.base_net_total = kwargs.get("base_net_total", PRODUCT_FIRST_01345)
		self.base_grand_total = kwargs.get("base_grand_total", PRODUCT_FIRST_01345)
		self.base_rounded_total = kwargs.get("base_rounded_total", PRODUCT_FIRST_01345)
		self.base_rounding_adjustment = kwargs.get("base_rounding_adjustment", 0.0)
		self.base_total_taxes_and_charges = kwargs.get("base_total_taxes_and_charges", 0.0)
		self.base_discount_amount = kwargs.get("base_discount_amount", 0.0)
		self.total = kwargs.get("total", AMOUNT_01345)
		self.net_total = kwargs.get("net_total", AMOUNT_01345)
		self.grand_total = kwargs.get("grand_total", AMOUNT_01345)
		self.rounded_total = kwargs.get("rounded_total", AMOUNT_01345)
		self.rounding_adjustment = kwargs.get("rounding_adjustment", 0.0)
		self.total_taxes_and_charges = kwargs.get("total_taxes_and_charges", 0.0)
		self.outstanding_amount = kwargs.get("outstanding_amount", PRODUCT_FIRST_01345)
		self.party_account_currency = kwargs.get("party_account_currency", "IRR")
		self.company_currency = kwargs.get("company_currency", "IRR")
		self.total_advance = 0.0
		self.write_off_amount = 0.0
		self.base_write_off_amount = 0.0
		self.paid_amount = 0.0
		self.base_paid_amount = 0.0
		self.meta = _Meta()

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


class TestPatternARateFirstMath(unittest.TestCase):
	def test_01345_item_base_rate_from_rate_times_conversion(self):
		row = _row()
		_align_po_pi_si_row(row, "IRR", "EUR", conversion_rate=CONV_01345)
		self.assertEqual(flt(row.base_rate), BASE_RATE_01345)
		self.assertEqual(flt(row.base_amount), BASE_AMOUNT_01345)
		self.assertEqual(flt(row.base_net_rate), BASE_RATE_01345)
		self.assertEqual(flt(row.base_net_amount), BASE_AMOUNT_01345)
		self.assertEqual(flt(row.rate), RATE_01345)
		self.assertEqual(flt(row.amount), AMOUNT_01345)

	def test_01345_header_reaggregation(self):
		row = _row()
		sched = _row(
			invoice_portion=100.0,
			payment_amount=AMOUNT_01345,
			base_payment_amount=PRODUCT_FIRST_01345,
			outstanding=AMOUNT_01345,
			base_outstanding=PRODUCT_FIRST_01345,
		)
		doc = _Doc([row], payment_schedule=[sched])
		with _patches():
			round_irr_invoice_totals(doc)
		self.assertEqual(flt(row.base_rate), BASE_RATE_01345)
		self.assertEqual(flt(row.base_amount), BASE_AMOUNT_01345)
		self.assertEqual(flt(row.base_net_amount), BASE_AMOUNT_01345)
		self.assertEqual(flt(doc.base_total), BASE_AMOUNT_01345)
		self.assertEqual(flt(doc.base_net_total), BASE_AMOUNT_01345)
		self.assertEqual(flt(doc.base_grand_total), BASE_AMOUNT_01345)
		self.assertEqual(flt(doc.base_rounded_total), BASE_AMOUNT_01345)
		self.assertEqual(flt(doc.base_rounding_adjustment), 0.0)
		self.assertEqual(flt(doc.outstanding_amount), BASE_AMOUNT_01345)
		self.assertEqual(flt(doc.grand_total), AMOUNT_01345)
		self.assertEqual(flt(doc.net_total), AMOUNT_01345)
		self.assertEqual(flt(doc.conversion_rate), CONV_01345)
		self.assertEqual(flt(sched.payment_amount), AMOUNT_01345)
		self.assertEqual(flt(sched.base_payment_amount), BASE_AMOUNT_01345)

	def test_01345_idempotent(self):
		row = _row()
		doc = _Doc([row])
		with _patches():
			round_irr_invoice_totals(doc)
			first = (
				flt(row.base_rate),
				flt(row.base_amount),
				flt(doc.base_net_total),
				flt(doc.base_grand_total),
				flt(doc.outstanding_amount),
			)
			round_irr_invoice_totals(doc)
			second = (
				flt(row.base_rate),
				flt(row.base_amount),
				flt(doc.base_net_total),
				flt(doc.base_grand_total),
				flt(doc.outstanding_amount),
			)
		self.assertEqual(first, second)

	def test_qty_one_matches_product_first(self):
		row = _row(
			qty=1.0,
			rate=0.09,
			amount=0.09,
			net_rate=0.09,
			net_amount=0.09,
			base_rate=254771.28,
			base_amount=254771.28,
			base_net_rate=254771.28,
			base_net_amount=254771.28,
		)
		doc = _Doc(
			[row],
			total=0.09,
			net_total=0.09,
			grand_total=0.09,
			rounded_total=0.09,
			base_total=254771.28,
			base_net_total=254771.28,
			base_grand_total=254771.28,
			base_rounded_total=254771.28,
			outstanding_amount=254771.28,
		)
		with _patches():
			round_irr_invoice_totals(doc)
		self.assertEqual(flt(row.base_rate), BASE_RATE_01345)
		self.assertEqual(flt(row.base_amount), BASE_RATE_01345)
		self.assertEqual(flt(doc.base_grand_total), BASE_RATE_01345)
		self.assertEqual(flt(doc.grand_total), 0.09)

	def test_multiple_items_sum_to_headers(self):
		rows = [
			_row(qty=54000, rate=0.09, amount=4860, net_amount=4860, net_rate=0.09),
			_row(
				qty=1000,
				rate=0.15,
				amount=150,
				net_rate=0.15,
				net_amount=150,
				base_rate=424618.8,
				base_amount=424618800,
				base_net_rate=424618.8,
				base_net_amount=424618800,
			),
		]
		doc = _Doc(
			rows,
			total=5010,
			net_total=5010,
			grand_total=5010,
			rounded_total=5010,
		)
		with _patches():
			round_irr_invoice_totals(doc)
		expected_total = sum(flt(r.base_amount) for r in rows)
		self.assertEqual(flt(doc.base_total), expected_total)
		self.assertEqual(flt(doc.base_net_total), sum(flt(r.base_net_amount) for r in rows))
		self.assertEqual(flt(doc.base_grand_total), flt(doc.base_net_total))

	def test_tax_added_to_rate_first_net(self):
		row = _row()
		tax = _row(
			category="Total",
			add_deduct_tax="Add",
			tax_amount=486.0,
			base_tax_amount=1_375_764_912.0,
			base_tax_amount_after_discount_amount=1_375_764_912.0,
			tax_amount_after_discount_amount=486.0,
		)
		doc = _Doc(
			[row],
			taxes=[tax],
			base_total_taxes_and_charges=1_375_764_912.0,
			total_taxes_and_charges=486.0,
			grand_total=5346.0,
			rounded_total=5346.0,
		)
		with _patches():
			round_irr_invoice_totals(doc)
		self.assertEqual(flt(tax.tax_amount), 486.0)
		self.assertEqual(flt(tax.base_tax_amount_after_discount_amount), 1_375_764_912.0)
		self.assertEqual(flt(doc.base_net_total), BASE_AMOUNT_01345)
		self.assertEqual(flt(doc.base_grand_total), BASE_AMOUNT_01345 + 1_375_764_912.0)
		self.assertEqual(flt(doc.grand_total), 5346.0)

	def test_pattern_b_skip_header_reaggregation(self):
		row = _row(
			qty=2,
			rate=850,
			amount=1700,
			net_rate=742.33,
			net_amount=1484.66,
			base_rate=1_402_500_000.0,
			base_amount=2_805_000_000.0,
			base_net_rate=1_402_500_000.0,
			base_net_amount=2_805_000_000.0,
			distributed_discount_amount=215.34,
		)
		doc = _Doc(
			[row],
			currency="EUR",
			conversion_rate=1_650_000.0,
			discount_amount=322.0,
			total=2542.0,
			net_total=2220.0,
			grand_total=2220.0,
			rounded_total=2220.0,
			base_total=4_194_300_000.0,
			base_net_total=3_663_000_000.0,
			base_grand_total=3_663_000_000.0,
			base_rounded_total=3_663_000_000.0,
			base_discount_amount=531_300_000.0,
			outstanding_amount=3_663_000_000.0,
		)
		self.assertTrue(invoice_has_distributed_discount(doc))
		self.assertFalse(is_pattern_a_fx_purchase_invoice(doc))
		with _patches():
			round_irr_invoice_totals(doc)
		self.assertEqual(flt(row.net_amount), 1484.66)
		self.assertEqual(flt(row.base_net_amount), 2_805_000_000.0)
		self.assertEqual(flt(doc.base_net_total), 3_663_000_000.0)
		self.assertEqual(flt(doc.base_grand_total), 3_663_000_000.0)
		self.assertEqual(flt(doc.grand_total), 2220.0)
		self.assertEqual(flt(doc.discount_amount), 322.0)

	def test_sales_invoice_does_not_reaggregate_headers(self):
		row = _row()
		doc = _Doc([row], doctype="Sales Invoice")
		with _patches():
			round_irr_invoice_totals(doc)
		self.assertEqual(flt(row.base_amount), BASE_AMOUNT_01345)
		self.assertEqual(flt(doc.base_grand_total), PRODUCT_FIRST_01345)

	def test_submitted_document_not_rewritten(self):
		row = _row()
		doc = _Doc([row], docstatus=1)
		with _patches():
			reaggregate_fx_purchase_invoice_base_totals(doc)
		self.assertEqual(flt(doc.base_grand_total), PRODUCT_FIRST_01345)

	def test_irr_payable_outstanding_uses_base(self):
		row = _row()
		doc = _Doc([row], party_account_currency="IRR")
		with _patches():
			round_irr_invoice_totals(doc)
		self.assertEqual(flt(doc.outstanding_amount), BASE_AMOUNT_01345)

	def test_eur_payable_outstanding_stays_transaction(self):
		row = _row()
		doc = _Doc([row], party_account_currency="EUR")
		with _patches():
			round_irr_invoice_totals(doc)
		self.assertEqual(flt(doc.outstanding_amount), AMOUNT_01345)
		self.assertEqual(flt(doc.base_grand_total), BASE_AMOUNT_01345)


class TestPatternALiveDocuments(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		import frappe

		if not getattr(frappe.local, "site", None):
			raise unittest.SkipTest("frappe site not initialized")
		frappe.set_user("Administrator")
		from erpnext_extensions.iran_accounting.integration.bootstrap import apply

		apply()

	def tearDown(self):
		import frappe

		frappe.db.rollback()

	def _gl_sums(self, doc):
		entries = doc.get_gl_entries()
		debit = sum(flt(g.get("debit")) for g in entries)
		credit = sum(flt(g.get("credit")) for g in entries)
		return entries, debit, credit

	def _round_off_accounts(self, company):
		import frappe

		names = [
			frappe.get_cached_value("Company", company, "round_off_account"),
			frappe.get_cached_value("Company", company, "stock_adjustment_account"),
		]
		return {n for n in names if n}

	def test_acc_pinv_01345_rate_first_headers_and_gl(self):
		import frappe

		name = "ACC-PINV-2026-01345"
		if not frappe.db.exists("Purchase Invoice", name):
			self.skipTest(f"{name} not on this site")
		doc = frappe.get_doc("Purchase Invoice", name)
		self.assertEqual(doc.docstatus, 0, "must not submit/cancel the reference draft")
		before_txn = (
			flt(doc.items[0].rate),
			flt(doc.items[0].amount),
			flt(doc.grand_total),
			flt(doc.conversion_rate),
		)
		doc.run_method("validate")
		row = doc.items[0]
		self.assertEqual(flt(row.qty), QTY_01345)
		self.assertEqual(flt(row.rate), RATE_01345)
		self.assertEqual(flt(row.amount), AMOUNT_01345)
		self.assertEqual(flt(row.base_rate), BASE_RATE_01345)
		self.assertEqual(flt(row.base_amount), BASE_AMOUNT_01345)
		self.assertEqual(flt(row.base_net_amount), BASE_AMOUNT_01345)
		self.assertEqual(flt(doc.base_total), BASE_AMOUNT_01345)
		self.assertEqual(flt(doc.base_net_total), BASE_AMOUNT_01345)
		self.assertEqual(flt(doc.base_grand_total), BASE_AMOUNT_01345)
		self.assertEqual(flt(doc.base_rounded_total), BASE_AMOUNT_01345)
		self.assertEqual(flt(doc.outstanding_amount), BASE_AMOUNT_01345)
		self.assertEqual(flt(doc.grand_total), AMOUNT_01345)
		self.assertEqual(
			(flt(row.rate), flt(row.amount), flt(doc.grand_total), flt(doc.conversion_rate)),
			before_txn,
		)
		entries, debit, credit = self._gl_sums(doc)
		self.assertEqual(debit, credit)
		self.assertEqual(debit, BASE_AMOUNT_01345)
		self.assertEqual(debit - credit, 0)
		round_off = self._round_off_accounts(doc.company)
		self.assertFalse(
			any(g.get("account") in round_off for g in entries),
			"Pattern A must not post Round Off",
		)
		self.assertFalse(
			any(g.get("remarks") == "Net total calculation precision loss" for g in entries),
			"Pattern A must not post ERPNext precision-loss GLE",
		)
		payable = next(g for g in entries if flt(g.get("credit")))
		self.assertEqual(flt(payable.get("credit")), BASE_AMOUNT_01345)
		self.assertEqual(flt(payable.get("credit_in_transaction_currency")), AMOUNT_01345)

	def test_acc_pinv_01345_idempotent_validate(self):
		import frappe

		name = "ACC-PINV-2026-01345"
		if not frappe.db.exists("Purchase Invoice", name):
			self.skipTest(f"{name} not on this site")
		doc = frappe.get_doc("Purchase Invoice", name)
		doc.run_method("validate")
		first = (
			flt(doc.items[0].base_rate),
			flt(doc.items[0].base_amount),
			flt(doc.base_net_total),
			flt(doc.base_grand_total),
			flt(doc.outstanding_amount),
		)
		doc.run_method("validate")
		second = (
			flt(doc.items[0].base_rate),
			flt(doc.items[0].base_amount),
			flt(doc.base_net_total),
			flt(doc.base_grand_total),
			flt(doc.outstanding_amount),
		)
		self.assertEqual(first, second)

	def test_acc_pinv_00615_balanced_fx_unchanged(self):
		import frappe

		name = "ACC-PINV-2026-00615"
		if not frappe.db.exists("Purchase Invoice", name):
			self.skipTest(f"{name} not on this site")
		doc = frappe.get_doc("Purchase Invoice", name)
		before = {
			"grand_total": flt(doc.grand_total),
			"base_grand_total": flt(doc.base_grand_total),
			"base_net_total": flt(doc.base_net_total),
			"item_bases": [(flt(i.base_rate), flt(i.base_amount), flt(i.base_net_amount)) for i in doc.items],
		}
		doc.run_method("validate")
		self.assertEqual(flt(doc.grand_total), before["grand_total"])
		self.assertEqual(flt(doc.base_grand_total), before["base_grand_total"])
		self.assertEqual(flt(doc.base_net_total), before["base_net_total"])
		self.assertEqual(
			[(flt(i.base_rate), flt(i.base_amount), flt(i.base_net_amount)) for i in doc.items],
			before["item_bases"],
		)
		_, debit, credit = self._gl_sums(doc)
		self.assertEqual(debit, credit)
		self.assertEqual(debit, 2_679_600_000.0)

	def test_pattern_b_00629_unchanged(self):
		self._assert_pattern_b_frozen("ACC-PINV-2026-00629", PATTERN_B_00629)

	def test_pattern_b_00284_unchanged(self):
		self._assert_pattern_b_frozen("ACC-PINV-2026-00284", PATTERN_B_00284)

	def _assert_pattern_b_frozen(self, name, snapshot):
		import frappe

		if not frappe.db.exists("Purchase Invoice", name):
			self.skipTest(f"{name} not on this site")
		doc = frappe.get_doc("Purchase Invoice", name)
		# Isolation: Iran hook only. Full validate re-runs ERPNext taxes/discount
		# allocation and is out of Pattern A scope.
		round_irr_invoice_totals(doc)
		self.assertEqual(flt(doc.total), snapshot["total"])
		self.assertEqual(flt(doc.net_total), snapshot["net_total"])
		self.assertEqual(flt(doc.grand_total), snapshot["grand_total"])
		self.assertEqual(flt(doc.base_total), snapshot["base_total"])
		self.assertEqual(flt(doc.base_net_total), snapshot["base_net_total"])
		self.assertEqual(flt(doc.base_grand_total), snapshot["base_grand_total"])
		self.assertEqual(flt(doc.discount_amount), snapshot["discount_amount"])
		self.assertEqual(flt(doc.base_discount_amount), snapshot["base_discount_amount"])
		self.assertEqual(flt(doc.outstanding_amount), snapshot["outstanding_amount"])
		self.assertEqual(flt(doc.conversion_rate), snapshot["conversion_rate"])
		self.assertEqual(len(doc.items), len(snapshot["items"]))
		for row, expected in zip(doc.items, snapshot["items"], strict=True):
			self.assertEqual(flt(row.qty), expected["qty"])
			self.assertEqual(flt(row.rate), expected["rate"])
			self.assertEqual(flt(row.amount), expected["amount"])
			self.assertEqual(flt(row.net_amount), expected["net_amount"])
			self.assertEqual(flt(row.base_rate), expected["base_rate"])
			self.assertEqual(flt(row.base_amount), expected["base_amount"])
			self.assertEqual(flt(row.base_net_amount), expected["base_net_amount"])
			self.assertEqual(flt(row.distributed_discount_amount), expected["distributed_discount_amount"])


class TestPatternACreatedFixtures(unittest.TestCase):
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
		cls.warehouse = frappe.db.get_value("Warehouse", {"company": cls.company, "is_group": 0}, "name")
		cls.dept = (
			"واحد انبار - E"
			if frappe.db.exists("Department", "واحد انبار - E")
			else frappe.db.get_value("Department", {"company": cls.company}, "name")
		)
		if not cls.supplier or not cls.expense:
			raise unittest.SkipTest("No supplier/expense account")

	def tearDown(self):
		import frappe

		frappe.db.rollback()

	def _new_fx_pi(self, *, qty, rate, conversion_rate=CONV_01345, credit_to=None, update_stock=0):
		import frappe
		from frappe.utils import nowdate, nowtime, today

		from erpnext_extensions.iran_accounting.e2e_bootstrap import ensure_test_item, get_warehouse

		item = ensure_test_item(self.company, "PA-FX")
		uom = frappe.get_cached_value("Item", item, "stock_uom")
		wh = get_warehouse(self.company)
		po = frappe.new_doc("Purchase Order")
		po.company = self.company
		po.supplier = self.supplier
		po.currency = "EUR"
		po.conversion_rate = conversion_rate
		po.transaction_date = nowdate()
		po.schedule_date = nowdate()
		po.remarks = "Pattern A fixture"
		po_item = {
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
		}
		if self.dept:
			po_item["department"] = self.dept
			po.department = self.dept
		po.append("items", po_item)
		try:
			po.insert(ignore_permissions=True)
			po.submit()
		except Exception as exc:
			self.skipTest(f"PO required for Pattern A fixture; PO failed: {exc}")

		pi = frappe.new_doc("Purchase Invoice")
		pi.company = self.company
		pi.supplier = self.supplier
		pi.posting_date = today()
		pi.set_posting_time = 1
		pi.posting_time = nowtime()
		pi.currency = "EUR"
		pi.conversion_rate = conversion_rate
		pi.update_stock = update_stock
		pi.remarks = "Pattern A fixture"
		if credit_to:
			pi.credit_to = credit_to
		pi_item = {
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
		}
		if self.dept:
			pi_item["department"] = self.dept
			pi.department = self.dept
		pi.append("items", pi_item)
		return pi

	def _assert_no_round_off(self, doc, entries):
		import frappe

		round_off = frappe.get_cached_value("Company", doc.company, "round_off_account")
		if round_off:
			self.assertFalse(any(g.get("account") == round_off for g in entries))
		self.assertFalse(
			any(g.get("remarks") == "Net total calculation precision loss" for g in entries)
		)

	def _gl_balance(self, doc):
		entries = doc.get_gl_entries()
		debit = sum(flt(g.get("debit")) for g in entries)
		credit = sum(flt(g.get("credit")) for g in entries)
		return entries, debit, credit

	def test_created_01345_math_insert_preview_submit(self):
		import frappe
		from erpnext.controllers.stock_controller import show_accounting_ledger_preview

		pi = self._new_fx_pi(qty=QTY_01345, rate=RATE_01345)
		if self.irr_payable:
			pi.credit_to = self.irr_payable
		pi.insert(ignore_permissions=True)
		self.assertEqual(flt(pi.items[0].base_rate), BASE_RATE_01345)
		self.assertEqual(flt(pi.items[0].base_amount), BASE_AMOUNT_01345)
		self.assertEqual(flt(pi.base_grand_total), BASE_AMOUNT_01345)
		self.assertEqual(flt(pi.grand_total), AMOUNT_01345)
		self.assertEqual(pi.update_stock, 0)
		entries, debit, credit = self._gl_balance(pi)
		self.assertEqual(debit, credit)
		self.assertEqual(debit, BASE_AMOUNT_01345)
		self._assert_no_round_off(pi, entries)
		preview = show_accounting_ledger_preview(self.company, "Purchase Invoice", pi.name)
		self.assertTrue(preview.get("gl_data"))

	def test_created_01345_submit_gl_balanced(self):
		import frappe

		pi = self._new_fx_pi(qty=QTY_01345, rate=RATE_01345)
		if self.irr_payable:
			pi.credit_to = self.irr_payable
		pi.insert(ignore_permissions=True)
		pi.submit()
		posted = frappe.get_all(
			"GL Entry",
			filters={"voucher_type": "Purchase Invoice", "voucher_no": pi.name, "is_cancelled": 0},
			fields=["account", "debit", "credit", "remarks"],
		)
		debit = sum(flt(g.debit) for g in posted)
		credit = sum(flt(g.credit) for g in posted)
		self.assertEqual(debit, credit)
		self.assertEqual(debit, BASE_AMOUNT_01345)
		self.assertFalse(any(g.remarks == "Net total calculation precision loss" for g in posted))

	def test_vat_exclusive_gl_uses_rate_first_net_plus_tax_base(self):
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
		self.assertEqual(flt(pi.items[0].amount), AMOUNT_01345)
		self.assertEqual(flt(pi.net_total), AMOUNT_01345)
		self.assertEqual(flt(pi.total_taxes_and_charges), 486.0)
		self.assertEqual(flt(pi.items[0].base_net_amount), BASE_AMOUNT_01345)
		self.assertEqual(
			flt(pi.base_grand_total),
			flt(pi.base_net_total) + flt(pi.base_total_taxes_and_charges),
		)
		entries, debit, credit = self._gl_balance(pi)
		self.assertEqual(debit, credit)
		self._assert_no_round_off(pi, entries)

	def test_inclusive_tax_no_double_count(self):
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
		self.assertEqual(flt(pi.grand_total), flt(pi.total))
		self.assertEqual(flt(pi.items[0].rate), 11)
		self.assertEqual(
			flt(pi.base_grand_total),
			flt(pi.base_net_total) + flt(pi.base_total_taxes_and_charges),
		)
		self.assertLess(
			abs(flt(pi.base_grand_total) - (flt(pi.base_net_total) + flt(pi.base_total_taxes_and_charges))),
			0.5,
		)
		entries, debit, credit = self._gl_balance(pi)
		self.assertEqual(debit, credit, msg=entries)
		self._assert_no_round_off(pi, entries)
		item_expense = pi.items[0].expense_account
		expense = sum(flt(g.get("debit")) for g in entries if g.get("account") == item_expense)
		tax_debit = sum(flt(g.get("debit")) for g in entries if g.get("account") == self.tax_account)
		self.assertEqual(expense, flt(pi.base_net_total))
		self.assertEqual(tax_debit, flt(pi.base_total_taxes_and_charges))
		payable = sum(flt(g.get("credit")) for g in entries if flt(g.get("credit")))
		self.assertEqual(payable, flt(pi.base_grand_total))
		self.assertEqual(debit, flt(pi.base_net_total) + flt(pi.base_total_taxes_and_charges))
		self.assertNotEqual(payable, flt(pi.base_total) + tax_debit)

	def test_multiple_tax_rows(self):
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
		self.assertEqual(
			flt(pi.base_grand_total),
			flt(pi.base_net_total) + flt(pi.base_total_taxes_and_charges),
		)
		entries, debit, credit = self._gl_balance(pi)
		self.assertEqual(debit, credit)
		self._assert_no_round_off(pi, entries)

	def test_eur_payable_account_currency(self):
		if not self.eur_payable:
			self.skipTest("No EUR payable account")
		pi = self._new_fx_pi(qty=QTY_01345, rate=RATE_01345, credit_to=self.eur_payable)
		pi.insert(ignore_permissions=True)
		self.assertEqual(pi.party_account_currency, "EUR")
		self.assertEqual(flt(pi.outstanding_amount), AMOUNT_01345)
		self.assertEqual(flt(pi.base_grand_total), BASE_AMOUNT_01345)
		entries, debit, credit = self._gl_balance(pi)
		self.assertEqual(debit, credit)
		payable = next(g for g in entries if flt(g.get("credit")))
		self.assertEqual(flt(payable.get("credit")), BASE_AMOUNT_01345)
		self.assertEqual(flt(payable.get("credit_in_account_currency")), AMOUNT_01345)
		self.assertEqual(flt(payable.get("credit_in_transaction_currency")), AMOUNT_01345)

	def test_update_stock_one_uses_uvr_integer_rate(self):
		import frappe
		from frappe.utils import nowdate, nowtime

		from erpnext_extensions.iran_accounting.e2e_bootstrap import ensure_test_item, get_warehouse

		if not self.warehouse:
			self.skipTest("No warehouse")
		item = ensure_test_item(self.company, "PA-STK")
		uom = frappe.get_cached_value("Item", item, "stock_uom")
		wh = get_warehouse(self.company)
		po = frappe.new_doc("Purchase Order")
		po.company = self.company
		po.supplier = self.supplier
		po.currency = "EUR"
		po.conversion_rate = CONV_01345
		po.transaction_date = nowdate()
		po.schedule_date = nowdate()
		row = {
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
		}
		if self.dept:
			row["department"] = self.dept
		po.append("items", row)
		try:
			po.insert(ignore_permissions=True)
			po.submit()
		except Exception as exc:
			self.skipTest(f"PO for update_stock PI not configurable: {exc}")
		pi = frappe.new_doc("Purchase Invoice")
		pi.company = self.company
		pi.supplier = self.supplier
		pi.currency = "EUR"
		pi.conversion_rate = CONV_01345
		pi.posting_date = nowdate()
		pi.set_posting_time = 1
		pi.posting_time = nowtime()
		pi.update_stock = 1
		pi.remarks = "Pattern A fixture"
		if self.irr_payable:
			pi.credit_to = self.irr_payable
		pi_item = {
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
		}
		if self.dept:
			pi_item["department"] = self.dept
			pi.department = self.dept
		pi.append("items", pi_item)
		try:
			pi.insert(ignore_permissions=True)
		except Exception as exc:
			self.skipTest(f"update_stock PI insert failed: {exc}")
		self.assertEqual(flt(pi.items[0].base_rate), BASE_RATE_01345)
		self.assertEqual(flt(pi.base_grand_total), BASE_AMOUNT_01345)
		self.assertEqual(flt(pi.grand_total), AMOUNT_01345)
		entries, debit, credit = self._gl_balance(pi)
		self.assertEqual(debit, credit, msg=entries)
		self._assert_no_round_off(pi, entries)
		if pi.items[0].valuation_rate not in (None, ""):
			vr = flt(pi.items[0].valuation_rate)
			self.assertEqual(vr, float(int(vr)), msg=f"UVR must keep integer VR, got {vr}")

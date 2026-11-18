# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.7.57 — Subcontracting Receipt clearing (111421) must balance under Iran integer SLE."""

from __future__ import annotations

import unittest
from unittest.mock import patch

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.domain.subcontracting_receipt_gl_balance import (
	clearing_account_net,
	expected_scr_sa_delta,
	normalize_subcontracting_receipt_gl_entries,
	uses_scr_clearing_balance_gl,
)

CLEARING = "111421 - واسط کالای امانی (برونسپاری) - E"
SERVICE = "611250 - Subcontracting Service Expense - E"
SA = "621301 - تعدیلات موجودی کالا - E"
INV = "111625 - موجودی محصولات - E"
RM_WH = "111610 - موجودی مشترک مواد - E"
COMPANY = "E"


def _leg(account, *, debit=0.0, credit=0.0):
	return frappe._dict(
		{
			"account": account,
			"debit": debit,
			"credit": credit,
			"debit_in_account_currency": debit,
			"credit_in_account_currency": credit,
			"debit_in_transaction_currency": debit,
			"credit_in_transaction_currency": credit,
			"account_currency": "IRR",
			"cost_center": "Main - E",
			"company": COMPANY,
		}
	)


def _fake_scr(
	*,
	qty: float,
	amount: float,
	service_total: float,
	svd: float,
	rm_amount: float,
	is_return: int = 0,
	expense: str = CLEARING,
	service_account: str = SERVICE,
):
	"""Minimal SCR-like document for unit tests."""
	service_per = flt(service_total) / flt(qty) if qty else 0.0
	item = frappe._dict(
		{
			"name": "scr-item-1",
			"bom": "BOM-X",
			"qty": qty,
			"rate": flt(amount) / flt(qty) if qty else 0.0,
			"amount": amount,
			"service_cost_per_qty": service_per,
			"expense_account": expense,
			"service_expense_account": service_account,
			"warehouse": "Stores - E",
			"secondary_item_type": None,
			"valuation_type": None,
		}
	)
	rm = frappe._dict(
		{
			"rm_item_code": "RM-1",
			"amount": rm_amount,
			"expense_account": expense,
			"reference_name": item.name,
		}
	)
	doc = frappe._dict(
		{
			"doctype": "Subcontracting Receipt",
			"name": "MAT-SCR-TEST",
			"company": COMPANY,
			"is_return": is_return,
			"items": [item],
			"supplied_items": [rm],
			"flags": frappe._dict(),
		}
	)
	return doc, item, svd


def _core_gl_merged(*, svd, amount, service, rm_amount, expense=CLEARING):
	"""Simulate Core make_item_gl_entries after process_gl_map merge on expense.

	divisional_loss = amount − SVD = −Δ when SVD = amount + Δ.
	Merged expense: debit = RM + (−Δ) = RM − Δ; credit = SVD − service.
	"""
	divisional = flt(amount) - flt(svd)  # −Δ
	gl = [
		_leg(INV, debit=svd),
		_leg(
			expense,
			debit=flt(rm_amount) + divisional,  # RM − Δ after negative debit merge effect
			credit=flt(svd) - flt(service),
		),
		_leg(SERVICE, credit=service),
		_leg(RM_WH, credit=rm_amount),
	]
	# SA: credit=divisional (−Δ when SVD>amount) — economically SA net = −divisional = Δ
	if divisional:
		gl.append(_leg(SA, credit=divisional))
	return gl


def _core_gl_unmerged(*, svd, amount, service, rm_amount, expense=CLEARING):
	"""Core legs before process_gl_map."""
	divisional = flt(amount) - flt(svd)
	gl = [
		_leg(INV, debit=svd),
		_leg(expense, credit=flt(svd) - flt(service)),
		_leg(SERVICE, credit=service),
		_leg(RM_WH, credit=rm_amount),
		_leg(expense, debit=rm_amount),
	]
	if divisional:
		gl.append(_leg(SA, credit=divisional))
		gl.append(_leg(expense, debit=divisional))
	return gl


def _sa_net(gl_entries) -> float:
	net = 0.0
	for e in gl_entries:
		if e.account != SA:
			continue
		net += flt(e.debit) - flt(e.credit)
	return flt(net)


def _patch_env(doc, item, svd):
	"""Context patches for normalize — use as `with ExitStack` or nest all three."""
	return (
		patch(
			"erpnext_extensions.iran_accounting.domain.subcontracting_receipt_gl_balance.is_irr_company",
			return_value=True,
		),
		patch(
			"erpnext_extensions.iran_accounting.domain.subcontracting_receipt_gl_balance._fg_sle_stock_value",
			return_value=svd,
		),
		patch(
			"erpnext_extensions.iran_accounting.domain.subcontracting_receipt_gl_balance._stock_adjustment_account",
			return_value=SA,
		),
	)


from contextlib import contextmanager


@contextmanager
def _patched(doc, item, svd):
	p0, p1, p2 = _patch_env(doc, item, svd)
	with p0, p1, p2:
		yield


class TestSCRClearingBalanceV5757(unittest.TestCase):
	"""Regression suite A–G for SCR clearing balance under Iran integer valuation."""

	# --- Test A: Exact integer valuation ---
	def test_a_exact_integer_valuation_balanced(self):
		qty, amount, service, svd, rm = 10.0, 10000.0, 1000.0, 10000.0, 9000.0
		doc, item, _ = _fake_scr(qty=qty, amount=amount, service_total=service, svd=svd, rm_amount=rm)
		gl = _core_gl_merged(svd=svd, amount=amount, service=service, rm_amount=rm)
		with _patched(doc, item, svd):
			out = normalize_subcontracting_receipt_gl_entries(doc, gl)
		self.assertEqual(clearing_account_net(out, CLEARING), 0.0)
		self.assertEqual(_sa_net(out), 0.0)

	# --- Test B: Fractional service cost (reference numbers) ---
	def test_b_fractional_service_cost_reference_case(self):
		# qty=1962, service=1_230_000, amount=1_239_193_140, SVD=1_239_193_314 (Δ=174)
		qty = 1962.0
		service = 1_230_000.0
		amount = 1_239_193_140.0
		svd = 1_239_193_314.0
		rm = amount - service  # 1_237_963_140
		delta = svd - amount  # 174
		doc, item, _ = _fake_scr(qty=qty, amount=amount, service_total=service, svd=svd, rm_amount=rm)
		gl = _core_gl_merged(svd=svd, amount=amount, service=service, rm_amount=rm)

		# Precondition: Core shows 2Δ imbalance
		self.assertEqual(clearing_account_net(gl, CLEARING), -2 * delta)

		with _patched(doc, item, svd):
			out = normalize_subcontracting_receipt_gl_entries(doc, gl)

		self.assertEqual(clearing_account_net(out, CLEARING), 0.0)
		# Debit == credit == RM / (amount − service)
		for e in out:
			if e.account == CLEARING:
				self.assertEqual(flt(e.debit), rm)
				self.assertEqual(flt(e.credit), rm)
		# Service unchanged
		svc = sum(flt(e.credit) - flt(e.debit) for e in out if e.account == SERVICE)
		self.assertEqual(svc, service)
		# SA = valuation gain (credit Δ) so voucher balances with INV=SVD
		self.assertEqual(_sa_net(out), -delta)
		# Voucher totals balance
		td = sum(flt(e.debit) for e in out)
		tc = sum(flt(e.credit) for e in out)
		self.assertEqual(td, tc)

	# --- Test C: IRR rounding difference on valuation rate ---
	def test_c_irr_rounding_difference(self):
		# Document amounts are integer IRR; SLE integer VR bumps SVD by +1
		qty, amount, service = 100.0, 50_000.0, 1_000.0
		svd = 50_001.0
		rm = amount - service
		delta = svd - amount
		doc, item, _ = _fake_scr(qty=qty, amount=amount, service_total=service, svd=svd, rm_amount=rm)
		gl = _core_gl_merged(svd=svd, amount=amount, service=service, rm_amount=rm)
		self.assertEqual(clearing_account_net(gl, CLEARING), -2 * delta)
		with _patched(doc, item, svd):
			out = normalize_subcontracting_receipt_gl_entries(doc, gl)
		self.assertEqual(clearing_account_net(out, CLEARING), 0.0)
		self.assertEqual(_sa_net(out), -delta)

	# --- Test D: Stock Adjustment reflected without clearing imbalance ---
	def test_d_stock_adjustment_preserved(self):
		qty, amount, service, svd = 1962.0, 1_239_193_140.0, 1_230_000.0, 1_239_193_314.0
		rm = amount - service
		delta = svd - amount
		doc, item, _ = _fake_scr(qty=qty, amount=amount, service_total=service, svd=svd, rm_amount=rm)
		gl = _core_gl_merged(svd=svd, amount=amount, service=service, rm_amount=rm)
		with _patched(doc, item, svd):
			self.assertEqual(expected_scr_sa_delta(doc), -delta)
			out = normalize_subcontracting_receipt_gl_entries(doc, gl)
		self.assertEqual(clearing_account_net(out, CLEARING), 0.0)
		self.assertEqual(_sa_net(out), -delta)
		# Inventory debit unchanged
		inv = sum(flt(e.debit) for e in out if e.account == INV)
		self.assertEqual(inv, svd)
		self.assertEqual(sum(flt(e.debit) for e in out), sum(flt(e.credit) for e in out))

	# --- Test E: Repost / re-normalize does not recreate imbalance ---
	def test_e_repost_idempotent(self):
		qty, amount, service, svd = 1962.0, 1_239_193_140.0, 1_230_000.0, 1_239_193_314.0
		rm = amount - service
		doc, item, _ = _fake_scr(qty=qty, amount=amount, service_total=service, svd=svd, rm_amount=rm)
		gl = _core_gl_merged(svd=svd, amount=amount, service=service, rm_amount=rm)
		with _patched(doc, item, svd):
			out1 = normalize_subcontracting_receipt_gl_entries(doc, gl)
			# Fresh Core map on repost (flags cleared as new doc load would)
			doc.flags = frappe._dict()
			gl2 = _core_gl_merged(svd=svd, amount=amount, service=service, rm_amount=rm)
			out2 = normalize_subcontracting_receipt_gl_entries(doc, gl2)
		self.assertEqual(clearing_account_net(out1, CLEARING), 0.0)
		self.assertEqual(clearing_account_net(out2, CLEARING), 0.0)
		self.assertEqual(_sa_net(out1), _sa_net(out2))

	# --- Test F: Repeated execution does not duplicate balancing ---
	def test_f_repeated_execution_no_duplicate(self):
		qty, amount, service, svd = 1962.0, 1_239_193_140.0, 1_230_000.0, 1_239_193_314.0
		rm = amount - service
		doc, item, _ = _fake_scr(qty=qty, amount=amount, service_total=service, svd=svd, rm_amount=rm)
		gl = _core_gl_merged(svd=svd, amount=amount, service=service, rm_amount=rm)
		with _patched(doc, item, svd):
			out1 = normalize_subcontracting_receipt_gl_entries(doc, list(gl))
			# Same doc.flags still set — second call is no-op
			before = [(e.account, flt(e.debit), flt(e.credit)) for e in out1]
			out2 = normalize_subcontracting_receipt_gl_entries(doc, out1)
			after = [(e.account, flt(e.debit), flt(e.credit)) for e in out2]
		self.assertEqual(before, after)
		self.assertEqual(len(out2), len(out1))

	# --- Test G: Negative and positive valuation differences ---
	def test_g_positive_and_negative_delta(self):
		# Positive Δ (SVD > amount) — reference shape
		amount, service, svd = 100_000.0, 5_000.0, 100_050.0
		rm = amount - service
		doc, item, _ = _fake_scr(qty=10, amount=amount, service_total=service, svd=svd, rm_amount=rm)
		gl = _core_gl_merged(svd=svd, amount=amount, service=service, rm_amount=rm)
		with _patched(doc, item, svd):
			out = normalize_subcontracting_receipt_gl_entries(doc, gl)
		self.assertEqual(clearing_account_net(out, CLEARING), 0.0)
		self.assertEqual(_sa_net(out), -50.0)  # credit gain
		self.assertEqual(sum(flt(e.debit) for e in out), sum(flt(e.credit) for e in out))

		# Negative Δ (SVD < amount) — valuation loss → debit SA
		amount2, service2, svd2 = 100_000.0, 5_000.0, 99_970.0
		rm2 = amount2 - service2
		doc2, item2, _ = _fake_scr(qty=10, amount=amount2, service_total=service2, svd=svd2, rm_amount=rm2)
		gl2 = _core_gl_merged(svd=svd2, amount=amount2, service=service2, rm_amount=rm2)
		with _patched(doc2, item2, svd2):
			out2 = normalize_subcontracting_receipt_gl_entries(doc2, gl2)
		self.assertEqual(clearing_account_net(out2, CLEARING), 0.0)
		self.assertEqual(_sa_net(out2), amount2 - svd2)  # debit loss
		self.assertEqual(sum(flt(e.debit) for e in out2), sum(flt(e.credit) for e in out2))

	def test_unmerged_map_also_balances(self):
		qty, amount, service, svd = 1962.0, 1_239_193_140.0, 1_230_000.0, 1_239_193_314.0
		rm = amount - service
		delta = svd - amount
		doc, item, _ = _fake_scr(qty=qty, amount=amount, service_total=service, svd=svd, rm_amount=rm)
		gl = _core_gl_unmerged(svd=svd, amount=amount, service=service, rm_amount=rm)
		self.assertEqual(clearing_account_net(gl, CLEARING), -2 * delta)
		with _patched(doc, item, svd):
			out = normalize_subcontracting_receipt_gl_entries(doc, gl)
		self.assertEqual(clearing_account_net(out, CLEARING), 0.0)
		self.assertEqual(_sa_net(out), -delta)
		self.assertEqual(sum(flt(e.debit) for e in out), sum(flt(e.credit) for e in out))

	def test_returns_and_non_scr_skipped(self):
		doc, item, svd = _fake_scr(
			qty=10, amount=1000, service_total=100, svd=1000, rm_amount=900, is_return=1
		)
		gl = [_leg(CLEARING, debit=900, credit=901)]
		with patch(
			"erpnext_extensions.iran_accounting.domain.subcontracting_receipt_gl_balance.is_irr_company",
			return_value=True,
		):
			self.assertFalse(uses_scr_clearing_balance_gl(doc))
			out = normalize_subcontracting_receipt_gl_entries(doc, gl)
		self.assertEqual(clearing_account_net(out, CLEARING), -1.0)


if __name__ == "__main__":
	unittest.main()

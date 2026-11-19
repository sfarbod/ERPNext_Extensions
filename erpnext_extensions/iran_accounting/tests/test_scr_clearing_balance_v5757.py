# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.7.57 — Subcontracting Receipt clearing (111421) must balance; net-zero rows dropped."""

from __future__ import annotations

import unittest
from contextlib import contextmanager
from unittest.mock import patch

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.domain.subcontracting_receipt_gl_balance import (
	clearing_account_net,
	clearing_account_present,
	enforce_scr_clearing_expense_accounts,
	expected_scr_sa_delta,
	normalize_subcontracting_receipt_gl_entries,
	uses_scr_clearing_balance_gl,
)

CLEARING = "111421 - واسط کالای امانی (برونسپاری) - E"
SERVICE = "611250 - Subcontracting Service Expense - E"
SERVICE_611218 = "611218 - هزینه خدمات - E"
SA = "621301 - تعدیلات موجودی کالا - E"
INV = "111625 - موجودی محصولات - E"
INV_111605 = "111605 - موجودی نیمه ساخته - E"
RM_WH = "111610 - موجودی مشترک مواد - E"
RM_WH_111635 = "111635 - کالای امانی ما نزد دیگران(موجودی محصول) - E"
RM_EXP_611233 = "611233 - هزینه تست وآزمایشات - E"
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
	divisional = flt(amount) - flt(svd)
	gl = [
		_leg(INV, debit=svd),
		_leg(
			expense,
			debit=flt(rm_amount) + divisional,
			credit=flt(svd) - flt(service),
		),
		_leg(SERVICE, credit=service),
		_leg(RM_WH, credit=rm_amount),
	]
	if divisional:
		gl.append(_leg(SA, credit=divisional))
	return gl


def _core_gl_unmerged(*, svd, amount, service, rm_amount, expense=CLEARING):
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


def _voucher_balanced(gl_entries) -> bool:
	return abs(sum(flt(e.debit) for e in gl_entries) - sum(flt(e.credit) for e in gl_entries)) < 1e-6


@contextmanager
def _patched(doc, item, svd):
	with (
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
		patch(
			"erpnext_extensions.iran_accounting.domain.manufacture_expense_account.get_company_currency",
			return_value="IRR",
		),
		patch(
			"erpnext_extensions.iran_accounting.domain.manufacture_expense_account.round_currency",
			side_effect=lambda v, ccy: round(float(v)),
		),
	):
		yield


class TestSCRClearingBalanceV5757(unittest.TestCase):
	"""Clearing 111421 always nets to zero; net-zero rows are not persisted."""

	def test_01_debit_equals_credit_net_zero_no_rows(self):
		"""Equal nonzero clearing movement → dropped (Iran drop_net_zero convention)."""
		qty, amount, service, svd = 1962.0, 1_239_193_140.0, 1_230_000.0, 1_239_193_314.0
		rm = amount - service
		delta = svd - amount
		doc, item, _ = _fake_scr(qty=qty, amount=amount, service_total=service, svd=svd, rm_amount=rm)
		gl = _core_gl_merged(svd=svd, amount=amount, service=service, rm_amount=rm)
		self.assertEqual(clearing_account_net(gl, CLEARING), -2 * delta)
		with _patched(doc, item, svd):
			out = normalize_subcontracting_receipt_gl_entries(doc, gl)
		self.assertFalse(clearing_account_present(out, CLEARING))
		self.assertEqual(clearing_account_net(out, CLEARING), 0.0)
		self.assertEqual(_sa_net(out), -delta)
		self.assertTrue(_voucher_balanced(out))

	def test_02_zero_debit_credit_produce_no_rows(self):
		doc, item, svd = _fake_scr(qty=10, amount=1000, service_total=100, svd=1000, rm_amount=900)
		gl = [
			_leg(INV, debit=1000),
			_leg(CLEARING, debit=0, credit=0),
			_leg(SERVICE, credit=100),
			_leg(RM_WH, credit=900),
		]
		with _patched(doc, item, svd):
			out = normalize_subcontracting_receipt_gl_entries(doc, gl)
		self.assertFalse(clearing_account_present(out, CLEARING))
		self.assertTrue(_voucher_balanced(out))

	def test_03_equal_nonzero_debit_credit_dropped(self):
		"""Rule D: follow drop_net_zero_merged_gl_rows — omit equal D/C clearing row."""
		doc, item, svd = _fake_scr(qty=10, amount=10000, service_total=1000, svd=10000, rm_amount=9000)
		gl = [
			_leg(INV, debit=10000),
			_leg(CLEARING, debit=9000, credit=9000),
			_leg(SERVICE, credit=1000),
			_leg(RM_WH, credit=9000),
		]
		with _patched(doc, item, svd):
			out = normalize_subcontracting_receipt_gl_entries(doc, gl)
		self.assertFalse(clearing_account_present(out, CLEARING))
		self.assertTrue(_voucher_balanced(out))

	def test_04_positive_irr_rounding_delta(self):
		qty, amount, service, svd = 100.0, 50_000.0, 1_000.0, 50_001.0
		rm = amount - service
		delta = svd - amount
		doc, item, _ = _fake_scr(qty=qty, amount=amount, service_total=service, svd=svd, rm_amount=rm)
		gl = _core_gl_merged(svd=svd, amount=amount, service=service, rm_amount=rm)
		with _patched(doc, item, svd):
			out = normalize_subcontracting_receipt_gl_entries(doc, gl)
		self.assertFalse(clearing_account_present(out, CLEARING))
		self.assertEqual(_sa_net(out), -delta)
		self.assertTrue(_voucher_balanced(out))
		self.assertEqual(sum(flt(e.debit) for e in out if e.account == INV), svd)

	def test_05_negative_irr_rounding_delta(self):
		amount, service, svd = 100_000.0, 5_000.0, 99_970.0
		rm = amount - service
		doc, item, _ = _fake_scr(qty=10, amount=amount, service_total=service, svd=svd, rm_amount=rm)
		gl = _core_gl_merged(svd=svd, amount=amount, service=service, rm_amount=rm)
		with _patched(doc, item, svd):
			out = normalize_subcontracting_receipt_gl_entries(doc, gl)
		self.assertFalse(clearing_account_present(out, CLEARING))
		self.assertEqual(_sa_net(out), amount - svd)
		self.assertTrue(_voucher_balanced(out))

	def test_06_fractional_service_cost_reference(self):
		qty, amount, service, svd = 1962.0, 1_239_193_140.0, 1_230_000.0, 1_239_193_314.0
		rm = amount - service
		doc, item, _ = _fake_scr(qty=qty, amount=amount, service_total=service, svd=svd, rm_amount=rm)
		gl = _core_gl_merged(svd=svd, amount=amount, service=service, rm_amount=rm)
		with _patched(doc, item, svd):
			out = normalize_subcontracting_receipt_gl_entries(doc, gl)
		svc = sum(flt(e.credit) - flt(e.debit) for e in out if e.account == SERVICE)
		self.assertEqual(svc, service)
		self.assertFalse(clearing_account_present(out, CLEARING))
		self.assertTrue(_voucher_balanced(out))

	def test_07_repeated_repost_idempotent(self):
		qty, amount, service, svd = 1962.0, 1_239_193_140.0, 1_230_000.0, 1_239_193_314.0
		rm = amount - service
		doc, item, _ = _fake_scr(qty=qty, amount=amount, service_total=service, svd=svd, rm_amount=rm)
		with _patched(doc, item, svd):
			out1 = normalize_subcontracting_receipt_gl_entries(
				doc, _core_gl_merged(svd=svd, amount=amount, service=service, rm_amount=rm)
			)
			doc.flags = frappe._dict()
			out2 = normalize_subcontracting_receipt_gl_entries(
				doc, _core_gl_merged(svd=svd, amount=amount, service=service, rm_amount=rm)
			)
		self.assertFalse(clearing_account_present(out1, CLEARING))
		self.assertFalse(clearing_account_present(out2, CLEARING))
		self.assertEqual(_sa_net(out1), _sa_net(out2))
		self.assertTrue(_voucher_balanced(out1))
		self.assertTrue(_voucher_balanced(out2))

	def test_08_reversal_reconstruction_preserves_rule(self):
		"""Second normalize on same in-memory map is no-op (flag); fresh Core map re-balances."""
		qty, amount, service, svd = 1962.0, 1_239_193_140.0, 1_230_000.0, 1_239_193_314.0
		rm = amount - service
		doc, item, _ = _fake_scr(qty=qty, amount=amount, service_total=service, svd=svd, rm_amount=rm)
		with _patched(doc, item, svd):
			out1 = normalize_subcontracting_receipt_gl_entries(
				doc, _core_gl_merged(svd=svd, amount=amount, service=service, rm_amount=rm)
			)
			before = [(e.account, flt(e.debit), flt(e.credit)) for e in out1]
			out2 = normalize_subcontracting_receipt_gl_entries(doc, out1)
			after = [(e.account, flt(e.debit), flt(e.credit)) for e in out2]
		self.assertEqual(before, after)
		self.assertFalse(clearing_account_present(out2, CLEARING))

	def test_09_existing_zero_value_rows_not_persisted(self):
		doc, item, svd = _fake_scr(qty=10, amount=1000, service_total=100, svd=1000, rm_amount=900)
		gl = [
			_leg(INV, debit=1000),
			_leg(CLEARING, debit=0.0, credit=0.0),
			_leg(CLEARING, debit=0.0, credit=0.0),
			_leg(SERVICE, credit=100),
			_leg(RM_WH, credit=900),
		]
		with _patched(doc, item, svd):
			out = normalize_subcontracting_receipt_gl_entries(doc, gl)
		self.assertFalse(clearing_account_present(out, CLEARING))
		for e in out:
			self.assertFalse(abs(flt(e.debit)) < 0.5 and abs(flt(e.credit)) < 0.5)

	def test_10_overall_voucher_balanced(self):
		qty, amount, service, svd = 1962.0, 1_239_193_140.0, 1_230_000.0, 1_239_193_314.0
		rm = amount - service
		doc, item, _ = _fake_scr(qty=qty, amount=amount, service_total=service, svd=svd, rm_amount=rm)
		gl = _core_gl_merged(svd=svd, amount=amount, service=service, rm_amount=rm)
		with _patched(doc, item, svd):
			self.assertEqual(expected_scr_sa_delta(doc), -(svd - amount))
			out = normalize_subcontracting_receipt_gl_entries(doc, gl)
		self.assertTrue(_voucher_balanced(out))
		self.assertGreater(len(out), 1)

	def test_unmerged_map_drops_clearing(self):
		qty, amount, service, svd = 1962.0, 1_239_193_140.0, 1_230_000.0, 1_239_193_314.0
		rm = amount - service
		delta = svd - amount
		doc, item, _ = _fake_scr(qty=qty, amount=amount, service_total=service, svd=svd, rm_amount=rm)
		gl = _core_gl_unmerged(svd=svd, amount=amount, service=service, rm_amount=rm)
		with _patched(doc, item, svd):
			out = normalize_subcontracting_receipt_gl_entries(doc, gl)
		self.assertFalse(clearing_account_present(out, CLEARING))
		self.assertEqual(_sa_net(out), -delta)
		self.assertTrue(_voucher_balanced(out))

	def test_credit_only_doubled_delta_with_separate_rm_accounts(self):
		"""Mismatched RM expense is remapped onto clearing, then clearing is dropped.

		Live failure mode (MAT-SCR-2026-00015): Cr clearing + Dr 611233. Policy
		remaps the RM expense debit onto 111421, rewrites credit to document
		material, credits SA for Δ, and removes balanced clearing rows.
		"""
		qty = 2015.0
		amount = 3_615_407_470.0
		service = 145_122_080.0
		svd = 3_615_407_705.0
		rm = amount - service
		delta = svd - amount
		doc, item, _ = _fake_scr(
			qty=qty,
			amount=amount,
			service_total=service,
			svd=svd,
			rm_amount=rm,
			expense=CLEARING,
			service_account=SERVICE_611218,
		)
		doc.supplied_items[0].expense_account = RM_EXP_611233
		doc_credit = amount - service
		gl = [
			_leg(INV_111605, debit=svd),
			_leg(CLEARING, credit=doc_credit + 2 * delta),
			_leg(SERVICE_611218, credit=service),
			_leg(SA, debit=delta),
			_leg(RM_WH_111635, credit=rm),
			_leg(RM_EXP_611233, debit=rm),
		]
		self.assertEqual(clearing_account_net(gl, CLEARING), -(doc_credit + 2 * delta))
		self.assertTrue(_voucher_balanced(gl))
		with _patched(doc, item, svd):
			out = normalize_subcontracting_receipt_gl_entries(doc, gl)
		self.assertFalse(clearing_account_present(out, CLEARING))
		self.assertFalse(clearing_account_present(out, RM_EXP_611233))
		self.assertEqual(_sa_net(out), -delta)
		self.assertEqual(sum(flt(e.credit) for e in out if e.account == SERVICE_611218), service)
		self.assertEqual(sum(flt(e.debit) for e in out if e.account == INV_111605), svd)
		self.assertEqual(sum(flt(e.credit) for e in out if e.account == RM_WH_111635), rm)
		self.assertTrue(_voucher_balanced(out))
		# After drop, RM is only on warehouse credit — no duplicate clearing/611233.
		self.assertEqual(sum(flt(e.debit) for e in out), svd)
		self.assertEqual(sum(flt(e.credit) for e in out), svd)

	def test_A_matching_clearing_accounts_drop_111421(self):
		"""Test A — FG expense = RM expense = clearing → rows removed."""
		qty, amount, service, svd = 100.0, 50_000.0, 1_000.0, 50_001.0
		rm = amount - service
		doc, item, _ = _fake_scr(qty=qty, amount=amount, service_total=service, svd=svd, rm_amount=rm)
		self.assertEqual(doc.get("items")[0].expense_account, CLEARING)
		self.assertEqual(doc.get("supplied_items")[0].expense_account, CLEARING)
		gl = _core_gl_unmerged(svd=svd, amount=amount, service=service, rm_amount=rm)
		before_debit = sum(flt(e.debit) for e in gl if e.account == CLEARING)
		before_credit = sum(flt(e.credit) for e in gl if e.account == CLEARING)
		# Unmerged Core: debit=rm (+ optional |Δ| mirror); credit=SVD−service.
		self.assertGreater(before_debit, 0)
		self.assertGreater(before_credit, 0)
		with _patched(doc, item, svd):
			out = normalize_subcontracting_receipt_gl_entries(doc, gl)
		self.assertFalse(clearing_account_present(out, CLEARING))
		self.assertEqual(clearing_account_net(out, CLEARING), 0.0)

	def test_B_mismatched_accounts_enforced_and_normalized(self):
		"""Test B — FG clearing vs RM 611233: enforce field + normalize GL."""
		doc, item, svd = _fake_scr(qty=10, amount=10000, service_total=1000, svd=10000, rm_amount=9000)
		doc.supplied_items[0].expense_account = RM_EXP_611233
		with patch(
			"erpnext_extensions.iran_accounting.domain.subcontracting_receipt_gl_balance.is_irr_company",
			return_value=True,
		):
			changed = enforce_scr_clearing_expense_accounts(doc)
		self.assertEqual(doc.supplied_items[0].expense_account, CLEARING)
		self.assertEqual(len(changed), 1)

		# GL path: even if document field were still wrong, remap + drop.
		doc.supplied_items[0].expense_account = RM_EXP_611233
		gl = [
			_leg(INV, debit=10000),
			_leg(CLEARING, credit=9000),
			_leg(SERVICE, credit=1000),
			_leg(RM_WH, credit=9000),
			_leg(RM_EXP_611233, debit=9000),
		]
		with _patched(doc, item, svd):
			out = normalize_subcontracting_receipt_gl_entries(doc, gl)
		self.assertFalse(clearing_account_present(out, CLEARING))
		self.assertFalse(clearing_account_present(out, RM_EXP_611233))
		self.assertTrue(_voucher_balanced(out))

	def test_C_target_transaction_values(self):
		"""Permanent fixture: MAT-SCR-2026-00015 verified Development values.

		Does not load the live voucher; reproduces qty / service / SVD / SA
		residual and account roles so the release gate stays deterministic.
		"""
		qty = 2015.0
		amount = 3_615_407_470.0
		service = 145_122_080.0
		svd = 3_615_407_705.0
		rm = 3_470_285_390.0
		delta = 235.0
		self.assertEqual(rm, amount - service)
		self.assertEqual(delta, svd - amount)
		doc, item, _ = _fake_scr(
			qty=qty,
			amount=amount,
			service_total=service,
			svd=svd,
			rm_amount=rm,
			expense=CLEARING,
			service_account=SERVICE_611218,
		)
		doc.supplied_items[0].expense_account = CLEARING
		gl = _core_gl_unmerged(
			svd=svd, amount=amount, service=service, rm_amount=rm, expense=CLEARING
		)
		# Retarget INV / RM WH / service accounts to live chart names.
		for e in gl:
			if e.account == INV:
				e.account = INV_111605
			elif e.account == RM_WH:
				e.account = RM_WH_111635
			elif e.account == SERVICE:
				e.account = SERVICE_611218
		with _patched(doc, item, svd):
			out = normalize_subcontracting_receipt_gl_entries(doc, gl)

		def _amt(account, side):
			return sum(flt(getattr(e, side)) for e in out if e.account == account)

		self.assertFalse(clearing_account_present(out, CLEARING))
		self.assertFalse(clearing_account_present(out, RM_EXP_611233))
		self.assertEqual(_amt(INV_111605, "debit"), svd)
		self.assertEqual(_amt(RM_WH_111635, "credit"), rm)
		self.assertEqual(_amt(SERVICE_611218, "credit"), service)
		self.assertEqual(_amt(SA, "credit"), delta)
		self.assertEqual(sum(flt(e.debit) for e in out), svd)
		self.assertEqual(sum(flt(e.credit) for e in out), svd)
		self.assertTrue(_voucher_balanced(out))

	def test_D_normalization_idempotent(self):
		"""Test D — repeated normalize yields identical clearing-free output."""
		qty, amount, service, svd = 2015.0, 3_615_407_470.0, 145_122_080.0, 3_615_407_705.0
		rm = amount - service
		doc, item, _ = _fake_scr(
			qty=qty,
			amount=amount,
			service_total=service,
			svd=svd,
			rm_amount=rm,
			expense=CLEARING,
			service_account=SERVICE_611218,
		)
		doc.supplied_items[0].expense_account = RM_EXP_611233

		def _build():
			return [
				_leg(INV_111605, debit=svd),
				_leg(CLEARING, credit=(amount - service) + 2 * (svd - amount)),
				_leg(SERVICE_611218, credit=service),
				_leg(SA, debit=svd - amount),
				_leg(RM_WH_111635, credit=rm),
				_leg(RM_EXP_611233, debit=rm),
			]

		with _patched(doc, item, svd):
			out1 = normalize_subcontracting_receipt_gl_entries(doc, _build())
			doc.flags = frappe._dict()
			out2 = normalize_subcontracting_receipt_gl_entries(doc, _build())
		shape = lambda rows: sorted((e.account, flt(e.debit), flt(e.credit)) for e in rows)
		self.assertEqual(shape(out1), shape(out2))
		self.assertFalse(clearing_account_present(out1, CLEARING))
		self.assertEqual(sum(flt(e.credit) for e in out1 if e.account == SERVICE_611218), service)
		self.assertEqual(sum(flt(e.credit) for e in out1 if e.account == RM_WH_111635), rm)

	def test_E_non_iran_and_returns_untouched(self):
		"""Test E — non-Iran / returns skip policy."""
		doc, item, svd = _fake_scr(qty=10, amount=1000, service_total=100, svd=1000, rm_amount=900)
		doc.supplied_items[0].expense_account = RM_EXP_611233
		gl = [_leg(CLEARING, credit=900), _leg(RM_EXP_611233, debit=900)]
		with patch(
			"erpnext_extensions.iran_accounting.domain.subcontracting_receipt_gl_balance.is_irr_company",
			return_value=False,
		):
			self.assertFalse(uses_scr_clearing_balance_gl(doc))
			self.assertEqual(enforce_scr_clearing_expense_accounts(doc), [])
			self.assertEqual(doc.supplied_items[0].expense_account, RM_EXP_611233)
			out = normalize_subcontracting_receipt_gl_entries(doc, list(gl))
		self.assertTrue(clearing_account_present(out, CLEARING))

	def test_F_multiple_supplied_rm_rows_enforced(self):
		"""Multiple RM rows all align to FG clearing on enforce."""
		doc, item, svd = _fake_scr(qty=10, amount=10000, service_total=1000, svd=10000, rm_amount=5000)
		doc.supplied_items.append(
			frappe._dict(
				{
					"rm_item_code": "RM-2",
					"amount": 4000.0,
					"expense_account": RM_EXP_611233,
					"reference_name": item.name,
				}
			)
		)
		doc.supplied_items[0].expense_account = RM_EXP_611233
		with patch(
			"erpnext_extensions.iran_accounting.domain.subcontracting_receipt_gl_balance.is_irr_company",
			return_value=True,
		):
			changed = enforce_scr_clearing_expense_accounts(doc)
		self.assertEqual(len(changed), 2)
		for rm in doc.get("supplied_items"):
			self.assertEqual(rm.expense_account, CLEARING)

	def test_returns_skipped(self):
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
		self.assertTrue(clearing_account_present(out, CLEARING))

	def test_G_lcv_folded_clearing_already_balanced_is_dropped(self):
		"""LCV folded into clearing (debit==credit==RM) must not be rewritten to pre-LCV credit.

		Mirrors MAT-SCR-2026-00023: amount−service is pre-LCV material, while
		process_gl_map already nets clearing at RM after a negative service LCV.
		"""
		qty = 1922.0
		rm = 932088501.0
		service_pre_lcv = 235996614.0
		lcv = -90875229.0
		amount = rm + service_pre_lcv + lcv  # post-LCV FG amount / SLE
		svd = amount
		# Merged Core+LCV map: clearing already balanced at RM.
		gl = [
			_leg(INV_111605, debit=svd),
			_leg(CLEARING, debit=rm, credit=rm),
			_leg(SERVICE_611218, credit=service_pre_lcv + lcv),
			_leg(RM_WH_111635, credit=rm),
		]
		doc, item, _ = _fake_scr(
			qty=qty,
			amount=amount,
			service_total=service_pre_lcv,
			svd=svd,
			rm_amount=rm,
			service_account=SERVICE_611218,
		)
		with patch(
			"erpnext_extensions.iran_accounting.domain.subcontracting_receipt_gl_balance.is_irr_company",
			return_value=True,
		), patch(
			"erpnext_extensions.iran_accounting.domain.subcontracting_receipt_gl_balance._fg_sle_stock_value",
			return_value=svd,
		):
			out = normalize_subcontracting_receipt_gl_entries(doc, gl)
		self.assertFalse(clearing_account_present(out, CLEARING))
		self.assertEqual(sum(flt(e.debit) for e in out if e.account == INV_111605), svd)
		self.assertEqual(sum(flt(e.credit) for e in out if e.account == RM_WH_111635), rm)
		self.assertEqual(
			sum(flt(e.credit) for e in out if e.account == SERVICE_611218),
			service_pre_lcv + lcv,
		)
		self.assertAlmostEqual(sum(flt(e.debit) for e in out), sum(flt(e.credit) for e in out))


if __name__ == "__main__":
	unittest.main()

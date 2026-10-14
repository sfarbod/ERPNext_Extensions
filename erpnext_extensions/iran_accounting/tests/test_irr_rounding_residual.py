# Copyright (c) 2026, ERPNext Extensions contributors
"""IRR rate rounding residual → Company Round Off Account (unit tests, no DB submit)."""

from __future__ import annotations

import unittest
from contextlib import contextmanager
from unittest import mock

from erpnext_extensions.iran_accounting.domain.irr_rounding_residual import (
	IRR_INVENTORY_VALUATION_RESIDUAL_MARKER,
	IRR_INVENTORY_VALUATION_RESIDUAL_REMARK,
	IRR_RATE_ROUNDING_RESIDUAL_MARKER,
	IRR_RATE_ROUNDING_RESIDUAL_REMARK,
	apply_irr_rate_rounding_residual_gl,
	collect_stock_entry_residuals,
	compute_rounding_residual,
	expected_round_off_gl_totals,
	is_irr_inventory_valuation_residual_gl,
	is_irr_rate_rounding_residual_gl,
	rate_derived_amount,
	round_off_signed_debit,
	strip_irr_rate_rounding_residual_gl,
)
from erpnext_extensions.iran_accounting.tests.hardening.fixtures import (
	STE_03516_AMOUNT,
	STE_03516_INT_RATE,
	STE_03516_QTY,
	STE_03516_RAW_RATE,
)


class _Row:
	def __init__(self, **kw):
		self.__dict__.update(kw)

	def get(self, k, default=None):
		return self.__dict__.get(k, default)


class _Doc:
	def __init__(self, **kw):
		self.__dict__.update(kw)

	def get(self, k, default=None):
		return self.__dict__.get(k, default)


@contextmanager
def _patches(account="SA - T", cost_center="CC-RO", round_off_account="RO - T"):
	"""Patch residual masters. Stock Entry inventory policy uses Stock Adjustment."""

	def resolve_sa(company, require=True):
		if require and not account:
			raise Exception("missing Stock Adjustment Account")
		if require and not cost_center:
			raise Exception("missing Residual Cost Center")
		return {"account": account, "cost_center": cost_center}

	def resolve_ro(company, require=True):
		if require and not round_off_account:
			raise Exception("missing Round Off Account")
		if require and not cost_center:
			raise Exception("missing Round Off Cost Center")
		return {"account": round_off_account, "cost_center": cost_center}

	def is_stock(acc):
		return acc == "Stock - T"

	with (
		mock.patch(
			"erpnext_extensions.iran_accounting.domain.irr_rounding_residual.is_irr_company",
			return_value=True,
		),
		mock.patch(
			"erpnext_extensions.iran_accounting.domain.irr_rounding_residual.get_company_currency",
			return_value="IRR",
		),
		mock.patch(
			"erpnext_extensions.iran_accounting.domain.irr_residual_classification.is_irr_company",
			return_value=True,
		),
		mock.patch(
			"erpnext_extensions.iran_accounting.domain.irr_residual_classification.get_company_currency",
			return_value="IRR",
		),
		mock.patch(
			"erpnext_extensions.iran_accounting.domain.irr_rounding_residual.resolve_company_stock_adjustment_residual",
			side_effect=resolve_sa,
		),
		mock.patch(
			"erpnext_extensions.iran_accounting.domain.irr_rounding_residual.validate_stock_adjustment_residual_configuration",
			return_value=None,
		),
		mock.patch(
			"erpnext_extensions.iran_accounting.domain.irr_rounding_residual.resolve_company_round_off",
			side_effect=resolve_ro,
		),
		mock.patch(
			"erpnext_extensions.iran_accounting.domain.irr_rounding_residual.validate_round_off_configuration",
			return_value=None,
		),
		mock.patch(
			"erpnext_extensions.iran_accounting.domain.irr_rounding_residual._is_stock_account",
			side_effect=is_stock,
		),
		mock.patch(
			"erpnext_extensions.iran_accounting.domain.irr_rounding_residual._populate_round_off_dimensions",
			return_value=None,
		),
		mock.patch(
			"erpnext_extensions.iran_accounting.domain.irr_residual_classification.resolve_round_off_dimensions",
			return_value={},
		),
		mock.patch(
			"erpnext_extensions.iran_accounting.domain.irr_residual_classification._dimension_fieldnames",
			return_value=[],
		),
		mock.patch(
			"erpnext_extensions.iran_accounting.zero_value_transfer._should_force_balanced_transfer_gl",
			return_value=False,
		),
		mock.patch(
			"frappe.get_cached_value",
			side_effect=lambda *a, **k: (
				round_off_account if a and a[-1] == "round_off_account" else None
			),
		),
	):
		yield


def _residual_rows(gl):
	return [
		e
		for e in gl
		if IRR_INVENTORY_VALUATION_RESIDUAL_REMARK in (e.get("remarks") or "")
		or IRR_RATE_ROUNDING_RESIDUAL_REMARK in (e.get("remarks") or "")
	]


class TestIRRRoundingResidualFormula(unittest.TestCase):
	def test_zero_residual_no_round_off(self):
		self.assertEqual(rate_derived_amount(7, 196, "IRR"), 1372)
		self.assertEqual(compute_rounding_residual(1372, 7, 196, "IRR"), 0)

	def test_negative_residual_incoming_debits_round_off(self):
		residual = compute_rounding_residual(1371, 7, 196, "IRR")
		self.assertEqual(residual, -1)
		self.assertEqual(round_off_signed_debit(residual, incoming=True), 1)

	def test_positive_residual_incoming_credits_round_off(self):
		residual = compute_rounding_residual(1373, 7, 196, "IRR")
		self.assertEqual(residual, 1)
		self.assertEqual(round_off_signed_debit(residual, incoming=True), -1)

	def test_positive_residual_outgoing_debits_round_off(self):
		self.assertEqual(round_off_signed_debit(1, incoming=False), 1)

	def test_mat_ste_03516_rate_first_zero_residual(self):
		from erpnext_extensions.iran_accounting.core.rounding import round_rate, round_row_amount

		int_rate = round_rate(STE_03516_RAW_RATE, 0)
		self.assertEqual(int_rate, int(STE_03516_INT_RATE))
		amount = round_row_amount(STE_03516_QTY, int_rate, 0)
		self.assertEqual(amount, int(STE_03516_AMOUNT))
		self.assertEqual(compute_rounding_residual(amount, STE_03516_QTY, int_rate, "IRR"), 0)

	def test_manufacture_add_lcv_residual_plus_two(self):
		self.assertEqual(rate_derived_amount(7, 204, "IRR"), 1428)
		self.assertEqual(compute_rounding_residual(1430, 7, 204, "IRR"), 2)
		self.assertEqual(round_off_signed_debit(2, incoming=True), -2)

	def test_manufacture_excluded_from_residual_collector(self):
		"""Manufacture valuation gaps use Stock Adjustment — not Round Off residual."""
		row = _Row(
			name="fg1",
			idx=1,
			item_code="FG",
			qty=7,
			transfer_qty=7,
			amount=1430,
			valuation_rate=204,
			s_warehouse=None,
			t_warehouse="Stores",
			cost_center="Main",
			is_finished_item=1,
		)
		doc = _Doc(
			doctype="Stock Entry",
			company="Test IRR Co",
			purpose="Manufacture",
			items=[row],
		)
		with (
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.irr_rounding_residual.get_company_currency",
				return_value="IRR",
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.irr_residual_classification.is_irr_company",
				return_value=True,
			),
		):
			self.assertEqual(collect_stock_entry_residuals(doc), [])
			self.assertEqual(expected_round_off_gl_totals(doc)["net_signed_debit"], 0)


class TestIRRRoundingResidualGLMap(unittest.TestCase):
	def _irr_doc(self, amount, qty, rate, *, purpose="Material Receipt", additional=0, lcv=0):
		row = _Row(
			name="row1",
			idx=1,
			item_code="ITEM",
			qty=qty,
			transfer_qty=qty,
			amount=amount,
			basic_amount=amount - additional - lcv,
			additional_cost=additional,
			landed_cost_voucher_amount=lcv,
			valuation_rate=rate,
			basic_rate=rate,
			t_warehouse="Stores",
			s_warehouse=None,
			cost_center="Main",
			is_finished_item=0,
		)
		return _Doc(
			doctype="Stock Entry",
			name="STE-TEST",
			company="Test IRR Co",
			purpose=purpose,
			posting_date="2026-01-01",
			items=[row],
			project=None,
		)

	def _base_gl(self, inv_debit, expense_credit):
		return [
			{
				"account": "Stock - T",
				"debit": inv_debit,
				"credit": 0,
				"company": "Test IRR Co",
				"posting_date": "2026-01-01",
				"voucher_type": "Stock Entry",
				"voucher_no": "STE-TEST",
				"remarks": "Accounting Entry for Stock",
				"cost_center": "Main",
			},
			{
				"account": "Expense - T",
				"debit": 0,
				"credit": expense_credit,
				"company": "Test IRR Co",
				"posting_date": "2026-01-01",
				"voucher_type": "Stock Entry",
				"voucher_no": "STE-TEST",
				"remarks": "Accounting Entry for Stock",
				"cost_center": "Main",
			},
		]

	def test_zero_residual_does_not_append_round_off(self):
		doc = self._irr_doc(1372, 7, 196)
		gl = self._base_gl(1372, 1372)
		with _patches():
			apply_irr_rate_rounding_residual_gl(doc, gl)
		self.assertEqual(len(gl), 2)
		self.assertFalse(_residual_rows(gl))

	def test_negative_residual_posts_debit_round_off(self):
		doc = self._irr_doc(1371, 7, 196)
		gl = self._base_gl(1371, 1371)
		with _patches(account="SA - T", cost_center="CC-RO"):
			apply_irr_rate_rounding_residual_gl(doc, gl)
		ro = _residual_rows(gl)
		self.assertEqual(len(ro), 1)
		self.assertEqual(ro[0]["account"], "SA - T")
		self.assertEqual(ro[0]["cost_center"], "CC-RO")
		self.assertEqual(ro[0].get(IRR_INVENTORY_VALUATION_RESIDUAL_MARKER), 1)
		self.assertTrue(
			is_irr_inventory_valuation_residual_gl(
				ro[0], company="Test IRR Co", stock_adjustment_account="SA - T"
			)
		)
		self.assertEqual(ro[0]["debit"], 1)
		self.assertEqual(ro[0]["credit"], 0)
		inv = [e for e in gl if e.get("account") == "Stock - T"][0]
		self.assertEqual(inv["debit"], 1371)
		self.assertEqual(sum(e.get("debit") or 0 for e in gl), sum(e.get("credit") or 0 for e in gl))

	def test_remarks_alone_are_not_enough_without_round_off_account(self):
		entry = {
			"account": "Expense - T",
			"remarks": f"{IRR_RATE_ROUNDING_RESIDUAL_REMARK}: fake",
		}
		self.assertFalse(
			is_irr_rate_rounding_residual_gl(entry, company="Test IRR Co", round_off_account="RO - T")
		)

	def test_positive_one_rial_residual_credits_round_off(self):
		doc = self._irr_doc(1373, 7, 196)
		gl = self._base_gl(1373, 1373)
		with _patches(account="SA - T", cost_center="CC-RO"):
			apply_irr_rate_rounding_residual_gl(doc, gl)
		ro = _residual_rows(gl)
		self.assertEqual(len(ro), 1)
		self.assertEqual(ro[0]["credit"], 1)
		self.assertEqual(ro[0]["debit"], 0)
		inv = [e for e in gl if e.get("account") == "Stock - T"][0]
		self.assertEqual(inv["debit"], 1373)
		self.assertEqual(sum(e.get("debit") or 0 for e in gl), sum(e.get("credit") or 0 for e in gl))

	def test_manufacture_does_not_post_round_off_residual(self):
		"""FINAL 3.8.4: Manufacture residual stays out of Round Off; Stock Adj untouched."""
		doc = self._irr_doc(1430, 7, 204, purpose="Manufacture", additional=137, lcv=59)
		gl = self._base_gl(1430, 1430)
		# Simulate Stock Adjustment valuation difference leg (must not be Round Off target).
		gl.append(
			{
				"account": "Stock Adjustment - T",
				"debit": 0,
				"credit": 5,
				"company": "Test IRR Co",
				"posting_date": "2026-01-01",
				"voucher_type": "Stock Entry",
				"voucher_no": "STE-TEST",
				"remarks": "Accounting Entry for Stock",
				"cost_center": "Main",
			}
		)
		with _patches(account="RO - T", cost_center="CC-RO"):
			with mock.patch(
				"erpnext_extensions.iran_accounting.domain.irr_rounding_residual._is_stock_adjustment_account",
				side_effect=lambda acc, company=None: acc == "Stock Adjustment - T",
			):
				apply_irr_rate_rounding_residual_gl(doc, gl)
				exp = expected_round_off_gl_totals(doc)
				self.assertEqual(exp["net_signed_debit"], 0)
		ro = [e for e in gl if IRR_RATE_ROUNDING_RESIDUAL_REMARK in (e.get("remarks") or "")]
		self.assertEqual(ro, [])
		inv = [e for e in gl if e.get("account") == "Stock - T"][0]
		self.assertEqual(inv["debit"], 1430)
		adj = [e for e in gl if e.get("account") == "Stock Adjustment - T"][0]
		self.assertEqual(adj["credit"], 5)

	def test_stock_adjustment_leg_not_reclassified_to_round_off(self):
		"""Material Receipt residual must reclassify expense, never Stock Adjustment."""
		doc = self._irr_doc(1371, 7, 196, purpose="Material Receipt")
		gl = [
			{
				"account": "Stock - T",
				"debit": 1371,
				"credit": 0,
				"company": "Test IRR Co",
				"posting_date": "2026-01-01",
				"voucher_type": "Stock Entry",
				"voucher_no": "STE-TEST",
				"remarks": "Accounting Entry for Stock",
				"cost_center": "Main",
			},
			{
				"account": "Stock Adjustment - T",
				"debit": 0,
				"credit": 1371,
				"company": "Test IRR Co",
				"posting_date": "2026-01-01",
				"voucher_type": "Stock Entry",
				"voucher_no": "STE-TEST",
				"remarks": "Accounting Entry for Stock",
				"cost_center": "Main",
			},
			{
				"account": "Expense - T",
				"debit": 0,
				"credit": 100,
				"company": "Test IRR Co",
				"posting_date": "2026-01-01",
				"voucher_type": "Stock Entry",
				"voucher_no": "STE-TEST",
				"remarks": "Additional Cost",
				"cost_center": "Main",
			},
		]
		with _patches(account="SA - T", cost_center="CC-RO"):
			with mock.patch(
				"erpnext_extensions.iran_accounting.domain.irr_rounding_residual._is_stock_adjustment_account",
				side_effect=lambda acc, company=None: acc in ("Stock Adjustment - T", "SA - T"),
			):
				apply_irr_rate_rounding_residual_gl(doc, gl)
		adj = [
			e
			for e in gl
			if e.get("account") == "Stock Adjustment - T"
			and IRR_INVENTORY_VALUATION_RESIDUAL_REMARK not in (e.get("remarks") or "")
		][0]
		self.assertEqual(adj["credit"], 1371)
		expense = [e for e in gl if e.get("account") == "Expense - T"][0]
		# residual −1 incoming → SA debit 1; expense gets −1 signed debit → credit +1
		self.assertEqual(expense["credit"], 101)
		ro = _residual_rows(gl)
		self.assertEqual(len(ro), 1)
		self.assertEqual(ro[0]["account"], "SA - T")
		self.assertEqual(ro[0]["debit"], 1)

	def test_idempotent_strip_and_reapply(self):
		doc = self._irr_doc(1371, 7, 196)
		with _patches(account="SA - T", cost_center="CC-RO"):
			fresh = self._base_gl(1371, 1371)
			apply_irr_rate_rounding_residual_gl(doc, fresh)
			apply_irr_rate_rounding_residual_gl(doc, fresh)
		ro = _residual_rows(fresh)
		self.assertEqual(len(ro), 1)
		self.assertEqual(ro[0]["debit"], 1)

	def test_missing_round_off_account_raises(self):
		doc = self._irr_doc(1371, 7, 196)
		gl = self._base_gl(1371, 1371)
		with _patches(account=None, cost_center="CC-RO"):
			with self.assertRaises(Exception):
				apply_irr_rate_rounding_residual_gl(doc, gl)

	def test_missing_round_off_cost_center_raises(self):
		doc = self._irr_doc(1371, 7, 196)
		gl = self._base_gl(1371, 1371)
		with _patches(account="SA - T", cost_center=None):
			with self.assertRaises(Exception):
				apply_irr_rate_rounding_residual_gl(doc, gl)

	def test_transfer_rows_skipped_in_collector(self):
		row = _Row(
			name="t1",
			idx=1,
			item_code="X",
			qty=7,
			transfer_qty=7,
			amount=1371,
			valuation_rate=196,
			s_warehouse="A",
			t_warehouse="B",
			cost_center="Main",
		)
		doc = _Doc(doctype="Stock Entry", company="Test IRR Co", items=[row])
		with mock.patch(
			"erpnext_extensions.iran_accounting.domain.irr_rounding_residual.get_company_currency",
			return_value="IRR",
		):
			self.assertEqual(collect_stock_entry_residuals(doc), [])

	def test_strip_removes_residual_lines_only(self):
		gl = self._base_gl(100, 100)
		gl.append(
			{
				"account": "RO",
				"debit": 1,
				"credit": 0,
				IRR_RATE_ROUNDING_RESIDUAL_MARKER: 1,
				"remarks": f"{IRR_RATE_ROUNDING_RESIDUAL_REMARK}: row 1",
			}
		)
		strip_irr_rate_rounding_residual_gl(gl)
		self.assertEqual(len(gl), 2)

	def test_multi_item_aggregation(self):
		rows = [
			_Row(
				name="r1",
				idx=1,
				item_code="A",
				qty=7,
				transfer_qty=7,
				amount=1371,
				valuation_rate=196,
				t_warehouse="S",
				s_warehouse=None,
				cost_center="Main",
			),
			_Row(
				name="r2",
				idx=2,
				item_code="B",
				qty=7,
				transfer_qty=7,
				amount=1373,
				valuation_rate=196,
				t_warehouse="S",
				s_warehouse=None,
				cost_center="Main",
			),
		]
		# residuals -1 and +1 → net Round Off 0
		doc = _Doc(
			doctype="Stock Entry",
			name="STE-MULTI",
			company="Test IRR Co",
			purpose="Material Receipt",
			posting_date="2026-01-01",
			items=rows,
			project=None,
		)
		gl = [
			{
				"account": "Stock - T",
				"debit": 2744,
				"credit": 0,
				"company": "Test IRR Co",
				"posting_date": "2026-01-01",
				"voucher_type": "Stock Entry",
				"voucher_no": "STE-MULTI",
				"remarks": "stock",
				"cost_center": "Main",
			},
			{
				"account": "Expense - T",
				"debit": 0,
				"credit": 2744,
				"company": "Test IRR Co",
				"posting_date": "2026-01-01",
				"voucher_type": "Stock Entry",
				"voucher_no": "STE-MULTI",
				"remarks": "exp",
				"cost_center": "Main",
			},
		]
		with _patches():
			apply_irr_rate_rounding_residual_gl(doc, gl)
		self.assertFalse(any(IRR_RATE_ROUNDING_RESIDUAL_REMARK in (e.get("remarks") or "") for e in gl))


if __name__ == "__main__":
	unittest.main()

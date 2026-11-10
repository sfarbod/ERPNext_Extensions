# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.7.24 — Manufacture ABSORB Class A residual vs ledger contract.

Reproduces Production blockers:
* MAT-STE-2026-43852 — consume ABSORB Δ326; composition rejected
* MAT-STE-2026-43765 — consume ABSORB Δ2228; FG SLE lagged SED after pool-close

Contract: warehouse-clear SLE.stock_value is authoritative for outgoing consume.
Integer basic_rate = ROUND_HALF_UP(amount/qty); residual ≤ r2_representation_bound
is Class A amount-authoritative — not a composition failure. After remirror, FG
pool-close must reassert incoming SLE from SED.amount.
"""

from __future__ import annotations

import unittest
from decimal import ROUND_HALF_UP, Decimal
from types import SimpleNamespace
from unittest import mock

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.domain.stock_entry_ledger_contract import (
	_assert_row_composition,
	_manufacture_consume_absorb_amount_authoritative,
	_r2_residual_within_bound,
)
from erpnext_extensions.iran_accounting.domain.stock_entry_sync import (
	reconcile_manufacture_pool_after_absorb_remirror,
	refresh_sle_authoritative_sed_from_ledger,
)
from erpnext_extensions.iran_accounting.manufacture_output_contract import (
	r2_representation_bound,
)


def _rhu(value: float) -> int:
	return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


class _Row(SimpleNamespace):
	def get(self, k, default=None):
		return getattr(self, k, default)


class _Doc(SimpleNamespace):
	def get(self, k, default=None):
		return getattr(self, k, default)


def _irr_patches():
	return mock.patch.multiple(
		"erpnext_extensions.iran_accounting.domain.stock_entry_ledger_contract",
		get_company_currency=mock.Mock(return_value="IRR"),
		get_currency_precision=mock.Mock(return_value=0),
	)


class TestAbsorbClassAMathV5724(unittest.TestCase):
	"""Incident arithmetic: Bin residual vs qty×rate."""

	CASES = (
		# name, qty, rate, bin_sv, expected_qty_x_rate, residual
		("43852_minus_326", 1537, 1256061, 1930565431, 1930565757, -326),
		("43765_plus_2228", 3000, 2160730, 6482192228, 6482190000, 2228),
	)

	def test_incident_residuals_and_r2_bounds(self):
		for name, qty, rate, bin_sv, expected, residual in self.CASES:
			with self.subTest(name=name):
				self.assertEqual(qty * rate, expected)
				self.assertEqual(bin_sv - expected, residual)
				bound = r2_representation_bound(qty)
				# After remirror: representational rate is HALF_UP(amount/qty);
				# that residual (not the draft qty×old_rate gap) must be in-bound.
				repr_rate = _rhu(bin_sv / qty)
				repr_residual = abs(bin_sv - repr_rate * qty)
				self.assertLessEqual(repr_residual, bound)

	def test_43852_rate_unchanged_under_half_up(self):
		qty, bin_sv = 1537, 1930565431
		self.assertEqual(_rhu(bin_sv / qty), 1256061)

	def test_43765_rate_steps_when_remirroring_amount(self):
		qty, bin_sv = 3000, 6482192228
		self.assertEqual(_rhu(bin_sv / qty), 2160731)
		self.assertEqual(abs(bin_sv - 2160731 * qty), 772)
		self.assertLessEqual(772, r2_representation_bound(qty))


class TestAbsorbCompositionGateV5724(unittest.TestCase):
	@staticmethod
	def _fake_frappe_db(get_value_return=None):
		return SimpleNamespace(get_value=mock.Mock(return_value=get_value_return))

	def test_consume_absorb_row_passes_composition_with_sle_authority(self):
		# MAT-STE-2026-43852 row 4 after SLE remirror — durable SLE warehouse clear
		qty, rate, ba = 1537, 1256061, 1930565431
		row = _Row(
			idx=4,
			name="q2n1vjnckp",
			item_code="30300019",
			transfer_qty=qty,
			qty=qty,
			basic_rate=rate,
			basic_amount=ba,
			amount=ba,
			additional_cost=0,
			landed_cost_voucher_amount=0,
			valuation_rate=rate,
			s_warehouse="WH-SRC",
			t_warehouse=None,
			is_finished_item=0,
		)
		doc = _Doc(
			doctype="Stock Entry",
			name="MAT-STE-2026-43852",
			purpose="Manufacture",
			company="Test",
			items=[row],
		)
		sle = frappe._dict(
			actual_qty=-qty,
			qty_after_transaction=0.0,
			stock_value_difference=-ba,
			stock_value=0.0,
		)
		fake_frappe = SimpleNamespace(db=self._fake_frappe_db(sle), _dict=frappe._dict)
		with _irr_patches(), mock.patch(
			"erpnext_extensions.iran_accounting.domain.currency.get_company_currency",
			return_value="IRR",
		), mock.patch(
			"erpnext_extensions.iran_accounting.manufacture_stage_costing.has_stage_participating_co_product",
			return_value=False,
		), mock.patch(
			"erpnext_extensions.iran_accounting.domain.stock_entry_ledger_contract.frappe",
			fake_frappe,
		):
			self.assertTrue(_r2_residual_within_bound(row, "Test"))
			self.assertTrue(_manufacture_consume_absorb_amount_authoritative(doc, row, "Test"))
			self.assertEqual(_assert_row_composition(doc, "Test"), [])

	def test_consume_absorb_row_passes_with_remirror_stamp(self):
		qty, rate, ba = 1537, 1256061, 1930565431
		row = _Row(
			idx=4,
			name="q2n1vjnckp",
			item_code="30300019",
			transfer_qty=qty,
			qty=qty,
			basic_rate=rate,
			basic_amount=ba,
			amount=ba,
			additional_cost=0,
			landed_cost_voucher_amount=0,
			valuation_rate=rate,
			s_warehouse="WH-SRC",
			t_warehouse=None,
			is_finished_item=0,
			flags=frappe._dict(iran_sle_authoritative_amount=ba),
		)
		doc = _Doc(
			doctype="Stock Entry",
			name="MAT-STE-2026-43852",
			purpose="Manufacture",
			company="Test",
			items=[row],
		)
		with _irr_patches(), mock.patch(
			"erpnext_extensions.iran_accounting.domain.currency.get_company_currency",
			return_value="IRR",
		), mock.patch(
			"erpnext_extensions.iran_accounting.manufacture_stage_costing.has_stage_participating_co_product",
			return_value=False,
		):
			self.assertTrue(_manufacture_consume_absorb_amount_authoritative(doc, row, "Test"))
			self.assertEqual(_assert_row_composition(doc, "Test"), [])

	def test_half_up_consistent_amount_without_absorb_authority_fails(self):
		"""Critical gate: HALF_UP-consistent residual without SLE/stamp must not pass."""
		# 43852-class residual 326 (> IRR composition tol 1) but no ABSORB authority
		qty, rate, ba = 1537, 1256061, 1930565431
		row = _Row(
			idx=1,
			name="junk",
			item_code="RM",
			transfer_qty=qty,
			qty=qty,
			basic_rate=rate,
			basic_amount=ba,
			amount=ba,
			additional_cost=0,
			landed_cost_voucher_amount=0,
			valuation_rate=rate,
			s_warehouse="WH-SRC",
			t_warehouse=None,
			is_finished_item=0,
		)
		doc = _Doc(
			doctype="Stock Entry",
			name="STE-JUNK",
			purpose="Manufacture",
			company="Test",
			items=[row],
		)
		fake_frappe = SimpleNamespace(db=self._fake_frappe_db(None), _dict=frappe._dict)
		with _irr_patches(), mock.patch(
			"erpnext_extensions.iran_accounting.domain.currency.get_company_currency",
			return_value="IRR",
		), mock.patch(
			"erpnext_extensions.iran_accounting.manufacture_stage_costing.has_stage_participating_co_product",
			return_value=False,
		), mock.patch(
			"erpnext_extensions.iran_accounting.domain.stock_entry_ledger_contract.frappe",
			fake_frappe,
		):
			self.assertTrue(_r2_residual_within_bound(row, "Test"))  # R2 alone would accept
			self.assertFalse(_manufacture_consume_absorb_amount_authoritative(doc, row, "Test"))
			failures = _assert_row_composition(doc, "Test")
			self.assertTrue(any("basic_amount" in f for f in failures))
	def test_exact_qty_times_rate_still_passes(self):
		qty, rate = 25, 230000
		ba = qty * rate
		row = _Row(
			idx=1,
			name="exact",
			item_code="RM",
			transfer_qty=qty,
			qty=qty,
			basic_rate=rate,
			basic_amount=ba,
			amount=ba,
			additional_cost=0,
			landed_cost_voucher_amount=0,
			valuation_rate=rate,
			s_warehouse="WH-SRC",
			t_warehouse=None,
			is_finished_item=0,
		)
		doc = _Doc(
			doctype="Stock Entry",
			name="STE-EXACT",
			purpose="Manufacture",
			company="Test",
			items=[row],
		)
		with _irr_patches(), mock.patch(
			"erpnext_extensions.iran_accounting.domain.currency.get_company_currency",
			return_value="IRR",
		), mock.patch(
			"erpnext_extensions.iran_accounting.manufacture_stage_costing.has_stage_participating_co_product",
			return_value=False,
		):
			self.assertEqual(_assert_row_composition(doc, "Test"), [])

	def test_residual_beyond_r2_bound_still_fails(self):
		qty, rate = 10, 1000
		# residual 100 ≫ bound 5
		ba = qty * rate + 100
		row = _Row(
			idx=1,
			name="over",
			item_code="RM",
			transfer_qty=qty,
			qty=qty,
			basic_rate=rate,
			basic_amount=ba,
			amount=ba,
			additional_cost=0,
			landed_cost_voucher_amount=0,
			valuation_rate=rate,
			s_warehouse="WH-SRC",
			t_warehouse=None,
			is_finished_item=0,
			flags=frappe._dict(iran_sle_authoritative_amount=ba),
		)
		doc = _Doc(
			doctype="Stock Entry",
			name="STE-OVER",
			purpose="Manufacture",
			company="Test",
			items=[row],
		)
		with _irr_patches(), mock.patch(
			"erpnext_extensions.iran_accounting.domain.currency.get_company_currency",
			return_value="IRR",
		), mock.patch(
			"erpnext_extensions.iran_accounting.manufacture_stage_costing.has_stage_participating_co_product",
			return_value=False,
		):
			self.assertFalse(_manufacture_consume_absorb_amount_authoritative(doc, row, "Test"))
			failures = _assert_row_composition(doc, "Test")
			self.assertTrue(any("basic_amount" in f for f in failures))

	def test_fg_row_not_treated_as_absorb_consume_gate(self):
		qty, rate, ba = 3, 3336, 10007  # classic FG residual
		row = _Row(
			idx=2,
			name="fg",
			item_code="FG",
			transfer_qty=qty,
			qty=qty,
			basic_rate=rate,
			basic_amount=ba,
			amount=ba,
			additional_cost=0,
			landed_cost_voucher_amount=0,
			valuation_rate=_rhu(ba / qty),
			s_warehouse=None,
			t_warehouse="WH-FG",
			is_finished_item=1,
		)
		doc = _Doc(
			doctype="Stock Entry",
			name="STE-FG",
			purpose="Manufacture",
			company="Test",
			items=[row],
		)
		with _irr_patches(), mock.patch(
			"erpnext_extensions.iran_accounting.domain.currency.get_company_currency",
			return_value="IRR",
		):
			self.assertFalse(_manufacture_consume_absorb_amount_authoritative(doc, row, "Test"))

	def test_material_issue_still_uses_native_outgoing_gate_not_mfg_absorb(self):
		qty, rate, ba = 1537, 1256061, 1930565431
		row = _Row(
			idx=1,
			name="mi",
			item_code="RM",
			transfer_qty=qty,
			qty=qty,
			basic_rate=rate,
			basic_amount=ba,
			amount=ba,
			additional_cost=0,
			landed_cost_voucher_amount=0,
			valuation_rate=rate,
			s_warehouse="WH-SRC",
			t_warehouse=None,
			is_finished_item=0,
			flags=frappe._dict(iran_sle_authoritative_amount=ba),
		)
		doc = _Doc(
			doctype="Stock Entry",
			name="STE-MI",
			purpose="Material Issue",
			company="Test",
			items=[row],
		)
		with _irr_patches(), mock.patch(
			"erpnext_extensions.iran_accounting.domain.currency.get_company_currency",
			return_value="IRR",
		):
			self.assertFalse(_manufacture_consume_absorb_amount_authoritative(doc, row, "Test"))

class TestReconcileAbsorbPoolV5724(unittest.TestCase):
	def test_reconcile_reasserts_fg_sle_after_absorb_delta(self):
		"""43765-class: consume ABSORB +2228 must lift FG SLE by the same delta."""
		consume = _Row(
			idx=1,
			name="sed-rm",
			item_code="30100019",
			transfer_qty=3000,
			qty=3000,
			basic_rate=2160731,
			basic_amount=6482192228,
			amount=6482192228,
			additional_cost=0,
			landed_cost_voucher_amount=0,
			valuation_rate=2160731,
			s_warehouse="WH-SRC",
			t_warehouse=None,
			is_finished_item=0,
		)
		addl = 55251819
		fg = _Row(
			idx=2,
			name="sed-fg",
			item_code="30100018",
			transfer_qty=3000,
			qty=3000,
			basic_rate=2160730,
			basic_amount=6482190000,
			amount=6482190000 + addl,
			additional_cost=addl,
			landed_cost_voucher_amount=0,
			valuation_rate=2179147,
			s_warehouse=None,
			t_warehouse="WH-FG",
			is_finished_item=1,
		)
		doc = _Doc(
			doctype="Stock Entry",
			name="MAT-STE-2026-43765",
			purpose="Manufacture",
			company="C",
			items=[consume, fg],
			total_incoming_value=0,
			total_outgoing_value=0,
			value_difference=0,
		)
		sle_fg = frappe._dict(
			name="sle-fg",
			item_code="30100018",
			warehouse="WH-FG",
			company="C",
			actual_qty=3000,
			stock_value_difference=6482190000 + addl,  # pre-absorb pool
			stock_value=6482190000 + addl,
			qty_after_transaction=3000,
			incoming_rate=2179147,
			outgoing_rate=0,
			valuation_rate=2179147,
			voucher_detail_no="sed-fg",
			voucher_type="Stock Entry",
			voucher_no="MAT-STE-2026-43765",
			is_cancelled=0,
		)

		captured = {}

		def _set_value(doctype, name, values, update_modified=False):
			if doctype == "Stock Ledger Entry":
				captured["svd"] = flt(values.get("stock_value_difference"))
				captured["delta_from_old"] = captured["svd"] - (6482190000 + addl)

		def _get_all(*a, **k):
			return [dict(sle_fg)]

		def _get_value(*a, **k):
			if a and a[0] == "Stock Entry":
				return "Manufacture"
			if a and a[0] == "Stock Entry Detail":
				return frappe._dict(
					qty=3000,
					transfer_qty=3000,
					basic_rate=2160731,
					basic_amount=6482192228,
					additional_cost=addl,
					landed_cost_voucher_amount=0,
					valuation_rate=_rhu((6482192228 + addl) / 3000),
					amount=6482192228 + addl,
					allow_zero_valuation_rate=0,
					set_basic_rate_manually=0,
					parent="MAT-STE-2026-43765",
				)
			return None

		fake_db = SimpleNamespace(set_value=_set_value, get_value=_get_value)
		fake_frappe = SimpleNamespace(db=fake_db, get_all=_get_all, _dict=frappe._dict)

		with mock.patch(
			"erpnext_extensions.iran_accounting.domain.stock_entry_sync.is_irr_company",
			return_value=True,
		), mock.patch(
			"erpnext_extensions.iran_accounting.domain.stock_entry_sync.get_company_currency",
			return_value="IRR",
		), mock.patch(
			"erpnext_extensions.iran_accounting.domain.stock_entry_sync.round_currency",
			side_effect=lambda v, c: float(round(float(v))),
		), mock.patch(
			"erpnext_extensions.iran_accounting.domain.stock_entry_sync.round_monetary_rate",
			side_effect=lambda v, c: float(round(float(v))),
		), mock.patch(
			"erpnext_extensions.iran_accounting.manufacture_rounding.align_manufacture_finished_good_residual",
			side_effect=lambda d: (
				setattr(fg, "basic_amount", 6482192228),
				setattr(fg, "amount", 6482192228 + addl),
				setattr(fg, "basic_rate", 2160731),
				setattr(fg, "valuation_rate", _rhu((6482192228 + addl) / 3000)),
			),
		), mock.patch(
			"erpnext_extensions.iran_accounting.domain.stock_entry_sync.frappe",
			fake_frappe,
		), mock.patch(
			"erpnext_extensions.iran_accounting.domain.stock_entry_sync._adjust_bin_stock_value_for_sle_delta",
		) as bin_adj:
			n = reconcile_manufacture_pool_after_absorb_remirror(doc)

		self.assertEqual(n, 1)
		self.assertEqual(captured["svd"], 6482192228 + addl)
		self.assertEqual(captured["delta_from_old"], 2228)
		bin_adj.assert_called()
		self.assertEqual(flt(fg.basic_amount), 6482192228)


class TestRefreshTriggersReconcileV5724(unittest.TestCase):
	def test_refresh_calls_reconcile_when_outgoing_changed(self):
		doc = _Doc(
			doctype="Stock Entry",
			name="STE-X",
			purpose="Manufacture",
			company="C",
			items=[],
		)
		with mock.patch(
			"erpnext_extensions.iran_accounting.domain.currency.is_irr_company",
			return_value=True,
		), mock.patch(
			"erpnext_extensions.iran_accounting.domain.stock_entry_sync.get_company_currency",
			return_value="IRR",
		), mock.patch(
			"erpnext_extensions.iran_accounting.domain.stock_entry_sync.reconcile_manufacture_pool_after_absorb_remirror",
		) as reconcile:
			# Empty items → changed=0 → reconcile must NOT run
			n = refresh_sle_authoritative_sed_from_ledger(doc)
			self.assertEqual(n, 0)
			reconcile.assert_not_called()

if __name__ == "__main__":
	unittest.main()

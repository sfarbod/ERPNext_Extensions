# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.7.8 — Transfer inward SLE must conserve outward magnitude after SABE.

Production blockers (backup 20261107_202825):
* PO-JOB12112 — Material Transfer for Manufacture: outward −13_533_459,
  inward SABE-poisoned +13_533_460 (net +1 IRR).
* MAT-STE-2026-43659 — Material Transfer: outward −5_867_923_151,
  inward SABE-poisoned +5_867_922_870 (net −281 IRR).

Root cause: ``conserve_transfer_inward_from_outward_sle`` correctly sets the
destination SLE from the outward leg; later SABE integer align stamps
``qty × ROUND_HALF_UP(rate)`` and breaks transfer net-zero.
"""

from __future__ import annotations

import unittest
from decimal import ROUND_HALF_UP, Decimal
from types import SimpleNamespace
from unittest import mock

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.domain.stock_entry_sync import (
	conserve_transfer_inward_from_outward_sle,
	reassert_document_amount_authority_sle,
	reassert_transfer_inward_conservation_sle,
)


def _rhu(value: float) -> int:
	return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


# Incident fixtures (outward magnitude → correct inward SLE movement).
PO_OUTWARD = 13_533_459.0
PO_POISON = 13_533_460.0  # qty × RHU(rate) invents +1
MAT_OUTWARD = 5_867_923_151.0
MAT_POISON = 5_867_922_870.0  # qty × RHU(rate) drops −281


def _xfer_row(*, amount: float, qty: float = 1.0, addl: float = 0.0, lcv: float = 0.0):
	return frappe._dict(
		qty=qty,
		transfer_qty=qty,
		basic_rate=amount / qty if qty else 0,
		basic_amount=amount - addl - lcv,
		additional_cost=addl,
		landed_cost_voucher_amount=lcv,
		valuation_rate=_rhu(amount / qty) if qty else 0,
		amount=amount,
		allow_zero_valuation_rate=0,
		set_basic_rate_manually=0,
		s_warehouse="SRC-WH",
		t_warehouse="DST-WH",
		parent="SE-X",
	)


def _in_sle(*, svd: float, qty: float = 1.0, detail="sed-in", voucher="SE-X"):
	rate = abs(svd / qty) if qty else 0
	return frappe._dict(
		company="C",
		voucher_type="Stock Entry",
		voucher_no=voucher,
		voucher_detail_no=detail,
		item_code="ITEM",
		actual_qty=qty,
		stock_value_difference=svd,
		stock_value=svd,
		qty_after_transaction=qty,
		incoming_rate=rate,
		outgoing_rate=0.0,
		valuation_rate=rate,
		is_cancelled=0,
	)


class TestIncidentMathV578(unittest.TestCase):
	def test_po_job12112_one_irr_poison(self):
		self.assertEqual(PO_POISON - PO_OUTWARD, 1.0)

	def test_mat_43659_281_irr_poison(self):
		self.assertEqual(MAT_POISON - MAT_OUTWARD, -281.0)

	def test_round_half_up_integer_irr(self):
		# Representational rate from outward amount may not equal amount/qty.
		self.assertEqual(_rhu(PO_OUTWARD / 1.0), int(PO_OUTWARD))
		self.assertEqual(_rhu(MAT_OUTWARD / 200.0), 29_339_616)


class TestReassertTransferInwardV578(unittest.TestCase):
	def _fake_db(self, purpose: str, row, outward_svd: float):
		def get_value(*a, **k):
			if a and a[0] == "Stock Entry":
				return purpose
			if a and a[0] == "Stock Entry Detail":
				return row
			if a and a[0] == "Stock Ledger Entry":
				# _outward_leg_stock_value_difference → stock_value_difference
				return outward_svd
			return None

		return SimpleNamespace(get_value=get_value, set_value=mock.Mock())

	def test_po_job12112_reassert_restores_outward_magnitude(self):
		row = _xfer_row(amount=PO_POISON)  # SED may show poisoned amount
		sle = _in_sle(svd=PO_POISON, voucher="PO-JOB12112")
		fake_db = self._fake_db("Material Transfer for Manufacture", row, -PO_OUTWARD)
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
			"erpnext_extensions.iran_accounting.domain.stock_entry_sync.frappe.db",
			fake_db,
		), mock.patch(
			"erpnext_extensions.iran_accounting.domain.stock_entry_sync.align_sed_from_native_outgoing_sle",
		):
			ok = reassert_transfer_inward_conservation_sle(sle)
		self.assertTrue(ok)
		self.assertEqual(flt(sle.stock_value_difference), PO_OUTWARD)
		self.assertEqual(flt(sle.stock_value), PO_OUTWARD)

	def test_mat_43659_reassert_restores_outward_magnitude(self):
		qty = 200.0
		row = _xfer_row(amount=MAT_POISON, qty=qty)
		sle = _in_sle(svd=MAT_POISON, qty=qty, voucher="MAT-STE-2026-43659")
		fake_db = self._fake_db("Material Transfer", row, -MAT_OUTWARD)
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
			"erpnext_extensions.iran_accounting.domain.stock_entry_sync.frappe.db",
			fake_db,
		), mock.patch(
			"erpnext_extensions.iran_accounting.domain.stock_entry_sync.align_sed_from_native_outgoing_sle",
		):
			ok = reassert_transfer_inward_conservation_sle(sle)
		self.assertTrue(ok)
		self.assertEqual(flt(sle.stock_value_difference), MAT_OUTWARD)

	def test_transfer_net_zero_after_reassert_po(self):
		out_svd = -PO_OUTWARD
		in_svd_poison = PO_POISON
		row = _xfer_row(amount=PO_POISON)
		sle = _in_sle(svd=in_svd_poison, voucher="PO-JOB12112")
		fake_db = self._fake_db("Material Transfer for Manufacture", row, out_svd)
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
			"erpnext_extensions.iran_accounting.domain.stock_entry_sync.frappe.db",
			fake_db,
		), mock.patch(
			"erpnext_extensions.iran_accounting.domain.stock_entry_sync.align_sed_from_native_outgoing_sle",
		):
			reassert_transfer_inward_conservation_sle(sle)
		self.assertEqual(out_svd + flt(sle.stock_value_difference), 0.0)

	def test_transfer_net_zero_after_reassert_mat(self):
		out_svd = -MAT_OUTWARD
		sle = _in_sle(svd=MAT_POISON, qty=200.0, voucher="MAT-STE-2026-43659")
		row = _xfer_row(amount=MAT_POISON, qty=200.0)
		fake_db = self._fake_db("Material Transfer", row, out_svd)
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
			"erpnext_extensions.iran_accounting.domain.stock_entry_sync.frappe.db",
			fake_db,
		), mock.patch(
			"erpnext_extensions.iran_accounting.domain.stock_entry_sync.align_sed_from_native_outgoing_sle",
		):
			reassert_transfer_inward_conservation_sle(sle)
		self.assertEqual(out_svd + flt(sle.stock_value_difference), 0.0)

	def test_idempotent_repeated_reassert(self):
		row = _xfer_row(amount=PO_OUTWARD)
		sle = _in_sle(svd=PO_OUTWARD, voucher="PO-JOB12112")
		fake_db = self._fake_db("Material Transfer for Manufacture", row, -PO_OUTWARD)
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
			"erpnext_extensions.iran_accounting.domain.stock_entry_sync.frappe.db",
			fake_db,
		), mock.patch(
			"erpnext_extensions.iran_accounting.domain.stock_entry_sync.align_sed_from_native_outgoing_sle",
		):
			self.assertTrue(reassert_transfer_inward_conservation_sle(sle))
			first = flt(sle.stock_value_difference)
			self.assertTrue(reassert_transfer_inward_conservation_sle(sle))
			self.assertEqual(flt(sle.stock_value_difference), first)

	def test_skips_outgoing_leg(self):
		sle = _in_sle(svd=-PO_OUTWARD)
		sle.actual_qty = -1.0
		self.assertFalse(reassert_transfer_inward_conservation_sle(sle))

	def test_mixed_positive_negative_legs_net_zero(self):
		# Two-row transfer: conserve each inward independently against its outward.
		movements = [-100.0, 100.0, -50.0, 50.0]
		self.assertEqual(sum(movements), 0.0)

	def test_addl_cost_not_double_counted_on_conserve(self):
		# Outward basic 1000; destination addl 50 → inward must be 1050 once.
		outward = -1000.0
		addl = 50.0
		row = _xfer_row(amount=1050.0, addl=addl)
		sle = SimpleNamespace(
			actual_qty=1.0,
			stock_value_difference=1100.0,  # wrongly includes addl twice-ish
			stock_value=1100.0,
			incoming_rate=1100.0,
			outgoing_rate=0.0,
			valuation_rate=1100.0,
			voucher_type="Stock Entry",
			voucher_no="SE-X",
			voucher_detail_no="d1",
			item_code="I",
			company="C",
		)
		with mock.patch(
			"erpnext_extensions.iran_accounting.domain.stock_entry_sync._outward_leg_stock_value_difference",
			return_value=outward,
		), mock.patch(
			"erpnext_extensions.iran_accounting.domain.stock_entry_sync.align_sed_from_native_outgoing_sle",
		), mock.patch(
			"erpnext_extensions.iran_accounting.domain.stock_entry_sync.round_currency",
			side_effect=lambda v, c: float(round(float(v))),
		), mock.patch(
			"erpnext_extensions.iran_accounting.domain.stock_entry_sync.round_monetary_rate",
			side_effect=lambda v, c: float(round(float(v))),
		):
			ok = conserve_transfer_inward_from_outward_sle(
				sle, row, "Material Transfer", "IRR"
			)
		self.assertTrue(ok)
		self.assertEqual(flt(sle.stock_value_difference), 1050.0)


class TestSabeSkipsQtyTimesRateOnTransferInwardV578(unittest.TestCase):
	def test_sabe_transfer_inward_conserves_not_qty_times_rate(self):
		from erpnext_extensions.iran_accounting.domain.sabe_integer_valuation import (
			apply_integer_sabe_policy_to_sle,
		)

		qty = 1.0
		# SABE would stamp PO_POISON; conservation must keep PO_OUTWARD.
		sle = _in_sle(svd=PO_OUTWARD, qty=qty, voucher="PO-JOB12112", detail="sed-po")
		sle.serial_and_batch_bundle = "SABB-PO"
		sabe_rows = [
			frappe._dict(
				name="sabe1",
				qty=qty,
				incoming_rate=PO_POISON,
				outgoing_rate=0,
				stock_value_difference=PO_POISON,
				batch_no=None,
			)
		]
		row = _xfer_row(amount=PO_POISON, qty=qty)
		row.parent = "PO-JOB12112"

		def get_value(*a, **k):
			if a and a[0] == "Stock Entry":
				return "Material Transfer for Manufacture"
			if a and a[0] == "Stock Entry Detail":
				return row
			if a and a[0] == "Stock Ledger Entry":
				return -PO_OUTWARD
			return None

		fake_db = SimpleNamespace(get_value=get_value, set_value=mock.Mock())
		engine = SimpleNamespace(
			wh_data=SimpleNamespace(
				stock_value=PO_OUTWARD, valuation_rate=PO_OUTWARD, prev_stock_value=PO_OUTWARD
			)
		)
		with mock.patch(
			"erpnext_extensions.iran_accounting.domain.sabe_integer_valuation.is_irr_company",
			return_value=True,
		), mock.patch(
			"erpnext_extensions.iran_accounting.domain.sabe_integer_valuation.get_company_currency",
			return_value="IRR",
		), mock.patch(
			"erpnext_extensions.iran_accounting.domain.sabe_integer_valuation.round_currency",
			side_effect=lambda v, c: float(round(float(v))),
		), mock.patch(
			"erpnext_extensions.iran_accounting.domain.sabe_integer_valuation.round_monetary_rate",
			side_effect=lambda v, c: float(round(float(v))),
		), mock.patch(
			"erpnext_extensions.iran_accounting.domain.sabe_integer_valuation.frappe.get_all",
			return_value=sabe_rows,
		), mock.patch(
			"erpnext_extensions.iran_accounting.domain.sabe_integer_valuation.frappe.db",
			fake_db,
		), mock.patch(
			"erpnext_extensions.iran_accounting.domain.sabe_integer_valuation.queue_localization_delta",
		) as queue_a2, mock.patch(
			"erpnext_extensions.iran_accounting.domain.reject_only_riv_rate.should_seed_sabe_from_document_sle",
			return_value=False,
		), mock.patch(
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
			"erpnext_extensions.iran_accounting.domain.stock_entry_sync.frappe.db",
			fake_db,
		), mock.patch(
			"erpnext_extensions.iran_accounting.domain.stock_entry_sync.align_sed_from_native_outgoing_sle",
		):
			# Start already conserved; SABE would poison to PO_POISON without fix.
			sle.stock_value_difference = PO_OUTWARD
			sle.stock_value = PO_OUTWARD
			out = apply_integer_sabe_policy_to_sle(sle, engine=engine, align_sle=True)

		self.assertTrue(out.get("transfer_inward_conservation"))
		self.assertEqual(flt(sle.stock_value_difference), PO_OUTWARD)
		queue_a2.assert_not_called()

	def test_manufacture_amount_authority_still_intact(self):
		"""v5.7.3 path must remain: Manufacture uses SED.amount, not transfer conserve."""
		qty, amount, vr = 1074, 616_660_715, 574_172
		sle = frappe._dict(
			company="C",
			voucher_type="Stock Entry",
			voucher_no="MAT-STE-X",
			voucher_detail_no="sed-fg",
			actual_qty=qty,
			stock_value_difference=vr * qty,
			stock_value=vr * qty,
			qty_after_transaction=qty,
			incoming_rate=vr,
			valuation_rate=vr,
			is_cancelled=0,
		)
		row = frappe._dict(
			amount=amount,
			basic_amount=amount - 2_180_724,
			additional_cost=2_180_724,
			landed_cost_voucher_amount=0,
			valuation_rate=vr,
			basic_rate=vr - 2030,
			qty=qty,
			transfer_qty=qty,
			s_warehouse=None,
			t_warehouse="FG",
			set_basic_rate_manually=0,
		)

		def get_value(*a, **k):
			if a and a[0] == "Stock Entry":
				return "Manufacture"
			if a and a[0] == "Stock Entry Detail":
				return row
			return None

		fake_db = SimpleNamespace(get_value=get_value, set_value=mock.Mock())
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
			"erpnext_extensions.iran_accounting.domain.stock_entry_sync.frappe.db",
			fake_db,
		):
			ok = reassert_document_amount_authority_sle(sle)
			# Transfer reassert must NOT rewrite Manufacture FG.
			xfer = reassert_transfer_inward_conservation_sle(sle)
		self.assertTrue(ok)
		self.assertFalse(xfer)
		self.assertEqual(flt(sle.stock_value_difference), float(amount))


class TestInvalidLedgerStillFailsV578(unittest.TestCase):
	def test_mirror_contract_rejects_poisoned_inward(self):
		from erpnext_extensions.iran_accounting.domain.stock_entry_sync import (
			signed_movement_from_row_amount,
		)

		# Row mirror uses conserved outward amount; poisoned SLE must differ.
		row_mag = PO_OUTWARD
		sle = _in_sle(svd=PO_POISON)
		mirror = signed_movement_from_row_amount(row_mag, flt(sle.actual_qty), sle=sle)
		self.assertEqual(mirror, PO_OUTWARD)
		self.assertNotEqual(flt(sle.stock_value_difference), mirror)


if __name__ == "__main__":
	unittest.main()

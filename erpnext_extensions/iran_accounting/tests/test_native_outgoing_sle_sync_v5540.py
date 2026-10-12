# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.5.40 — Material Issue/SABB: preserve native SLE; do not stamp poisoned SED.

Regression for MAT-STE-2026-36831 class defects:
Iran sync_irr_sle_from_stock_entry_row must not replace BatchNoValuation /
SABB outgoing movement with an unrelated Stock Entry Detail amount.
Approved Manufacture/Repack SED→SLE mirroring must remain unchanged.
"""

from __future__ import annotations

import unittest
from unittest import mock

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.domain.stock_entry_sync import (
	NATIVE_OUTGOING_STOCK_VALUATION_PURPOSES,
	align_sed_from_native_outgoing_sle,
	refresh_native_outgoing_sed_from_db,
	should_preserve_native_outgoing_valuation,
	sync_irr_sle_from_stock_entry_row,
)


def _sle(**fields):
	base = dict(
		company="Test IRR Co",
		voucher_type="Stock Entry",
		voucher_no="MAT-STE-TEST",
		voucher_detail_no="sed-1",
		actual_qty=-1.0,
		stock_value_difference=-67589.0,
		stock_value=1000000.0,
		outgoing_rate=67589.0,
		incoming_rate=0.0,
		valuation_rate=67589.0,
		is_cancelled=0,
		serial_and_batch_bundle="sabb-1",
	)
	base.update(fields)
	return frappe._dict(base)


def _row(**fields):
	base = dict(
		qty=1,
		transfer_qty=1,
		basic_rate=32980402089536.0,
		basic_amount=32980402089536.0,
		amount=32980402089536.0,
		valuation_rate=32980402089536.0,
		additional_cost=0,
		landed_cost_voucher_amount=0,
		allow_zero_valuation_rate=0,
		set_basic_rate_manually=0,
		parent="MAT-STE-TEST",
		serial_and_batch_bundle="sabb-1",
		s_warehouse="Stores",
		t_warehouse=None,
	)
	base.update(fields)
	return frappe._dict(base)


class TestShouldPreserveNativeOutgoing(unittest.TestCase):
	def test_material_issue_outgoing_non_manual(self):
		self.assertTrue(
			should_preserve_native_outgoing_valuation(
				_sle(), _row(), "Material Issue"
			)
		)

	def test_manufacture_keeps_document_authority(self):
		self.assertFalse(
			should_preserve_native_outgoing_valuation(_sle(), _row(), "Manufacture")
		)

	def test_repack_keeps_document_authority(self):
		self.assertFalse(
			should_preserve_native_outgoing_valuation(_sle(), _row(), "Repack")
		)

	def test_manual_rate_keeps_document_authority(self):
		self.assertFalse(
			should_preserve_native_outgoing_valuation(
				_sle(), _row(set_basic_rate_manually=1, basic_rate=100, amount=100), "Material Issue"
			)
		)

	def test_allow_zero_keeps_document_zero(self):
		self.assertFalse(
			should_preserve_native_outgoing_valuation(
				_sle(stock_value_difference=0, outgoing_rate=0),
				_row(
					basic_rate=0,
					basic_amount=0,
					amount=0,
					valuation_rate=0,
					allow_zero_valuation_rate=1,
				),
				"Material Issue",
			)
		)

	def test_inward_not_preserved(self):
		self.assertFalse(
			should_preserve_native_outgoing_valuation(
				_sle(actual_qty=1, stock_value_difference=67589),
				_row(),
				"Material Issue",
			)
		)

	def test_transfer_purposes_included(self):
		for purpose in NATIVE_OUTGOING_STOCK_VALUATION_PURPOSES:
			self.assertTrue(
				should_preserve_native_outgoing_valuation(_sle(), _row(), purpose),
				msg=purpose,
			)


class TestSyncPreservesNativeBatchMovement(unittest.TestCase):
	"""A2/A4/A10 — poisoned SED must not overwrite vanilla SABB movement."""

	def test_material_issue_keeps_vanilla_svd_and_realigns_sed(self):
		sle = _sle()
		row = _row()
		set_value = mock.Mock()

		with (
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.stock_entry_sync.is_irr_company",
				return_value=True,
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.stock_entry_sync.get_company_currency",
				return_value="IRR",
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.stock_entry_sync.frappe.db.get_value",
				side_effect=[row, "Material Issue"],
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.stock_entry_sync.frappe.db.set_value",
				set_value,
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.stock_entry_sync.round_currency",
				side_effect=lambda v, _c: float(round(flt(v))),
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.stock_entry_sync.round_monetary_rate",
				side_effect=lambda v, _c: float(round(flt(v))),
			),
		):
			sync_irr_sle_from_stock_entry_row(sle)

		self.assertEqual(flt(sle.stock_value_difference), -67589.0)
		self.assertEqual(flt(sle.outgoing_rate), 67589.0)
		self.assertEqual(flt(sle.valuation_rate), 67589.0)
		# value_before = 1_000_000 - (-67589); after = before + movement → unchanged
		self.assertEqual(flt(sle.stock_value), 1000000.0)
		set_value.assert_called()
		args, kwargs = set_value.call_args
		self.assertEqual(args[0], "Stock Entry Detail")
		self.assertEqual(args[1], "sed-1")
		self.assertEqual(args[2]["basic_rate"], 67589.0)
		self.assertEqual(args[2]["amount"], 67589.0)

	def test_material_issue_13100058_scale_poison(self):
		sle = _sle(
			stock_value_difference=-290007.0,
			outgoing_rate=290007.0,
			valuation_rate=290007.0,
			stock_value=2_900_000.0,
		)
		row = _row(
			basic_rate=30_547_766.0,
			basic_amount=30_547_766.0,
			amount=30_547_766.0,
			valuation_rate=30_547_766.0,
		)
		with (
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.stock_entry_sync.is_irr_company",
				return_value=True,
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.stock_entry_sync.get_company_currency",
				return_value="IRR",
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.stock_entry_sync.frappe.db.get_value",
				side_effect=[row, "Material Issue"],
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.stock_entry_sync.frappe.db.set_value"
			) as set_value,
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.stock_entry_sync.round_currency",
				side_effect=lambda v, _c: float(round(flt(v))),
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.stock_entry_sync.round_monetary_rate",
				side_effect=lambda v, _c: float(round(flt(v))),
			),
		):
			sync_irr_sle_from_stock_entry_row(sle)

		self.assertEqual(flt(sle.stock_value_difference), -290007.0)
		self.assertEqual(flt(sle.outgoing_rate), 290007.0)
		self.assertEqual(set_value.call_args[0][2]["basic_rate"], 290007.0)

	def test_manufacture_still_mirrors_sed_amount(self):
		"""C8/C10 — approved manufacturing SED→SLE mirror must remain."""
		sle = _sle(
			stock_value_difference=-500.0,  # vanilla placeholder
			outgoing_rate=500.0,
			valuation_rate=500.0,
			stock_value=9_500.0,
		)
		row = _row(
			basic_rate=1000.0,
			basic_amount=1000.0,
			amount=1000.0,
			valuation_rate=1000.0,
		)
		with (
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.stock_entry_sync.is_irr_company",
				return_value=True,
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.stock_entry_sync.get_company_currency",
				return_value="IRR",
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.stock_entry_sync.frappe.db.get_value",
				side_effect=[row, "Manufacture"],
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.stock_entry_sync.round_currency",
				side_effect=lambda v, _c: float(round(flt(v))),
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.stock_entry_sync.round_monetary_rate",
				side_effect=lambda v, _c: float(round(flt(v))),
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.stock_entry_sync.frappe.db.set_value"
			) as set_value,
		):
			sync_irr_sle_from_stock_entry_row(sle)

		# Document amount wins for Manufacture consume/output mirroring.
		self.assertEqual(flt(sle.stock_value_difference), -1000.0)
		self.assertEqual(flt(sle.outgoing_rate), 1000.0)
		set_value.assert_not_called()


class TestRefreshNativeOutgoingFromDb(unittest.TestCase):
	def test_refreshes_memory_from_db(self):
		row = frappe._dict(
			name="sed-1",
			s_warehouse="Stores",
			set_basic_rate_manually=0,
			basic_rate=32980402089536.0,
			valuation_rate=32980402089536.0,
			basic_amount=32980402089536.0,
			amount=32980402089536.0,
		)
		doc = frappe._dict(doctype="Stock Entry", purpose="Material Issue", items=[row])
		live = frappe._dict(
			basic_rate=67589.0,
			valuation_rate=67589.0,
			basic_amount=67589.0,
			amount=67589.0,
		)
		with mock.patch(
			"erpnext_extensions.iran_accounting.domain.stock_entry_sync.frappe.db.get_value",
			return_value=live,
		):
			refresh_native_outgoing_sed_from_db(doc)
		self.assertEqual(row.basic_rate, 67589.0)
		self.assertEqual(row.amount, 67589.0)

	def test_skips_manufacture(self):
		row = frappe._dict(
			name="sed-1",
			s_warehouse="Stores",
			set_basic_rate_manually=0,
			basic_rate=1000.0,
			amount=1000.0,
		)
		doc = frappe._dict(doctype="Stock Entry", purpose="Manufacture", items=[row])
		with mock.patch(
			"erpnext_extensions.iran_accounting.domain.stock_entry_sync.frappe.db.get_value"
		) as get_value:
			refresh_native_outgoing_sed_from_db(doc)
		get_value.assert_not_called()
		self.assertEqual(row.basic_rate, 1000.0)


class TestAlignSedHelper(unittest.TestCase):
	def test_preserves_additional_cost_on_amount(self):
		sle = _sle()
		row = _row(additional_cost=100)
		with (
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.stock_entry_sync.frappe.db.set_value"
			) as set_value,
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.stock_entry_sync.round_currency",
				side_effect=lambda v, _c: float(round(flt(v))),
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.stock_entry_sync.round_monetary_rate",
				side_effect=lambda v, _c: float(round(flt(v))),
			),
		):
			align_sed_from_native_outgoing_sle(sle, row, 67589.0, -67589.0, "IRR")
		payload = set_value.call_args[0][2]
		self.assertEqual(payload["basic_amount"], 67589.0)
		self.assertEqual(payload["amount"], 67689.0)


if __name__ == "__main__":
	unittest.main()

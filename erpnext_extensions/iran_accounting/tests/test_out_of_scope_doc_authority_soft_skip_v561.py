# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.6.1: document-authority FAIL_CLOSED must soft-skip out-of-scope RIV cascades."""

from __future__ import annotations

import unittest
from unittest import mock

import frappe


def _make_sle(**extra):
	base = dict(
		company="اسپاد فارمد",
		voucher_type="Stock Entry",
		voucher_no="MAT-STE-X",
		voucher_detail_no="row1",
		item_code="30100298",
		warehouse="WH-WIP",
		actual_qty=-24.0,
		qty_after_transaction=0.0,
		stock_value=0.0,
		stock_value_difference=-100.0,
		outgoing_rate=1.0,
		incoming_rate=0.0,
		valuation_rate=1.0,
		is_cancelled=0,
	)
	base.update(extra)
	return frappe._dict(base)


def _make_row(**extra):
	base = dict(
		qty=24.0,
		transfer_qty=24.0,
		basic_rate=1.0,
		basic_amount=24.0,
		additional_cost=0.0,
		landed_cost_voucher_amount=0.0,
		valuation_rate=1.0,
		amount=24.0,
		allow_zero_valuation_rate=0,
		set_basic_rate_manually=0,
		parent="MAT-STE-X",
		serial_and_batch_bundle=None,
		s_warehouse="WH-WIP",
		t_warehouse=None,
	)
	base.update(extra)
	return frappe._dict(base)


class TestOutOfScopeDocAuthoritySoftSkip(unittest.TestCase):
	def _run_sync(self, *, in_scope: bool, item_code: str):
		from erpnext_extensions.iran_accounting.domain import stock_entry_sync as ses
		from erpnext_extensions.iran_accounting.domain.riv_valuation_guard import (
			ValuationIntegrityError,
		)

		sle = _make_sle(item_code=item_code)
		row = _make_row()
		engine = mock.Mock()
		frappe.local.iran_riv_update_entries_after = engine

		def fake_get_value(doctype, name, fields=None, as_dict=False, **kwargs):
			if doctype == "Stock Entry Detail":
				return row if as_dict or isinstance(fields, (list, tuple)) else row
			if doctype == "Stock Entry":
				return "Manufacture"
			return None

		try:
			with mock.patch.object(ses, "is_irr_company", return_value=True), mock.patch.object(
				ses, "get_company_currency", return_value="IRR"
			), mock.patch.object(ses, "round_currency", side_effect=lambda v, c: float(v)), mock.patch.object(
				ses, "should_preserve_native_outgoing_valuation", return_value=False
			), mock.patch.object(
				ses, "is_valued_source_zero_outgoing", return_value=False
			), mock.patch(
				"frappe.db.get_value", side_effect=fake_get_value
			), mock.patch.object(
				ses,
				"_apply_document_authority_movement",
				side_effect=ValuationIntegrityError(
					f"I4\nitem_code={item_code}\ndetail=manufacture_ghost_exceeds_auto_sa_ceiling"
				),
			), mock.patch(
				"erpnext_extensions.iran_accounting.domain.riv_valuation_scope.is_sle_in_riv_blocking_scope",
				return_value=in_scope,
			), mock.patch(
				"erpnext_extensions.iran_accounting.domain.riv_valuation_scope.log_out_of_scope_integrity_anomaly"
			) as log_anom:
				if in_scope:
					with self.assertRaises(ValuationIntegrityError):
						ses.sync_irr_sle_from_stock_entry_row(sle)
					log_anom.assert_not_called()
				else:
					ses.sync_irr_sle_from_stock_entry_row(sle)
					log_anom.assert_called()
		finally:
			frappe.local.iran_riv_update_entries_after = None

	def test_apply_document_authority_failure_soft_skips_out_of_scope(self):
		self._run_sync(in_scope=False, item_code="30100298")

	def test_in_scope_still_raises(self):
		self._run_sync(in_scope=True, item_code="13100057")


if __name__ == "__main__":
	unittest.main()

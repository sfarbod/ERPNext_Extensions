# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.5.46: refresh native-outgoing SED into memory before Stock Entry GL."""

from __future__ import annotations

import unittest
from unittest import mock

from erpnext_extensions.iran_accounting.domain.stock_entry_sync import (
	NATIVE_OUTGOING_STOCK_VALUATION_PURPOSES,
	refresh_native_outgoing_sed_from_db,
	sync_native_outgoing_sed_into_memory_for_gl,
)


class _Row:
	def __init__(self, **kw):
		self.__dict__.update(kw)

	def get(self, k, default=None):
		return self.__dict__.get(k, default)


class _Doc:
	def __init__(self, **kw):
		self.__dict__.update(kw)
		self.items = kw.get("items") or []

	def get(self, k, default=None):
		if k == "items":
			return self.items
		return self.__dict__.get(k, default)

	def set_total_incoming_outgoing_value(self):
		inc = out = 0.0
		for row in self.items:
			amt = float(row.amount or 0)
			if row.get("t_warehouse"):
				inc += amt
			if row.get("s_warehouse"):
				out += amt
		self.total_incoming_value = inc
		self.total_outgoing_value = out
		self.value_difference = abs(inc - out)


class TestNativeTransferGlSyncV5546(unittest.TestCase):
	def test_mtfm_in_native_outgoing_purposes(self):
		self.assertIn("Material Transfer for Manufacture", NATIVE_OUTGOING_STOCK_VALUATION_PURPOSES)
		self.assertIn("Material Transfer", NATIVE_OUTGOING_STOCK_VALUATION_PURPOSES)

	def test_refresh_replaces_stale_integer_sed_with_native_db_amount(self):
		row = _Row(
			name="sed1",
			idx=1,
			item_code="13100023",
			s_warehouse="WIP",
			t_warehouse="Approved",
			qty=1400,
			transfer_qty=1400,
			basic_rate=659957,
			valuation_rate=659957,
			basic_amount=923939800,
			amount=923939800,
			set_basic_rate_manually=0,
		)
		doc = _Doc(
			doctype="Stock Entry",
			company="C",
			purpose="Material Transfer for Manufacture",
			items=[row],
		)
		live = mock.Mock(
			basic_rate=659957,
			valuation_rate=659957,
			basic_amount=923940169,
			amount=923940169,
		)
		with mock.patch(
			"erpnext_extensions.iran_accounting.domain.stock_entry_sync.frappe.db.get_value",
			return_value=live,
		):
			refresh_native_outgoing_sed_from_db(doc)
		self.assertEqual(row.amount, 923940169)
		self.assertEqual(row.basic_amount, 923940169)

	def test_sync_for_gl_updates_header_totals_for_cross_account_transfer(self):
		row = _Row(
			name="sed1",
			idx=1,
			item_code="X",
			s_warehouse="WIP",
			t_warehouse="Approved",
			basic_rate=100,
			valuation_rate=100,
			basic_amount=1000,
			amount=1000,
			set_basic_rate_manually=0,
		)
		doc = _Doc(
			doctype="Stock Entry",
			company="C",
			purpose="Material Transfer for Manufacture",
			items=[row],
			total_incoming_value=1000,
			total_outgoing_value=1000,
			value_difference=0,
		)
		live = mock.Mock(
			basic_rate=100,
			valuation_rate=100,
			basic_amount=1369,
			amount=1369,
		)
		with (
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.stock_entry_sync.is_irr_company",
				return_value=True,
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.currency.is_irr_company",
				return_value=True,
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.stock_entry_sync.frappe.db.get_value",
				return_value=live,
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.stock_entry.align_zero_value_transfer_totals",
				return_value=None,
			),
		):
			ok = sync_native_outgoing_sed_into_memory_for_gl(doc)
		self.assertTrue(ok)
		self.assertEqual(row.amount, 1369)
		self.assertEqual(doc.total_incoming_value, 1369)
		self.assertEqual(doc.total_outgoing_value, 1369)

	def test_sync_for_gl_skips_manufacture(self):
		doc = _Doc(
			doctype="Stock Entry",
			company="C",
			purpose="Manufacture",
			items=[],
		)
		with mock.patch(
			"erpnext_extensions.iran_accounting.domain.currency.is_irr_company",
			return_value=True,
		):
			self.assertFalse(sync_native_outgoing_sed_into_memory_for_gl(doc))

	def test_sync_for_gl_skips_non_irr(self):
		doc = _Doc(
			doctype="Stock Entry",
			company="C",
			purpose="Material Transfer for Manufacture",
			items=[],
		)
		with mock.patch(
			"erpnext_extensions.iran_accounting.domain.currency.is_irr_company",
			return_value=False,
		):
			self.assertFalse(sync_native_outgoing_sed_into_memory_for_gl(doc))

	def test_expected_transfer_gl_uses_refreshed_amount(self):
		from erpnext_extensions.iran_accounting.zero_value_transfer import (
			expected_balanced_transfer_gl_magnitude,
		)

		row = _Row(
			name="sed1",
			s_warehouse="WIP",
			t_warehouse="Approved",
			amount=1207278169,
		)
		doc = _Doc(doctype="Stock Entry", company="C", purpose="Material Transfer for Manufacture", items=[row])

		def _inv_dict(item_row, _map, warehouse_field):
			wh = item_row.get(warehouse_field)
			acc = "111701" if wh == "WIP" else "111620"
			return {"account": acc}

		doc.get_inventory_account_map = mock.Mock(return_value={})
		doc.get_inventory_account_dict = _inv_dict
		self.assertEqual(expected_balanced_transfer_gl_magnitude(doc), 1207278169)

	def test_same_account_transfer_excluded_from_expected_magnitude(self):
		from erpnext_extensions.iran_accounting.zero_value_transfer import (
			expected_balanced_transfer_gl_magnitude,
		)

		row = _Row(
			name="sed1",
			s_warehouse="A",
			t_warehouse="B",
			amount=110,
		)
		doc = _Doc(doctype="Stock Entry", company="C", purpose="Material Transfer", items=[row])

		def _inv_dict(item_row, _map, warehouse_field):
			return {"account": "111625"}

		doc.get_inventory_account_map = mock.Mock(return_value={})
		doc.get_inventory_account_dict = _inv_dict
		self.assertEqual(expected_balanced_transfer_gl_magnitude(doc), 0.0)

	def test_fractional_pool_fallback_when_net_sabe_qty_negative(self):
		from erpnext_extensions.iran_accounting.domain.temporary_valuation_adjustment import (
			resolve_fractional_batch_pool,
		)

		row = _Row(
			name="sed1",
			item_code="13100023",
			batch_no="BATCH-X",
			s_warehouse="WIP",
			qty=1400,
			transfer_qty=1400,
		)
		doc = _Doc(doctype="Stock Entry", company="C", purpose="Material Transfer for Manufacture")
		with (
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.temporary_valuation_adjustment.get_company_currency",
				return_value="IRR",
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.temporary_valuation_adjustment.amount_is_fractional",
				side_effect=lambda v, _c: abs(float(v) - round(float(v))) > 1e-9,
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.stock_entry_prevention._warehouse_balance",
				return_value=(5000.0, 1.0),
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.temporary_valuation_adjustment._inward_batch_avg_at_warehouse",
				return_value=(10000.0, 6571043073.989, 657104.3073989),
			),
		):
			pool = resolve_fractional_batch_pool(doc, row, bal_qty=-79320.0, bal_value=-8329753222.042)
		self.assertIsNotNone(pool)
		pool_qty, _pv, avg, source = pool
		self.assertEqual(pool_qty, 1400.0)
		self.assertAlmostEqual(avg, 657104.3073989)
		self.assertEqual(source, "inward_avg_fallback")

	def test_observed_native_fractional_sabe_is_trace_only(self):
		from erpnext_extensions.iran_accounting.domain.temporary_valuation_adjustment import (
			CATEGORY_TRACE_ONLY,
			REASON_HISTORICAL_BATCH_RATE_MISMATCH,
			classify_observed_native_fractional_sabe,
		)

		doc = _Doc(
			doctype="Stock Entry",
			name="SE-TEST",
			company="C",
			purpose="Material Transfer for Manufacture",
			posting_date="2026-10-13",
			posting_time="12:00:00",
		)
		fake_rows = [
			mock.Mock(
				sabe_name="sabe1",
				batch_no="B1",
				qty=-1400,
				incoming_rate=659957.263789869,
				stock_value_difference=-923940169.3058165,
				warehouse="WIP",
				voucher_detail_no="sed1",
				item_code="13100023",
			)
		]
		with (
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.temporary_valuation_adjustment.get_company_currency",
				return_value="IRR",
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.temporary_valuation_adjustment.resolve_stock_adjustment_account",
				return_value="621301",
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.temporary_valuation_adjustment.amount_is_fractional",
				return_value=True,
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.temporary_valuation_adjustment.pending_adjustments",
				return_value=[],
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.temporary_valuation_adjustment.frappe.db.sql",
				return_value=fake_rows,
			),
		):
			out = classify_observed_native_fractional_sabe(doc)
		self.assertEqual(len(out), 1)
		self.assertEqual(out[0]["reason_code"], REASON_HISTORICAL_BATCH_RATE_MISMATCH)
		self.assertEqual(out[0]["accounting_category"], CATEGORY_TRACE_ONLY)
		self.assertEqual(out[0]["posting_mode"], "TRACE")
		self.assertEqual(out[0]["debit_credit"], "None")

	def test_sync_idempotent_second_refresh(self):
		row = _Row(
			name="sed1",
			s_warehouse="WIP",
			t_warehouse="PKG",
			basic_rate=659957,
			valuation_rate=659957,
			basic_amount=923940169,
			amount=923940169,
			set_basic_rate_manually=0,
		)
		doc = _Doc(
			doctype="Stock Entry",
			company="C",
			purpose="Material Transfer for Manufacture",
			items=[row],
		)
		live = mock.Mock(
			basic_rate=659957,
			valuation_rate=659957,
			basic_amount=923940169,
			amount=923940169,
		)
		with mock.patch(
			"erpnext_extensions.iran_accounting.domain.stock_entry_sync.frappe.db.get_value",
			return_value=live,
		):
			refresh_native_outgoing_sed_from_db(doc)
			refresh_native_outgoing_sed_from_db(doc)
		self.assertEqual(row.amount, 923940169)

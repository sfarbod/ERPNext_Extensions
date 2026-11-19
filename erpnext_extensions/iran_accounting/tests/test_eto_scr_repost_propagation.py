# Copyright (c) 2026, ERPNext Extensions contributors
"""ETO Subcontracting Receipt cost must propagate on repost (v5.8.1).

See docs/investigations/ETO_SCR_REPOST_PROPAGATION.md.

Root cause: the integer SABE policy localized outward Serial and Batch Entry
rows from a stale ``outgoing_rate`` instead of Core's fresh batch-wise rate
(written to ``incoming_rate``), so an upstream SCR correction was overwritten
with the historical rate on every repost.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import frappe

from erpnext_extensions.iran_accounting.domain import eto_scr_repost_scope as scope


class TestEtoEmptyWarehouseBatchAlign(unittest.TestCase):
	"""v5.8.3 — empty-warehouse ETO FG must prefer batch SABE over MA residual."""

	def test_helper_requires_eto_repost_and_bundle(self):
		sle = frappe._dict(
			{
				"item_code": "30600001",
				"actual_qty": -1854.0,
				"qty_after_transaction": 0.0,
				"serial_and_batch_bundle": "bundle-1",
			}
		)
		with patch.object(scope, "is_eto_repost_in_progress", return_value=False):
			self.assertFalse(scope.should_align_empty_warehouse_sle_from_batch_sabe(sle))
		with patch.object(scope, "is_eto_repost_in_progress", return_value=True), patch.object(
			scope, "is_eto_finished_good", return_value=True
		):
			self.assertTrue(scope.should_align_empty_warehouse_sle_from_batch_sabe(sle))
			sle2 = frappe._dict(sle)
			sle2.serial_and_batch_bundle = None
			self.assertFalse(scope.should_align_empty_warehouse_sle_from_batch_sabe(sle2))

ETO_SERVICE_ITEM = "23000001"
ETO_FG_ITEM = "30600001"

# Staging reproduction (MAT-SCR-2026-00016 → MAT-STE-2026-26587 outward leg).
OLD_RATE = 1744604.0
NEW_RATE = 2216580.0


def _row(qty, incoming_rate, outgoing_rate):
	return frappe._dict(qty=qty, incoming_rate=incoming_rate, outgoing_rate=outgoing_rate)


class TestSelectSabeRawRate(unittest.TestCase):
	def test_eto_repost_outward_uses_core_fresh_rate_over_stale_outgoing(self):
		row = _row(-61, NEW_RATE, OLD_RATE)
		self.assertEqual(scope.select_sabe_raw_rate(row, prefer_core_outward_rate=True), NEW_RATE)

	def test_non_eto_outward_keeps_stamped_outgoing_rate(self):
		row = _row(-61, NEW_RATE, OLD_RATE)
		self.assertEqual(scope.select_sabe_raw_rate(row, prefer_core_outward_rate=False), OLD_RATE)

	def test_eto_repost_sub_rial_difference_keeps_outgoing_rate(self):
		row = _row(-61, OLD_RATE + 0.3, OLD_RATE)
		self.assertEqual(scope.select_sabe_raw_rate(row, prefer_core_outward_rate=True), OLD_RATE)

	def test_eto_repost_zero_core_rate_keeps_outgoing_rate(self):
		row = _row(-61, 0.0, OLD_RATE)
		self.assertEqual(scope.select_sabe_raw_rate(row, prefer_core_outward_rate=True), OLD_RATE)

	def test_inward_rows_unchanged(self):
		row = _row(61, NEW_RATE, 0.0)
		self.assertEqual(scope.select_sabe_raw_rate(row, prefer_core_outward_rate=True), NEW_RATE)
		self.assertEqual(scope.select_sabe_raw_rate(row, prefer_core_outward_rate=False), NEW_RATE)

	def test_outward_without_stamp_uses_incoming(self):
		row = _row(-61, NEW_RATE, 0.0)
		self.assertEqual(scope.select_sabe_raw_rate(row, prefer_core_outward_rate=False), NEW_RATE)


class TestEtoRepostScope(unittest.TestCase):
	def _get_all(self, doctype, filters=None, pluck=None, limit=None, **kwargs):
		filters = filters or {}
		if doctype == "Subcontracting Receipt Item":
			return {"SCR-ETO": ["SCO-ETO"], "SCR-OTHER": ["SCO-OTHER"]}.get(filters.get("parent"), [])
		if doctype == "Subcontracting Order Service Item":
			services = set(filters["item_code"][1])
			if "parent" in filters:
				rows = {"SCO-ETO": ETO_SERVICE_ITEM, "SCO-OTHER": "29999999"}
				return [p for p in filters["parent"][1] if rows.get(p) in services]
			if "fg_item" in filters:
				return ["x"] if filters["fg_item"] == ETO_FG_ITEM and ETO_SERVICE_ITEM in services else []
		return []

	def setUp(self):
		patcher = patch.object(scope.frappe, "get_all", side_effect=self._get_all)
		patcher.start()
		self.addCleanup(patcher.stop)

	def test_transaction_repost_on_eto_scr_in_scope(self):
		doc = frappe._dict(based_on="Transaction", voucher_type="Subcontracting Receipt", voucher_no="SCR-ETO")
		self.assertTrue(scope.is_eto_repost_doc(doc))

	def test_transaction_repost_on_other_scr_out_of_scope(self):
		doc = frappe._dict(based_on="Transaction", voucher_type="Subcontracting Receipt", voucher_no="SCR-OTHER")
		self.assertFalse(scope.is_eto_repost_doc(doc))

	def test_transaction_repost_on_stock_entry_out_of_scope(self):
		doc = frappe._dict(based_on="Transaction", voucher_type="Stock Entry", voucher_no="SCR-ETO")
		self.assertFalse(scope.is_eto_repost_doc(doc))

	def test_item_warehouse_repost_for_eto_fg_in_scope(self):
		doc = frappe._dict(based_on="Item and Warehouse", item_code=ETO_FG_ITEM)
		self.assertTrue(scope.is_eto_repost_doc(doc))

	def test_item_warehouse_repost_for_other_item_out_of_scope(self):
		doc = frappe._dict(based_on="Item and Warehouse", item_code="20100043")
		self.assertFalse(scope.is_eto_repost_doc(doc))

	def test_not_in_scope_outside_repost(self):
		engine = SimpleNamespace(
			repost_doc=frappe._dict(
				name="RIV-1", based_on="Transaction", voucher_type="Subcontracting Receipt", voucher_no="SCR-ETO"
			)
		)
		with patch.object(scope.frappe, "flags", frappe._dict(through_repost_item_valuation=False)):
			self.assertFalse(scope.is_eto_repost_in_progress(engine))

	def test_in_scope_during_eto_repost_including_dependants(self):
		engine = SimpleNamespace(
			repost_doc=frappe._dict(
				name="RIV-2", based_on="Transaction", voucher_type="Subcontracting Receipt", voucher_no="SCR-ETO"
			)
		)
		setattr(frappe.local, scope._CACHE_ATTR, {})
		with patch.object(scope.frappe, "flags", frappe._dict(through_repost_item_valuation=True)):
			# Same engine processes the transfer, Manufacture consume, FG and issues.
			self.assertTrue(scope.is_eto_repost_in_progress(engine))

	def test_site_config_overrides_service_items(self):
		with patch.object(scope.frappe, "conf", frappe._dict(iran_eto_service_items=["29999999"])):
			doc = frappe._dict(based_on="Transaction", voucher_type="Subcontracting Receipt", voucher_no="SCR-OTHER")
			self.assertTrue(scope.is_eto_repost_doc(doc))


if __name__ == "__main__":
	unittest.main()

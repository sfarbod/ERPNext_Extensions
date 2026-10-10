# Copyright (c) 2026, ERPNext Extensions contributors
"""Unit tests: Job Card Rebuild Scrap→By suggestion + SE sync (approval-gated)."""

from __future__ import annotations

import unittest
from unittest import mock

import frappe

from erpnext_extensions.iran_accounting.job_card_stock_rebuild import secondary_type as st


class TestScrapToBySuggestion(unittest.TestCase):
	def test_scrap_suggests_by_when_manufacture_se_has_co_product(self):
		jc_row = {
			"name": "jcsec1",
			"idx": 1,
			"item_code": "30500006",
			"secondary_item_type": "Scrap",
			"stock_qty": 1,
			"stock_uom": "Nos",
			"bom_secondary_item": None,
			"custom_output_class": None,
			"custom_output_equivalent_factor": 0,
		}
		se_ev = frappe._dict(
			name="sed1",
			parent="MAT-STE-1",
			secondary_item_type="By-Product",
			custom_output_class="CO_PRODUCT",
			custom_output_equivalent_factor=1.0,
			qty=1,
			t_warehouse="FG",
		)
		with mock.patch.object(st, "load_jc_secondary_items", return_value=[jc_row]):
			with mock.patch.object(st, "_finished_good", return_value="20100064"):
				with mock.patch.object(st.frappe.db, "get_value", return_value=None):
					with mock.patch.object(st, "_manufacture_se_stage_evidence", return_value=se_ev):
						with mock.patch(
							"erpnext_extensions.iran_accounting.scrap_costing.is_product_reject",
							return_value=False,
						):
							out = st.suggest_secondary_type_changes("JC-1")
		row = out["rows"][0]
		self.assertEqual(row["suggested_type"], "By-Product")
		self.assertEqual(row["suggested_iran_class"], "CO_PRODUCT")
		self.assertTrue(row["approval_enabled"])
		self.assertEqual(row["action"], "TYPE_CHANGE_SUGGESTED")

	def test_ordinary_scrap_without_se_evidence_no_change(self):
		jc_row = {
			"name": "jcsec2",
			"idx": 1,
			"item_code": "SCRAP1",
			"secondary_item_type": "Scrap",
			"stock_qty": 1,
			"stock_uom": "Nos",
			"bom_secondary_item": None,
			"custom_output_class": None,
			"custom_output_equivalent_factor": 0,
		}
		with mock.patch.object(st, "load_jc_secondary_items", return_value=[jc_row]):
			with mock.patch.object(st, "_finished_good", return_value="FG"):
				with mock.patch.object(st.frappe.db, "get_value", return_value=None):
					with mock.patch.object(st, "_manufacture_se_stage_evidence", return_value=None):
						with mock.patch.object(st, "_bom_secondary_type", return_value=None):
							with mock.patch.object(st, "_jc_item_for", return_value=None):
								with mock.patch.object(
									st, "_has_component_transfer", return_value=False
								):
									with mock.patch(
										"erpnext_extensions.iran_accounting.scrap_costing.is_product_reject",
										return_value=False,
									):
										out = st.suggest_secondary_type_changes("JC-2")
		row = out["rows"][0]
		self.assertIsNone(row["suggested_type"])
		self.assertFalse(row["approval_enabled"])
		self.assertEqual(row["action"], "NO CHANGE")


class TestSeSyncOnApproval(unittest.TestCase):
	def test_sync_sets_co_product_and_equiv_when_unset(self):
		se_row = frappe._dict(
			name="sedX",
			parent="SE-X",
			secondary_item_type="Scrap",
			custom_output_class=None,
			custom_output_equivalent_factor=0,
		)
		calls = []

		def fake_set_value(doctype, name, values, update_modified=False):
			calls.append((doctype, name, values))

		with mock.patch.object(st.frappe.db, "sql", return_value=[se_row]):
			with mock.patch.object(st.frappe.db, "set_value", side_effect=fake_set_value):
				with mock.patch.object(st.frappe, "clear_document_cache"):
					out = st._sync_manufacture_se_secondary_type("JC", "ITEM", "By-Product")
		self.assertEqual(len(out), 1)
		self.assertEqual(out[0]["type"], "sed_secondary_type")
		self.assertEqual(calls[0][0], "Stock Entry Detail")
		self.assertEqual(calls[0][2]["secondary_item_type"], "By-Product")
		self.assertEqual(calls[0][2]["custom_output_class"], "CO_PRODUCT")
		self.assertEqual(calls[0][2]["custom_output_equivalent_factor"], 1.0)

	def test_sync_noop_when_already_aligned(self):
		se_row = frappe._dict(
			name="sedY",
			parent="SE-Y",
			secondary_item_type="By-Product",
			custom_output_class="CO_PRODUCT",
			custom_output_equivalent_factor=1.0,
		)
		with mock.patch.object(st.frappe.db, "sql", return_value=[se_row]):
			with mock.patch.object(st.frappe.db, "set_value") as set_value:
				out = st._sync_manufacture_se_secondary_type("JC", "ITEM", "By-Product")
		self.assertEqual(out[0]["type"], "sed_secondary_type_noop")
		set_value.assert_not_called()

	def test_approval_required_rejects_unapprovable(self):
		suggestions = [
			{
				"secondary_row": "r1",
				"current_type": "Scrap",
				"suggested_type": "By-Product",
				"approval_enabled": False,
				"evidence_fingerprint": "abc",
				"item_code": "X",
			}
		]
		with mock.patch.object(st.frappe.db, "exists", return_value=True):
			with mock.patch.object(st.frappe.db, "get_value", side_effect=["JC-1", "Scrap"]):
				v = st.validate_type_approvals(
					"JC-1",
					[
						{
							"job_card": "JC-1",
							"secondary_row": "r1",
							"current_type": "Scrap",
							"approved_type": "By-Product",
							"evidence_fingerprint": "abc",
						}
					],
					suggestions,
				)
		self.assertFalse(v["ok"])

# Copyright (c) 2026, ERPNext Extensions contributors
"""Phase 5D — Transfer Repair Group, residual policy, idempotency."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from frappe.utils import flt

from erpnext_extensions.iran_accounting.historical_stock.transfer_convergence import (
	ALREADY_REPAIRED,
	ECONOMIC_DIFFERENCE,
	REPAIRED_TO_NO_ACTION,
	ROUNDING_NO_ACTION,
	build_transfer_repair_group,
	classify_convergence_outcome,
	fingerprints_equal,
	pair_value_consistent,
	residual_class,
	transfer_already_balanced,
)


class TestTransferConvergenceV530(unittest.TestCase):
	def test_residual_rounding_vs_economic(self):
		self.assertEqual(residual_class(0.0), ROUNDING_NO_ACTION)
		self.assertEqual(residual_class(1e-9), ROUNDING_NO_ACTION)
		# Sub-IRR but above float unit is still economic — do not hide.
		self.assertEqual(residual_class(0.5), ECONOMIC_DIFFERENCE)
		self.assertEqual(residual_class(50.0), ECONOMIC_DIFFERENCE)

	def test_pair_value_consistent(self):
		self.assertTrue(pair_value_consistent({"svd": -1000}, {"svd": 1000}))
		self.assertFalse(pair_value_consistent({"svd": -1000}, {"svd": 998}))

	def test_fingerprints_equal(self):
		a = [{"name": "1", "svd": 10.0, "incoming_rate": 5}]
		b = [{"name": "1", "svd": 10.0, "incoming_rate": 5}]
		c = [{"name": "1", "svd": 11.0, "incoming_rate": 5}]
		self.assertTrue(fingerprints_equal(a, b))
		self.assertFalse(fingerprints_equal(a, c))

	def test_group_key_is_item_batch_detail_scoped(self):
		sed = MagicMock(
			item_code="ITEM-A",
			s_warehouse="WH-S",
			t_warehouse="WH-T",
			batch_no="B1",
			qty=2,
			basic_rate=10,
			valuation_rate=10,
			amount=20,
			serial_and_batch_bundle=None,
		)
		sed.name = "SED1"

		def _get_value(doctype, name, field=None, as_dict=False, **kwargs):
			if doctype == "Stock Entry":
				return "Material Transfer"
			if doctype == "Stock Entry Detail":
				return sed if as_dict else "SED1"
			return None

		def _sql(query, args=None, as_dict=False):
			q = str(query)
			if "Stock Ledger Entry" in q:
				return []
			if "Stock Entry Detail" in q:
				return []
			return []

		with (
			patch(
				"erpnext_extensions.iran_accounting.historical_stock.transfer_convergence.frappe.db.get_value",
				side_effect=_get_value,
			),
			patch(
				"erpnext_extensions.iran_accounting.historical_stock.transfer_convergence.frappe.db.sql",
				side_effect=_sql,
			),
		):
			g = build_transfer_repair_group(
				{
					"voucher": "STE-1",
					"item": "ITEM-A",
					"batch": "B1",
					"voucher_detail": "SED1",
					"purpose": "Material Transfer",
					"s_warehouse": "WH-S",
					"t_warehouse": "WH-T",
				}
			)
		self.assertEqual(g["scope"], "ITEM_BATCH_VOUCHER_DETAIL")
		self.assertEqual(g["group_key"], "STE-1|ITEM-A|B1|SED1")

	def test_transfer_already_balanced(self):
		out = MagicMock(
			actual_qty=-2,
			stock_value_difference=-200,
			incoming_rate=0,
			outgoing_rate=100,
			name="OUT",
		)
		inn = MagicMock(
			actual_qty=2,
			stock_value_difference=200,
			incoming_rate=100,
			outgoing_rate=0,
			name="IN",
		)
		sed = MagicMock(basic_rate=100, valuation_rate=100, name="SED1", qty=2)
		with (
			patch(
				"erpnext_extensions.iran_accounting.historical_stock.transfer_convergence.build_transfer_repair_group",
				return_value={
					"group_key": "STE|I||SED1",
					"voucher": "STE",
					"item": "I",
					"batch": "",
					"voucher_detail": "SED1",
					"purpose": "Material Transfer",
					"s_warehouse": "S",
					"t_warehouse": "T",
					"se_detail": sed,
					"sle_members": [out, inn],
					"sibling_details": [],
					"scope": "ITEM_BATCH_VOUCHER_DETAIL",
				},
			),
			patch(
				"erpnext_extensions.iran_accounting.historical_stock.transfer_convergence.transfer_pair_legs",
				return_value=(out, inn),
			),
		):
			bal = transfer_already_balanced(
				{"purpose": "Material Transfer", "voucher": "STE", "item": "I"}
			)
		self.assertIsNotNone(bal)
		self.assertEqual(bal["status"], ALREADY_REPAIRED)
		self.assertEqual(bal["economic_writes"], 0)

	def test_classify_repaired_to_no_action(self):
		before = {"reconstruction": {"classification": "EXACT", "diff": 50}}
		after = {"reconstruction": {"classification": "NO_ACTION", "diff": 0}}
		outcome = classify_convergence_outcome(
			before=before,
			after=after,
			apply_result={"aborted": False, "applied": [{"written": True}]},
			second_fp_equal=True,
			sibling_still_exact=False,
		)
		self.assertEqual(outcome, REPAIRED_TO_NO_ACTION)

	def test_wrong_rate_selected_skips_sle_path_when_detail_present(self):
		"""surface=SLE + voucher_detail must not double-apply (idempotency leak)."""
		from erpnext_extensions.iran_accounting.historical_stock.reconstruct import (
			repair_wrong_rate_selected,
		)

		row = {
			"voucher": "STE-1",
			"item": "I",
			"voucher_detail": "SED1",
			"surface": "SLE",
			"purpose": "Material Transfer",
			"proposed_rate": 100,
			"qty": 1,
		}
		with patch(
			"erpnext_extensions.iran_accounting.historical_stock.reconstruct.repair_zero_rate_selected",
			return_value={
				"dry_run": False,
				"aborted": False,
				"applied": [
					{
						**row,
						"written": False,
						"status": ALREADY_REPAIRED,
						"economic_writes": 0,
					}
				],
				"blocked": [],
			},
		) as zero_fn:
			result = repair_wrong_rate_selected([row], dry_run=False)
		zero_fn.assert_called_once()
		# Only the SE/Transfer path result — no extra SLE-only written=True append.
		self.assertEqual(len(result["applied"]), 1)
		self.assertEqual(result["applied"][0]["status"], ALREADY_REPAIRED)
		self.assertFalse(result["applied"][0]["written"])

	def test_waiting_apply_classifies_blocked_not_still_exact(self):
		from erpnext_extensions.iran_accounting.historical_stock.transfer_convergence import BLOCKED
		from erpnext_extensions.iran_accounting.historical_stock.transfer_valuation import (
			WAITING_UPSTREAM,
		)

		before = {"reconstruction": {"classification": "EXACT", "diff": 50}}
		after = {"reconstruction": {"classification": "EXACT", "diff": 50}}
		outcome = classify_convergence_outcome(
			before=before,
			after=after,
			apply_result={
				"aborted": False,
				"applied": [
					{"status": WAITING_UPSTREAM, "written": False, "economic_writes": 0}
				],
			},
			second_fp_equal=True,
		)
		self.assertEqual(outcome, BLOCKED)

	def test_zero_outgoing_svd_refuses_previous_healthy_invention(self):
		"""Qty left source with svd=0 must not invent rate from previous_healthy."""
		from erpnext_extensions.iran_accounting.historical_stock.transfer_valuation import (
			WAITING_UPSTREAM,
			reconstruct_transfer_valuation,
		)

		out = MagicMock(
			name="OUT",
			warehouse="WH-S",
			actual_qty=-10,
			outgoing_rate=0,
			stock_value_difference=0,
			qty_after_transaction=0,
			stock_value=0,
			batch_no=None,
		)
		inn = MagicMock(
			name="IN",
			warehouse="WH-T",
			actual_qty=10,
			incoming_rate=100,
			stock_value_difference=1000,
		)
		with (
			patch(
				"erpnext_extensions.iran_accounting.historical_stock.transfer_valuation._outgoing_sle",
				return_value=out,
			),
			patch(
				"erpnext_extensions.iran_accounting.historical_stock.transfer_valuation._incoming_sle",
				return_value=inn,
			),
			patch(
				"erpnext_extensions.iran_accounting.historical_stock.transfer_valuation._source_upstream_health",
				return_value={"status": "healthy", "dependencies": [], "root_voucher": None},
			),
			patch(
				"erpnext_extensions.iran_accounting.historical_stock.transfer_valuation._previous_healthy",
				return_value={"rate": 999, "voucher_no": "STE-OLD"},
			),
		):
			recon = reconstruct_transfer_valuation(
				{
					"voucher": "STE-1",
					"item": "I",
					"purpose": "Material Transfer for Manufacture",
					"s_warehouse": "WH-S",
					"t_warehouse": "WH-T",
					"qty": 10,
					"current_rate": 999,
					"posting_date": "2026-01-01",
					"posting_time": "10:00:00",
				}
			)
		self.assertEqual(recon["classification"], WAITING_UPSTREAM)
		self.assertEqual(flt(recon.get("expected_rate") or 0), 0.0)
		self.assertIn("zero stock_value_difference", recon.get("reason") or "")


if __name__ == "__main__":
	unittest.main()

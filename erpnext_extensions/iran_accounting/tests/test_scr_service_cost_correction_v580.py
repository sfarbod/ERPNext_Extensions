# Copyright (c) 2026, ERPNext Extensions contributors
"""Tests for reusable SCR service-cost correction (PO amount ÷ FG qty)."""

from __future__ import annotations

import unittest
from unittest.mock import patch

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.subcontracting_receipt_service_cost import (
	CONFIRM_APPLY,
	SERVICE_COST_TOLERANCE,
)
from erpnext_extensions.iran_accounting.subcontracting_receipt_service_cost.allocation import (
	ScrServiceCostAllocationError,
	allocation_within_cap,
	classify_fee_type,
	intended_service_cost_per_qty,
)
from erpnext_extensions.iran_accounting.subcontracting_receipt_service_cost.service import (
	_propose_item_correction,
	process_allowlist,
)


class TestScrServiceCostAllocationMath(unittest.TestCase):
	def test_fixed_total_eto_reference_case(self):
		# PUR-ORD-2026-01395 / MAT-SCR-2026-00015 reference numbers
		intended = intended_service_cost_per_qty(po_service_amount=145_122_080, sco_fg_qty=2015)
		self.assertAlmostEqual(intended, 72_020.88337469, places=8)
		self.assertEqual(classify_fee_type(po_service_qty=4, fg_item_qty=2015, sco_fg_qty=2015), "fixed_total_for_operation")

	def test_per_unit_when_service_qty_equals_fg_qty(self):
		self.assertEqual(
			classify_fee_type(po_service_qty=100, fg_item_qty=100, sco_fg_qty=100),
			"per_unit",
		)
		self.assertAlmostEqual(
			intended_service_cost_per_qty(po_service_amount=50_000, sco_fg_qty=100),
			500.0,
		)

	def test_zero_fg_qty_raises(self):
		with self.assertRaises(ScrServiceCostAllocationError):
			intended_service_cost_per_qty(po_service_amount=100, sco_fg_qty=0)

	def test_partial_receipts_cap(self):
		intended = intended_service_cost_per_qty(po_service_amount=145_122_080, sco_fg_qty=2015)
		cap = allocation_within_cap(
			po_service_amount=145_122_080,
			sibling_allocations=[
				{"subcontracting_receipt": "SCR-A", "qty": 1000, "service_cost_per_qty": intended},
				{"subcontracting_receipt": "SCR-B", "qty": 1015, "service_cost_per_qty": intended},
			],
		)
		self.assertTrue(cap["within_cap"])
		self.assertAlmostEqual(cap["total_allocated"], 145_122_080, places=4)

	def test_full_po_amount_on_each_partial_exceeds_cap(self):
		# Anti-pattern: giving every SCR the full PO amount as a "per unit" of 1
		cap = allocation_within_cap(
			po_service_amount=145_122_080,
			sibling_allocations=[
				{"subcontracting_receipt": "SCR-A", "qty": 1000, "service_cost_per_qty": 145_122_080 / 1000},
				{"subcontracting_receipt": "SCR-B", "qty": 1015, "service_cost_per_qty": 145_122_080 / 1015},
			],
		)
		self.assertFalse(cap["within_cap"])

	def test_last_purchase_rate_must_not_become_service_cost(self):
		po_amount = 145_122_080
		lpr = 122_787
		intended = intended_service_cost_per_qty(po_service_amount=po_amount, sco_fg_qty=2015)
		self.assertNotAlmostEqual(intended, lpr, places=2)
		# Simulated wrong SCR that used LPR
		wrong = _propose_item_correction(
			{
				"name": "row1",
				"item_code": "30600001",
				"qty": 2015,
				"service_cost_per_qty": lpr,
				"rm_cost_per_qty": 1_722_226,
				"additional_cost_per_qty": 0,
				"rate": lpr + 1_722_226,
				"amount": 2015 * (lpr + 1_722_226),
				"subcontracting_order_item": "sco-item",
			},
			{
				"intended_service_cost_per_qty": intended,
				"fee_type": "fixed_total_for_operation",
				"po_service_amount": po_amount,
				"po_last_purchase_rate": lpr,
				"purchase_order": "PO",
				"purchase_order_item": "poi",
				"subcontracting_order": "SCO",
			},
		)
		self.assertFalse(wrong["already_correct"])
		self.assertFalse(wrong["used_last_purchase_rate"])
		self.assertTrue(wrong["rm_preserved"])
		self.assertAlmostEqual(wrong["after"]["rm_cost_per_qty"], 1_722_226)
		self.assertAlmostEqual(wrong["after"]["service_cost_per_qty"], intended, places=8)
		self.assertAlmostEqual(
			wrong["after"]["rate"],
			1_722_226 + intended,
			places=8,
		)

	def test_already_correct_proposal(self):
		intended = 72_020.88337469
		prop = _propose_item_correction(
			{
				"name": "row1",
				"qty": 2015,
				"service_cost_per_qty": intended,
				"rm_cost_per_qty": 1_722_226,
				"additional_cost_per_qty": 0,
				"rate": 1_722_226 + intended,
				"amount": 2015 * (1_722_226 + intended),
			},
			{
				"intended_service_cost_per_qty": intended,
				"fee_type": "fixed_total_for_operation",
				"po_service_amount": 145_122_080,
				"po_last_purchase_rate": 122_787,
				"purchase_order": "PO",
				"purchase_order_item": "poi",
				"subcontracting_order": "SCO",
			},
		)
		self.assertTrue(prop["already_correct"])
		self.assertLessEqual(abs(prop["delta_service_cost_per_qty"]), SERVICE_COST_TOLERANCE)


class TestScrServiceCostApiGuards(unittest.TestCase):
	def test_confirm_token_required_for_apply(self):
		with patch(
			"erpnext_extensions.iran_accounting.subcontracting_receipt_service_cost.service.require_privileged_user",
			return_value="Administrator",
		), patch(
			"erpnext_extensions.iran_accounting.subcontracting_receipt_service_cost.service.require_post_for_mutation",
		):
			with self.assertRaises(Exception):
				process_allowlist(
					["MAT-SCR-FAKE"],
					dry_run=False,
					confirm="WRONG",
				)

	def test_dry_run_does_not_require_confirm(self):
		with patch(
			"erpnext_extensions.iran_accounting.subcontracting_receipt_service_cost.service.require_privileged_user",
			return_value="Administrator",
		), patch(
			"erpnext_extensions.iran_accounting.subcontracting_receipt_service_cost.service.inspect_subcontracting_receipt",
			return_value={
				"eligible": True,
				"action": "correct",
				"fingerprint": "abc",
				"items": [],
				"reasons": [],
			},
		):
			out = process_allowlist(["MAT-SCR-FAKE"], dry_run=True)
			self.assertTrue(out["dry_run"])
			self.assertEqual(out["results"][0]["status"], "dry_run")


class TestMultipleServiceAndFgScenarios(unittest.TestCase):
	def test_multiple_independent_service_amounts(self):
		# Two independent PO service amounts / FG qtys
		a = intended_service_cost_per_qty(po_service_amount=100_000, sco_fg_qty=50)
		b = intended_service_cost_per_qty(po_service_amount=200_000, sco_fg_qty=100)
		self.assertAlmostEqual(a, 2000.0)
		self.assertAlmostEqual(b, 2000.0)
		# Caps are independent
		cap_a = allocation_within_cap(
			po_service_amount=100_000,
			sibling_allocations=[{"qty": 50, "service_cost_per_qty": a}],
		)
		cap_b = allocation_within_cap(
			po_service_amount=200_000,
			sibling_allocations=[{"qty": 100, "service_cost_per_qty": b}],
		)
		self.assertTrue(cap_a["within_cap"])
		self.assertTrue(cap_b["within_cap"])

	def test_duplicate_execution_idempotent_math(self):
		intended = intended_service_cost_per_qty(po_service_amount=10_000, sco_fg_qty=10)
		first = _propose_item_correction(
			{"qty": 10, "service_cost_per_qty": 0, "rm_cost_per_qty": 5, "additional_cost_per_qty": 0, "rate": 5, "amount": 50},
			{
				"intended_service_cost_per_qty": intended,
				"fee_type": "per_unit",
				"po_service_amount": 10_000,
				"po_last_purchase_rate": 999,
				"purchase_order": "PO",
				"purchase_order_item": "poi",
				"subcontracting_order": "SCO",
			},
		)
		second = _propose_item_correction(
			{
				"qty": 10,
				"service_cost_per_qty": first["after"]["service_cost_per_qty"],
				"rm_cost_per_qty": 5,
				"additional_cost_per_qty": 0,
				"rate": first["after"]["rate"],
				"amount": first["after"]["amount"],
			},
			{
				"intended_service_cost_per_qty": intended,
				"fee_type": "per_unit",
				"po_service_amount": 10_000,
				"po_last_purchase_rate": 999,
				"purchase_order": "PO",
				"purchase_order_item": "poi",
				"subcontracting_order": "SCO",
			},
		)
		self.assertFalse(first["already_correct"])
		self.assertTrue(second["already_correct"])


def _site_has_reference_scr() -> bool:
	return bool(frappe.db.exists("Subcontracting Receipt", "MAT-SCR-2026-00015"))


@unittest.skipUnless(
	False,  # enable manually on Dev restored site: set to _site_has_reference_scr()
	"Optional live Dev dry-run against restored reference SCR",
)
class TestLiveReferenceDryRun(unittest.TestCase):
	def test_dry_run_mat_scr_2026_00015(self):
		from erpnext_extensions.iran_accounting.subcontracting_receipt_service_cost.service import (
			inspect_subcontracting_receipt,
		)

		report = inspect_subcontracting_receipt("MAT-SCR-2026-00015")
		self.assertEqual(report["action"], "correct")
		self.assertTrue(report["eligible"])
		item = report["items"][0]
		self.assertAlmostEqual(item["after"]["service_cost_per_qty"], 72_020.88337469, places=6)
		self.assertAlmostEqual(item["before"]["service_cost_per_qty"], 122_787, places=2)
		self.assertAlmostEqual(item["after"]["rm_cost_per_qty"], 1_722_226, places=2)

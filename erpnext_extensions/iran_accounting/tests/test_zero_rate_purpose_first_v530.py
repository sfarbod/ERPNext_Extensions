# Copyright (c) 2026, ERPNext Extensions contributors
"""Unit tests — Zero Rate purpose-first semantics (v5.3.0)."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from erpnext_extensions.iran_accounting.historical_stock import (
	CONFIDENCE_EXACT,
	STATUS_MATERIAL_RECEIPT_USER_REVIEW,
	STATUS_RECONSTRUCTABLE,
	Z_MATERIAL_RECEIPT_USER_REVIEW,
)
from erpnext_extensions.iran_accounting.historical_stock.zero_rate import classify_zero_row


def _base(**kw):
	row = {
		"name": "d1",
		"idx": 1,
		"parent": "MAT-STE-TEST",
		"purpose": "Material Receipt",
		"item_code": "ITEM1",
		"qty": 10,
		"basic_rate": 0,
		"valuation_rate": 0,
		"amount": 0,
		"s_warehouse": None,
		"t_warehouse": "Stores - E",
		"secondary_item_type": None,
		"is_scrap_item": 0,
		"allow_zero_valuation_rate": 0,
		"is_finished_item": 0,
		"company": "Espad",
		"posting_date": "2026-06-01",
		"posting_time": "10:00:00",
		"batch_no": None,
		"serial_and_batch_bundle": None,
		"work_order": None,
		"job_card": None,
	}
	row.update(kw)
	return row


def _patches(
	*,
	version=(0.0, 0.0),
	batch=0.0,
	prev=0.0,
	transfer=0.0,
	patient=None,
	poisoned=False,
):
	"""Patch Zero Rate helpers + Transfer reconstruction (Phase 5B/5D path)."""

	def _fake_transfer_recon(row, cache=None):
		from erpnext_extensions.iran_accounting.historical_stock import (
			CONFIDENCE_AMBIGUOUS,
			CONFIDENCE_EXACT,
			CONFIDENCE_MANUAL,
		)
		from erpnext_extensions.iran_accounting.historical_stock.transfer_valuation import (
			EXACT,
			RECONSTRUCTABLE,
			WAITING_UPSTREAM,
			USER_ACTION_REQUIRED,
		)

		purpose = row.get("purpose") or ""
		cur = float(row.get("basic_rate") or row.get("current_rate") or 0)
		base = {
			"purpose": purpose,
			"voucher": row.get("parent") or row.get("voucher"),
			"item": row.get("item_code") or row.get("item"),
			"current_rate": cur,
			"expected_rate": 0.0,
			"upstream_health": "healthy",
			"root_voucher": row.get("parent") or row.get("voucher"),
			"authoritative_source": None,
			"reason": "",
			"confidence": CONFIDENCE_MANUAL,
			"classification": WAITING_UPSTREAM,
		}
		if poisoned:
			return {
				**base,
				"classification": WAITING_UPSTREAM,
				"upstream_health": "poisoned",
				"reason": "mocked poisoned upstream",
				"confidence": CONFIDENCE_AMBIGUOUS,
				"root_voucher": "UPSTREAM-POISON",
			}
		if float(transfer or 0) > 0:
			return {
				**base,
				"classification": EXACT,
				"expected_rate": float(transfer),
				"authoritative_source": "outgoing_sle_svd",
				"confidence": CONFIDENCE_EXACT,
				"reason": "mocked outgoing_sle_svd",
				"upstream_health": "healthy",
			}
		if float(prev or 0) > 0 and purpose in (
			"Material Issue",
			"Material Consumption for Manufacture",
			"Material Transfer",
			"Material Transfer for Manufacture",
			"Send to Subcontractor",
		):
			return {
				**base,
				"classification": RECONSTRUCTABLE,
				"expected_rate": float(prev),
				"authoritative_source": "previous_healthy_source_sle",
				"confidence": CONFIDENCE_EXACT,
				"reason": "mocked previous_healthy",
				"upstream_health": "healthy",
			}
		if purpose == "Material Issue":
			return {
				**base,
				"classification": USER_ACTION_REQUIRED,
				"reason": "no authoritative source valuation found for transfer/issue",
			}
		return {
			**base,
			"classification": WAITING_UPSTREAM,
			"upstream_health": "missing",
			"reason": "no authoritative source valuation found for transfer/issue",
			"confidence": CONFIDENCE_AMBIGUOUS,
		}

	return (
		patch(
			"erpnext_extensions.iran_accounting.historical_stock.zero_rate._version_rate",
			return_value=version,
		),
		patch(
			"erpnext_extensions.iran_accounting.historical_stock.zero_rate._batch_inward_rate",
			return_value=batch,
		),
		patch(
			"erpnext_extensions.iran_accounting.historical_stock.zero_rate._previous_healthy_sle_rate",
			return_value=prev,
		),
		patch(
			"erpnext_extensions.iran_accounting.historical_stock.zero_rate._source_transfer_sle_rate",
			return_value=transfer,
		),
		patch(
			"erpnext_extensions.iran_accounting.historical_stock.zero_rate._patient_for_identity",
			return_value=patient,
		),
		patch(
			"erpnext_extensions.iran_accounting.historical_stock.zero_rate._identity_poisoned",
			return_value=poisoned,
		),
		patch(
			"erpnext_extensions.iran_accounting.historical_stock.expected.attach_rate_analysis",
			side_effect=lambda d, *_a, **_k: d,
		),
		patch(
			"erpnext_extensions.iran_accounting.historical_stock.scrap_warehouse.is_scrap_reject_waste_warehouse",
			side_effect=lambda wh, company=None: bool(wh)
			and any(t in (wh or "").lower() for t in ("reject", "scrap", "ضایعات")),
		),
		patch(
			"erpnext_extensions.iran_accounting.historical_stock.transfer_valuation.reconstruct_transfer_valuation",
			side_effect=_fake_transfer_recon,
		),
	)


class TestZeroRatePurposeFirst(unittest.TestCase):
	def test_a_material_receipt_reject_warehouse_user_review(self):
		ctx = _patches(prev=999.0, batch=999.0)  # must NOT invent from these
		with ctx[0], ctx[1], ctx[2], ctx[3], ctx[4], ctx[5], ctx[6], ctx[7], ctx[8]:
			out = classify_zero_row(_base(t_warehouse="انبار Reject مواد اولیه اسپاد"))
		self.assertEqual(out["status"], STATUS_MATERIAL_RECEIPT_USER_REVIEW)
		self.assertEqual(out["zero_class"], Z_MATERIAL_RECEIPT_USER_REVIEW)
		self.assertFalse(out.get("eligible"))
		self.assertFalse(out.get("actionable"))
		self.assertEqual(out.get("proposed_rate"), 0)
		self.assertTrue(out.get("user_action_required"))
		self.assertIn("will not invent", (out.get("message") or "").lower())

	def test_b_material_receipt_normal_warehouse_user_review(self):
		ctx = _patches(prev=1500.0, batch=1500.0)
		with ctx[0], ctx[1], ctx[2], ctx[3], ctx[4], ctx[5], ctx[6], ctx[7], ctx[8]:
			out = classify_zero_row(_base(t_warehouse="Stores - E"))
		self.assertEqual(out["status"], STATUS_MATERIAL_RECEIPT_USER_REVIEW)
		self.assertEqual(out.get("proposed_rate"), 0)
		self.assertFalse(out.get("eligible"))

	def test_c_material_transfer_reject_reconstructs_from_source(self):
		ctx = _patches(transfer=51219.0)
		with ctx[0], ctx[1], ctx[2], ctx[3], ctx[4], ctx[5], ctx[6], ctx[7], ctx[8]:
			out = classify_zero_row(
				_base(
					purpose="Material Transfer",
					s_warehouse="Stores - E",
					t_warehouse="انبار ضایعات اقلام اسپاد",
				)
			)
		self.assertEqual(out["status"], STATUS_RECONSTRUCTABLE)
		self.assertEqual(out["proposed_rate"], 51219.0)
		self.assertEqual(out["source_of_truth"], "outgoing_sle_svd")
		self.assertTrue(out.get("eligible"))
		self.assertEqual(out.get("confidence"), CONFIDENCE_EXACT)

	def test_d_material_transfer_normal_reconstructs_from_source(self):
		ctx = _patches(transfer=100.0)
		with ctx[0], ctx[1], ctx[2], ctx[3], ctx[4], ctx[5], ctx[6], ctx[7], ctx[8]:
			out = classify_zero_row(
				_base(
					purpose="Material Transfer",
					s_warehouse="WH-A",
					t_warehouse="WH-B",
				)
			)
		self.assertEqual(out["status"], STATUS_RECONSTRUCTABLE)
		self.assertEqual(out["proposed_rate"], 100.0)
		self.assertTrue(out.get("eligible"))

	def test_e_transfer_for_manufacture_reconstructs(self):
		ctx = _patches(transfer=77.0)
		with ctx[0], ctx[1], ctx[2], ctx[3], ctx[4], ctx[5], ctx[6], ctx[7], ctx[8]:
			out = classify_zero_row(
				_base(
					purpose="Material Transfer for Manufacture",
					s_warehouse="Approved",
					t_warehouse="WIP",
				)
			)
		self.assertEqual(out["status"], STATUS_RECONSTRUCTABLE)
		self.assertEqual(out["proposed_rate"], 77.0)
		self.assertTrue(out.get("eligible"))

	def test_f_manufacture_fg_uses_issued_pool_when_available(self):
		# Manufacture with scrap row issued-pool path is covered elsewhere;
		# FG with previous_healthy / batch reconstructable.
		ctx = _patches(prev=200.0)
		with ctx[0], ctx[1], ctx[2], ctx[3], ctx[4], ctx[5], ctx[6], ctx[7], ctx[8]:
			out = classify_zero_row(
				_base(
					purpose="Manufacture",
					s_warehouse=None,
					t_warehouse="FG WH",
					is_finished_item=1,
				)
			)
		self.assertEqual(out["status"], STATUS_RECONSTRUCTABLE)
		self.assertEqual(out["proposed_rate"], 200.0)
		self.assertTrue(out.get("eligible"))

	def test_g_reject_warehouse_alone_does_not_exempt(self):
		# Material Transfer into Reject with no source → not NO_ACTION via warehouse.
		ctx = _patches()
		with ctx[0], ctx[1], ctx[2], ctx[3], ctx[4], ctx[5], ctx[6], ctx[7], ctx[8]:
			out = classify_zero_row(
				_base(
					purpose="Material Transfer",
					s_warehouse="WH-A",
					t_warehouse="انبار Reject مواد اولیه اسپاد",
				)
			)
		self.assertNotEqual(out.get("status"), "NO_ACTION_REQUIRED")
		self.assertNotEqual(out.get("zero_class"), "LEGITIMATE_SCRAP_ZERO_RATE")

	def test_h_material_receipt_never_copies_later_ma(self):
		ctx = _patches(prev=999999.0, batch=888888.0)
		with ctx[0], ctx[1], ctx[2], ctx[3], ctx[4], ctx[5], ctx[6], ctx[7], ctx[8]:
			out = classify_zero_row(_base())
		self.assertEqual(out.get("proposed_rate"), 0)
		self.assertEqual(out["status"], STATUS_MATERIAL_RECEIPT_USER_REVIEW)
		# Document-local version would be allowed — ensure MA alone is rejected.
		self.assertIn("previous_healthy_sle_not_used", out.get("reconstruction_sources") or {})

	def test_material_receipt_version_is_document_local_ok(self):
		ctx = _patches(version=(1500.0, 15000.0), batch=1500.0)
		with ctx[0], ctx[1], ctx[2], ctx[3], ctx[4], ctx[5], ctx[6], ctx[7], ctx[8]:
			out = classify_zero_row(_base())
		self.assertEqual(out["status"], STATUS_RECONSTRUCTABLE)
		self.assertEqual(out["proposed_rate"], 1500.0)
		self.assertTrue(out.get("eligible"))

	def test_material_issue_reconstructs_from_source_chain(self):
		ctx = _patches(prev=321.5)
		with ctx[0], ctx[1], ctx[2], ctx[3], ctx[4], ctx[5], ctx[6], ctx[7], ctx[8]:
			out = classify_zero_row(
				_base(
					purpose="Material Issue",
					s_warehouse="Stores - E",
					t_warehouse=None,
				)
			)
		self.assertEqual(out["status"], STATUS_RECONSTRUCTABLE)
		self.assertEqual(out["proposed_rate"], 321.5)
		self.assertTrue(out.get("eligible"))
		self.assertEqual(out.get("source_of_truth"), "previous_healthy_source_sle")

	def test_material_issue_poisoned_upstream_waits(self):
		ctx = _patches(prev=100.0, poisoned=True)
		with ctx[0], ctx[1], ctx[2], ctx[3], ctx[4], ctx[5], ctx[6], ctx[7], ctx[8]:
			out = classify_zero_row(
				_base(
					purpose="Material Issue",
					s_warehouse="Stores - E",
					t_warehouse=None,
				)
			)
		self.assertIn(out["status"], ("VALUATION_POISON_DEPENDENCY", "DEPENDENCY_REPAIR_REQUIRED"))
		self.assertFalse(out.get("eligible"))
		self.assertFalse(out.get("eligible"))

	def test_repack_output_uses_allocated_input_value(self):
		ctx = _patches()
		with ctx[0], ctx[1], ctx[2], ctx[3], ctx[4], ctx[5], ctx[6], ctx[7], ctx[8], patch(
			"erpnext_extensions.iran_accounting.historical_stock.zero_rate._repack_allocated_rate",
			return_value=88.0,
		):
			out = classify_zero_row(
				_base(
					purpose="Repack",
					s_warehouse=None,
					t_warehouse="FG WH",
					is_finished_item=1,
				)
			)
		self.assertEqual(out["status"], STATUS_RECONSTRUCTABLE)
		self.assertEqual(out["proposed_rate"], 88.0)
		self.assertEqual(out.get("source_of_truth"), "repack_allocated")
		self.assertTrue(out.get("eligible"))

	def test_repack_multi_output_allocation_helper(self):
		from erpnext_extensions.iran_accounting.historical_stock.zero_rate import _repack_allocated_rate

		details = [
			{
				"name": "src",
				"idx": 1,
				"item_code": "RM",
				"qty": 10,
				"transfer_qty": 10,
				"s_warehouse": "WH-A",
				"t_warehouse": None,
				"basic_rate": 0,
				"valuation_rate": 0,
				"is_finished_item": 0,
				"allow_zero_valuation_rate": 0,
				"additional_cost": 0,
			},
			{
				"name": "fg1",
				"idx": 2,
				"item_code": "FG1",
				"qty": 4,
				"transfer_qty": 4,
				"s_warehouse": None,
				"t_warehouse": "WH-B",
				"basic_rate": 0,
				"valuation_rate": 0,
				"is_finished_item": 1,
				"allow_zero_valuation_rate": 0,
				"additional_cost": 0,
			},
			{
				"name": "fg2",
				"idx": 3,
				"item_code": "FG2",
				"qty": 6,
				"transfer_qty": 6,
				"s_warehouse": None,
				"t_warehouse": "WH-B",
				"basic_rate": 0,
				"valuation_rate": 0,
				"is_finished_item": 1,
				"allow_zero_valuation_rate": 0,
				"additional_cost": 0,
			},
		]
		with patch(
			"erpnext_extensions.iran_accounting.historical_stock.zero_rate.frappe.db.sql",
			return_value=details,
		), patch(
			"erpnext_extensions.iran_accounting.historical_stock.zero_rate._previous_healthy_sle_rate",
			return_value=50.0,  # 10*50 = 500 → rate = 500/10 = 50 per unit
		):
			rate = _repack_allocated_rate(
				_base(
					name="fg1",
					purpose="Repack",
					item_code="FG1",
					qty=4,
					s_warehouse=None,
					t_warehouse="WH-B",
					is_finished_item=1,
				)
			)
		self.assertEqual(rate, 50.0)


class TestTransactionSemanticsRegistry(unittest.TestCase):
	def test_registry_covers_site_purposes(self):
		from erpnext_extensions.iran_accounting.historical_stock.transaction_semantics import (
			PURPOSE_REGISTRY,
			RECONSTRUCT_REPACK,
			known_stock_entry_purposes,
			purpose_semantics,
		)

		for p in (
			"Material Receipt",
			"Material Transfer",
			"Material Transfer for Manufacture",
			"Manufacture",
			"Material Issue",
			"Repack",
			"Send to Subcontractor",
		):
			self.assertIn(p, PURPOSE_REGISTRY)
			self.assertTrue(purpose_semantics(p).get("policy"))
		self.assertEqual(purpose_semantics("Repack").get("policy"), RECONSTRUCT_REPACK)
		self.assertIn("Material Issue", known_stock_entry_purposes())
		self.assertIn("Repack", known_stock_entry_purposes())


class TestCrossKpiRootGraph(unittest.TestCase):
	def test_root_chain_dedup_and_cross_kpi(self):
		from erpnext_extensions.iran_accounting.historical_stock.root_graph import (
			build_zero_wrong_root_graph,
		)

		zero = [
			{
				"voucher": "STE-ROOT",
				"purpose": "Material Transfer",
				"item": "A",
				"warehouse": "W1",
				"status": "RECONSTRUCTABLE",
				"eligible": True,
				"confidence": "EXACT",
				"posting_date": "2026-01-01",
				"proposed_rate": 10,
			},
			{
				"voucher": "STE-DOWN",
				"purpose": "Manufacture",
				"item": "B",
				"warehouse": "W2",
				"status": "DEPENDENCY_REPAIR_REQUIRED",
				"patient_zero": {"voucher_no": "STE-ROOT"},
				"posting_date": "2026-01-02",
			},
		]
		wrong = [
			{
				"voucher": "STE-DOWN",
				"purpose": "Manufacture",
				"item": "B",
				"warehouse": "W2",
				"status": "WAITING_RATE",
				"patient_zero": {"voucher_no": "STE-ROOT"},
				"flag": "MATCHED_BUT_CORRUPT",
				"posting_date": "2026-01-02",
			}
		]
		g = build_zero_wrong_root_graph(zero, wrong)
		self.assertEqual(g["ZERO_RAW_FINDINGS"], 2)
		self.assertEqual(g["WRONG_RAW_FINDINGS"], 1)
		self.assertGreaterEqual(g["CROSS_KPI_ROOT_CHAINS"], 1)
		self.assertEqual(g["MATCHED_BUT_CORRUPT"], 1)
		self.assertGreaterEqual(g["ZERO_INDEPENDENT_ROOTS"], 1)


if __name__ == "__main__":
	unittest.main()
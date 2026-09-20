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
	)


class TestZeroRatePurposeFirst(unittest.TestCase):
	def test_a_material_receipt_reject_warehouse_user_review(self):
		ctx = _patches(prev=999.0, batch=999.0)  # must NOT invent from these
		with ctx[0], ctx[1], ctx[2], ctx[3], ctx[4], ctx[5], ctx[6], ctx[7]:
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
		with ctx[0], ctx[1], ctx[2], ctx[3], ctx[4], ctx[5], ctx[6], ctx[7]:
			out = classify_zero_row(_base(t_warehouse="Stores - E"))
		self.assertEqual(out["status"], STATUS_MATERIAL_RECEIPT_USER_REVIEW)
		self.assertEqual(out.get("proposed_rate"), 0)
		self.assertFalse(out.get("eligible"))

	def test_c_material_transfer_reject_reconstructs_from_source(self):
		ctx = _patches(transfer=51219.0)
		with ctx[0], ctx[1], ctx[2], ctx[3], ctx[4], ctx[5], ctx[6], ctx[7]:
			out = classify_zero_row(
				_base(
					purpose="Material Transfer",
					s_warehouse="Stores - E",
					t_warehouse="انبار ضایعات اقلام اسپاد",
				)
			)
		self.assertEqual(out["status"], STATUS_RECONSTRUCTABLE)
		self.assertEqual(out["proposed_rate"], 51219.0)
		self.assertEqual(out["source_of_truth"], "source_transfer_sle")
		self.assertTrue(out.get("eligible"))
		self.assertEqual(out.get("confidence"), CONFIDENCE_EXACT)

	def test_d_material_transfer_normal_reconstructs_from_source(self):
		ctx = _patches(transfer=100.0)
		with ctx[0], ctx[1], ctx[2], ctx[3], ctx[4], ctx[5], ctx[6], ctx[7]:
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
		with ctx[0], ctx[1], ctx[2], ctx[3], ctx[4], ctx[5], ctx[6], ctx[7]:
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
		with ctx[0], ctx[1], ctx[2], ctx[3], ctx[4], ctx[5], ctx[6], ctx[7]:
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
		with ctx[0], ctx[1], ctx[2], ctx[3], ctx[4], ctx[5], ctx[6], ctx[7]:
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
		with ctx[0], ctx[1], ctx[2], ctx[3], ctx[4], ctx[5], ctx[6], ctx[7]:
			out = classify_zero_row(_base())
		self.assertEqual(out.get("proposed_rate"), 0)
		self.assertEqual(out["status"], STATUS_MATERIAL_RECEIPT_USER_REVIEW)
		# Document-local version would be allowed — ensure MA alone is rejected.
		self.assertIn("previous_healthy_sle_not_used", out.get("reconstruction_sources") or {})

	def test_material_receipt_version_is_document_local_ok(self):
		ctx = _patches(version=(1500.0, 15000.0), batch=1500.0)
		with ctx[0], ctx[1], ctx[2], ctx[3], ctx[4], ctx[5], ctx[6], ctx[7]:
			out = classify_zero_row(_base())
		self.assertEqual(out["status"], STATUS_RECONSTRUCTABLE)
		self.assertEqual(out["proposed_rate"], 1500.0)
		self.assertTrue(out.get("eligible"))


class TestTransactionSemanticsRegistry(unittest.TestCase):
	def test_registry_covers_site_purposes(self):
		from erpnext_extensions.iran_accounting.historical_stock.transaction_semantics import (
			PURPOSE_REGISTRY,
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


if __name__ == "__main__":
	unittest.main()

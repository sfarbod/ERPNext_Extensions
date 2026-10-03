# Copyright (c) 2026, ERPNext Extensions contributors
"""PENDING_PURCHASE_INVOICE_VALUATION regression tests (A–H)."""

from __future__ import annotations

import sys
import unittest
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt

from erpnext_extensions.iran_accounting.domain.pending_purchase_invoice_valuation import (
	CLASS_PENDING_PURCHASE_INVOICE_VALUATION,
	LEGITIMATE_PENDING_UPSTREAM_ACCOUNTING,
	UNEXPLAINED_NEGATIVE_INCOMING_SVD,
	align_pending_pi_sle_incoming_to_document_zero,
	classify_i3_context,
	classify_pending_purchase_invoice_valuation,
	has_finalized_purchase_valuation,
	honor_zero_inbound_for_pending_pi,
	is_pending_purchase_invoice_valuation_sle,
)
from erpnext_extensions.iran_accounting.domain.riv_valuation_guard import (
	ValuationIntegrityError,
	assert_svd_direction,
)


def _pending_ok(**kwargs):
	base = dict(
		voucher_type="Purchase Receipt",
		voucher_no="PR-TEST-1",
		item_code="ITEM-A",
		voucher_detail_no="PRI-1",
		actual_qty=13,
	)
	base.update(kwargs)
	return base


class TestPendingPurchaseInvoiceValuationV5345(FrappeTestCase):
	"""A–H business contract for pending-PI zero-rate PR."""

	@patch(
		"erpnext_extensions.iran_accounting.domain.pending_purchase_invoice_valuation.finalized_purchase_invoices_for_pr",
		return_value=[],
	)
	@patch(
		"erpnext_extensions.iran_accounting.domain.pending_purchase_invoice_valuation.pr_item_document_rate",
		return_value=0.0,
	)
	@patch("frappe.db.sql", return_value=((0,),))
	@patch(
		"frappe.db.get_value",
		side_effect=lambda *a, **k: (
			frappe._dict(name="PR-TEST-1", docstatus=1, is_return=0, company="C")
			if a and a[0] == "Purchase Receipt"
			else (13 if a and a[0] == "Purchase Receipt Item" else None)
		),
	)
	def test_a_pr_zero_pi_pending_classifies_no_invented_rate(self, *_mocks):
		"""A: PR rate=0 + PI pending → pending classification; no invented rate."""
		res = classify_pending_purchase_invoice_valuation(**_pending_ok())
		self.assertTrue(res["pending"])
		self.assertEqual(res["status"], CLASS_PENDING_PURCHASE_INVOICE_VALUATION)
		self.assertEqual(res["repair_disposition"], LEGITIMATE_PENDING_UPSTREAM_ACCOUNTING)
		self.assertEqual(res["evidence"]["pr_document_rate"], 0.0)
		# Alignment clears stale SLE incoming — never invents a positive rate.
		sle = frappe._dict(
			voucher_type="Purchase Receipt",
			voucher_no="PR-TEST-1",
			item_code="ITEM-A",
			voucher_detail_no="PRI-1",
			actual_qty=13,
			incoming_rate=16458339.0,
		)
		with patch(
			"erpnext_extensions.iran_accounting.domain.pending_purchase_invoice_valuation.is_pending_purchase_invoice_valuation_sle",
			return_value=True,
		):
			self.assertTrue(align_pending_pi_sle_incoming_to_document_zero(sle))
			self.assertEqual(flt(sle.incoming_rate), 0.0)

	@patch(
		"erpnext_extensions.iran_accounting.domain.pending_purchase_invoice_valuation.finalized_purchase_invoices_for_pr",
		return_value=[frappe._dict(name="PINV-1", rate=1000)],
	)
	@patch(
		"erpnext_extensions.iran_accounting.domain.pending_purchase_invoice_valuation.pr_item_document_rate",
		return_value=0.0,
	)
	@patch(
		"frappe.db.get_value",
		side_effect=lambda *a, **k: (
			frappe._dict(name="PR-TEST-1", docstatus=1, is_return=0, company="C")
			if a and a[0] == "Purchase Receipt"
			else 13
		),
	)
	def test_b_finalized_pi_not_pending(self, *_mocks):
		"""B: finalized PI present → NOT pending; PI is valuation authority."""
		res = classify_pending_purchase_invoice_valuation(**_pending_ok())
		self.assertFalse(res["pending"])
		self.assertIn("purchase_invoice_finalized", res["reasons"])
		self.assertTrue(
			has_finalized_purchase_valuation("PR-TEST-1", item_code="ITEM-A", pr_detail="PRI-1")
		)

	@patch(
		"erpnext_extensions.iran_accounting.domain.pending_purchase_invoice_valuation.pr_item_document_rate",
		return_value=22800000.0,
	)
	@patch(
		"frappe.db.get_value",
		return_value=frappe._dict(name="PR-TEST-1", docstatus=1, is_return=0, company="C"),
	)
	def test_c_nonzero_pr_rate_not_automatically_legitimate(self, *_mocks):
		"""C: PR already valued → NOT pending (valuation should already exist on PR)."""
		res = classify_pending_purchase_invoice_valuation(**_pending_ok())
		self.assertFalse(res["pending"])
		self.assertIn("pr_rate_nonzero", res["reasons"])

	def test_d_unexplained_negative_incoming_svd_still_blocks(self):
		"""D: genuine unexplained negative incoming SVD → I3 still raises."""
		sle = frappe._dict(
			actual_qty=13,
			stock_value_difference=-12683322,
			incoming_rate=20686113,
			item_code="X",
			warehouse="W",
		)
		with self.assertRaises(ValuationIntegrityError) as ctx:
			assert_svd_direction(sle, company="C")
		self.assertIn("I3", str(ctx.exception))

	def test_e_pending_classification_does_not_suppress_i3_globally(self):
		"""E: pending class does not make assert_svd_direction permissive."""
		# Even if SLE looks pending-PI shaped, negative SVD must still throw.
		sle = frappe._dict(
			voucher_type="Purchase Receipt",
			voucher_no="PR-TEST-1",
			item_code="ITEM-A",
			voucher_detail_no="PRI-1",
			actual_qty=13,
			stock_value_difference=-1_000_000,
			incoming_rate=0,
		)
		with patch(
			"erpnext_extensions.iran_accounting.domain.pending_purchase_invoice_valuation.is_pending_purchase_invoice_valuation_sle",
			return_value=True,
		):
			self.assertEqual(classify_i3_context(sle=sle), UNEXPLAINED_NEGATIVE_INCOMING_SVD)
			with self.assertRaises(ValuationIntegrityError):
				assert_svd_direction(sle, company="C")

	def test_f_no_sql_skipped_in_pending_module(self):
		"""F: classifier module must not SQL-Skip RIV."""
		from pathlib import Path

		text = Path(
			"/workspace/development/frappe-bench/apps/erpnext_extensions/"
			"erpnext_extensions/iran_accounting/domain/pending_purchase_invoice_valuation.py"
		).read_text(encoding="utf-8")
		self.assertNotIn("SET status='Skipped'", text)
		self.assertNotIn('status="Skipped"', text)

	def test_g_no_continue_on_failed_in_pending_module(self):
		"""G: no continue-on-Failed helper."""
		from pathlib import Path

		text = Path(
			"/workspace/development/frappe-bench/apps/erpnext_extensions/"
			"erpnext_extensions/iran_accounting/domain/pending_purchase_invoice_valuation.py"
		).read_text(encoding="utf-8")
		self.assertNotIn("continue-on-Failed", text)
		self.assertNotIn("continue_on_failed", text)

	def test_h_no_direct_sle_gl_bin_repair_api(self):
		"""H: module aligns in-memory incoming_rate only — no SLE/GL/Bin SQL repair."""
		from pathlib import Path

		text = Path(
			"/workspace/development/frappe-bench/apps/erpnext_extensions/"
			"erpnext_extensions/iran_accounting/domain/pending_purchase_invoice_valuation.py"
		).read_text(encoding="utf-8")
		self.assertNotIn("UPDATE `tabStock Ledger Entry`", text)
		self.assertNotIn("UPDATE `tabGL Entry`", text)
		self.assertNotIn("UPDATE `tabBin`", text)
		self.assertNotIn("db.set_value(\n\t\t\"Stock Ledger Entry\"", text)

	def test_not_every_zero_pr_without_evidence(self):
		"""Do not classify every zero-rate PR — missing submitted PR rejects."""
		with patch("frappe.db.get_value", return_value=None):
			res = classify_pending_purchase_invoice_valuation(**_pending_ok())
			self.assertFalse(res["pending"])
			self.assertIn("pr_missing", res["reasons"])

	@patch(
		"erpnext_extensions.iran_accounting.domain.pending_purchase_invoice_valuation.classify_pending_purchase_invoice_valuation",
		return_value={"pending": True},
	)
	@patch(
		"frappe.db.get_value",
		return_value=frappe._dict(parent="PR-TEST-1", item_code="ITEM-A", qty=13),
	)
	def test_honor_zero_inbound_for_pending_pi(self, *_mocks):
		self.assertTrue(honor_zero_inbound_for_pending_pi("Purchase Receipt", "PRI-1"))
		self.assertFalse(honor_zero_inbound_for_pending_pi("Stock Entry", "SED-1"))

	def test_ma_preserves_leftover_stock_value_no_invented_rate(self):
		"""Zero inbound must preserve warehouse stock_value (incl. leftover); never invent rate."""
		from erpnext_extensions.iran_accounting.domain.pending_purchase_invoice_valuation import (
			apply_pending_pi_moving_average,
			apply_pending_pi_post_vanilla_economics,
		)

		sle = frappe._dict(
			voucher_type="Purchase Receipt",
			voucher_no="PR-TEST-1",
			item_code="ITEM-A",
			voucher_detail_no="PRI-1",
			actual_qty=13,
			incoming_rate=16458339.0,
		)
		engine = MagicMock()
		engine.wh_data = frappe._dict(
			qty_after_transaction=3.0,
			valuation_rate=16458339.0,
			stock_value=82291695.0,  # leftover vs qty*rate
			prev_stock_value=82291695.0,
		)
		with patch(
			"erpnext_extensions.iran_accounting.domain.pending_purchase_invoice_valuation.is_pending_purchase_invoice_valuation_sle",
			return_value=True,
		):
			self.assertTrue(apply_pending_pi_moving_average(engine, sle))
			self.assertEqual(flt(sle.incoming_rate), 0.0)
			# rate diluted from preserved value: 82291695 / 16
			self.assertAlmostEqual(flt(engine.wh_data.valuation_rate), 82291695.0 / 16.0, places=4)
			# Simulate process_sle assignment
			engine.wh_data.qty_after_transaction = 16.0
			engine.wh_data.stock_value = 16.0 * flt(engine.wh_data.valuation_rate)
			svd = engine.wh_data.stock_value - 82291695.0
			self.assertAlmostEqual(svd, 0.0, places=4)

			sle.stock_value = engine.wh_data.stock_value
			sle.stock_value_difference = svd
			sle.qty_after_transaction = 16.0
			sle.valuation_rate = engine.wh_data.valuation_rate
			# Deterministic layer invents incoming from prev balance — must be cleared.
			sle.incoming_rate = 27430565.0
			self.assertTrue(apply_pending_pi_post_vanilla_economics(sle, engine=engine))
			self.assertEqual(flt(sle.incoming_rate), 0.0)
			self.assertEqual(flt(sle.stock_value_difference), 0.0)

	def test_preflight_pending_status_not_poison_blocker(self):
		"""LEGITIMATE_PENDING_UPSTREAM_ACCOUNTING must not block FULL_REPOST_PREFLIGHT."""
		from erpnext_extensions.iran_accounting.historical_stock.next_downtime.preflight import (
			FULL_REPOST_READY,
			full_repost_preflight,
		)

		res = full_repost_preflight(
			{
				"known_riv_poison_roots": [
					{
						"item": "15010444",
						"warehouse": "W",
						"patient_zero": "MAT-PRE-2026-00793-1",
						"status": "LEGITIMATE_PENDING_UPSTREAM_ACCOUNTING",
					}
				]
			}
		)
		self.assertEqual(res["status"], FULL_REPOST_READY)
		self.assertTrue(res["ready"])


def run_suite() -> dict:
	suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
	result = unittest.TextTestRunner(verbosity=2).run(suite)
	return {
		"ok": result.wasSuccessful(),
		"tests": result.testsRun,
		"failures": len(result.failures),
		"errors": len(result.errors),
	}

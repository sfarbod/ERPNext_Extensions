# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.6.1 — evidence-based HRC coverage, scanner contracts, transitional guard."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

import frappe

from erpnext_extensions.iran_accounting.domain.historical_valuation_residual import (
	ACTIVE_HISTORICAL_VALUATION_GUARD,
	STRICT_AFTER_HISTORICAL_REBUILD,
	TRANSITIONAL_NON_BLOCKING,
	is_transitional_non_blocking,
)
from erpnext_extensions.iran_accounting.historical_rebuild import coverage_verify as cv
from erpnext_extensions.iran_accounting.historical_rebuild.integrity_types import (
	CONFIRMED_CORRUPTION,
	REPOST_REQUIRED,
	VERIFIED_CLEAN,
)


class TestTransitionalGuardPreserved(unittest.TestCase):
	def test_transitional_still_active(self):
		self.assertEqual(ACTIVE_HISTORICAL_VALUATION_GUARD, TRANSITIONAL_NON_BLOCKING)
		self.assertTrue(is_transitional_non_blocking())
		self.assertNotEqual(ACTIVE_HISTORICAL_VALUATION_GUARD, STRICT_AFTER_HISTORICAL_REBUILD)

	def test_strict_todo_remains_in_source(self):
		from erpnext_extensions.iran_accounting.domain import historical_valuation_residual as hvr

		src = Path(hvr.__file__).read_text()
		self.assertIn("TODO(HISTORICAL-REBUILD-STRICT-GUARD)", src)
		self.assertIn("TRANSITIONAL_NON_BLOCKING", src)


class TestCoverageVerifyEvidence(unittest.TestCase):
	def test_completed_riv_wrong_item_not_cover(self):
		with mock.patch.object(cv, "has_unresolved_failed_riv", return_value=None), mock.patch.object(
			cv, "find_completed_covering_riv", return_value="RIV-X"
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.coverage_verify.frappe.db.get_value",
			return_value=frappe._dict(
				item_code="OTHER",
				warehouse="WH",
				posting_date="2026-01-01",
				posting_time="00:00:00",
				status="Completed",
				docstatus=1,
			),
		):
			ev = cv.verify_identity_coverage("ITEM", "WH", "2026-02-01", "12:00:00")
		self.assertFalse(ev["ok"])
		self.assertIn("identity_mismatch", ev["reason"])

	def test_completed_riv_wrong_warehouse_not_cover(self):
		with mock.patch.object(cv, "has_unresolved_failed_riv", return_value=None), mock.patch.object(
			cv, "find_completed_covering_riv", return_value="RIV-X"
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.coverage_verify.frappe.db.get_value",
			return_value=frappe._dict(
				item_code="ITEM",
				warehouse="OTHER-WH",
				posting_date="2026-01-01",
				posting_time="00:00:00",
				status="Completed",
				docstatus=1,
			),
		):
			ev = cv.verify_identity_coverage("ITEM", "WH", "2026-02-01", "12:00:00")
		self.assertFalse(ev["ok"])
		self.assertIn("identity_mismatch", ev["reason"])

	def test_completed_riv_dirty_sle_not_cover(self):
		riv = frappe._dict(
			item_code="ITEM",
			warehouse="WH",
			posting_date="2026-01-01",
			posting_time="00:00:00",
			status="Completed",
			docstatus=1,
		)
		dirty_chain = {
			"dirty": True,
			"issue_count": 1,
			"issues": [{"type": "ZERO_QTY_RESIDUAL", "classification": CONFIRMED_CORRUPTION}],
			"sle_count": 2,
		}
		with mock.patch.object(cv, "has_unresolved_failed_riv", return_value=None), mock.patch.object(
			cv, "find_completed_covering_riv", return_value="RIV-OK"
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.coverage_verify.frappe.db.get_value",
			return_value=riv,
		), mock.patch.object(cv, "inspect_sle_chain", return_value=dirty_chain):
			ev = cv.verify_identity_coverage("ITEM", "WH", "2026-02-01", "12:00:00")
		self.assertFalse(ev["ok"])
		self.assertIn("sle_chain_dirty", ev["reason"])
		self.assertEqual(ev["verification_class"], REPOST_REQUIRED)

	def test_completed_riv_open_tva_not_cover(self):
		riv = frappe._dict(
			item_code="ITEM",
			warehouse="WH",
			posting_date="2026-01-01",
			posting_time="00:00:00",
			status="Completed",
			docstatus=1,
		)
		clean = {"dirty": False, "issue_count": 0, "issues": [], "sle_count": 1}
		with mock.patch.object(cv, "has_unresolved_failed_riv", return_value=None), mock.patch.object(
			cv, "find_completed_covering_riv", return_value="RIV-OK"
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.coverage_verify.frappe.db.get_value",
			return_value=riv,
		), mock.patch.object(cv, "inspect_sle_chain", return_value=clean), mock.patch.object(
			cv, "open_tva_for_identity", return_value=[frappe._dict(name="TVA-1")]
		):
			ev = cv.verify_identity_coverage("ITEM", "WH", "2026-02-01", "12:00:00")
		self.assertFalse(ev["ok"])
		self.assertIn("open_tva", ev["reason"])

	def test_completed_riv_failed_dependency_not_cover(self):
		with mock.patch.object(cv, "has_unresolved_failed_riv", return_value="RIV-FAIL"):
			ev = cv.verify_identity_coverage("ITEM", "WH", "2026-02-01", "12:00:00")
		self.assertFalse(ev["ok"])
		self.assertIn("unresolved_failed_riv", ev["reason"])

	def test_correctly_covered_and_verified(self):
		riv = frappe._dict(
			item_code="ITEM",
			warehouse="WH",
			posting_date="2026-01-01",
			posting_time="00:00:00",
			status="Completed",
			docstatus=1,
		)
		clean = {"dirty": False, "issue_count": 0, "issues": [], "sle_count": 3}
		with mock.patch.object(cv, "has_unresolved_failed_riv", return_value=None), mock.patch.object(
			cv, "find_completed_covering_riv", return_value="RIV-OK"
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.coverage_verify.frappe.db.get_value",
			return_value=riv,
		), mock.patch.object(cv, "inspect_sle_chain", return_value=clean), mock.patch.object(
			cv, "open_tva_for_identity", return_value=[]
		):
			ev = cv.verify_identity_coverage("ITEM", "WH", "2026-02-01", "12:00:00")
		self.assertTrue(ev["ok"])
		self.assertEqual(ev["verification_class"], VERIFIED_CLEAN)
		self.assertEqual(ev["covering_riv"], "RIV-OK")

	def test_insufficient_chronological_coverage(self):
		"""No Completed RIV at earlier/equal boundary → not covered."""
		with mock.patch.object(cv, "has_unresolved_failed_riv", return_value=None), mock.patch.object(
			cv, "find_completed_covering_riv", return_value=None
		):
			ev = cv.verify_identity_coverage("ITEM", "WH", "2026-01-01", "10:00:00")
		self.assertFalse(ev["ok"])
		self.assertEqual(ev["reason"], "no_completed_covering_riv")


class TestInspectSleChain(unittest.TestCase):
	def test_zero_qty_residual_detected(self):
		rows = [
			frappe._dict(
				name="SLE1",
				item_code="I",
				warehouse="W",
				company="C",
				voucher_type="Stock Entry",
				voucher_no="SE-1",
				posting_date="2026-01-01",
				posting_time="10:00:00",
				creation="2026-01-01",
				actual_qty=-1,
				qty_after_transaction=0,
				stock_value=1000,
				stock_value_difference=-500,
			)
		]
		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.coverage_verify._identity_sle_chain",
			return_value=rows,
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.coverage_verify.frappe.db.get_value",
			return_value=None,
		):
			out = cv.inspect_sle_chain("I", "W", company="C")
		self.assertTrue(out["dirty"])
		types = {i["type"] for i in out["issues"]}
		self.assertIn("ZERO_QTY_RESIDUAL", types)

	def test_positive_qty_negative_value_detected(self):
		rows = [
			frappe._dict(
				name="SLE1",
				item_code="I",
				warehouse="W",
				company="C",
				voucher_type="Stock Entry",
				voucher_no="SE-1",
				posting_date="2026-01-01",
				posting_time="10:00:00",
				creation="2026-01-01",
				actual_qty=1,
				qty_after_transaction=1,
				stock_value=-500,
				stock_value_difference=-500,
			)
		]
		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.coverage_verify._identity_sle_chain",
			return_value=rows,
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.coverage_verify.frappe.db.get_value",
			return_value=None,
		):
			out = cv.inspect_sle_chain("I", "W", company="C")
		types = {i["type"] for i in out["issues"]}
		self.assertIn("NEGATIVE_STOCK_VALUE", types)

	def test_valuation_continuity_break(self):
		rows = [
			frappe._dict(
				name="SLE1",
				item_code="I",
				warehouse="W",
				company="C",
				voucher_type="Stock Entry",
				voucher_no="SE-1",
				posting_date="2026-01-01",
				posting_time="10:00:00",
				creation="a",
				actual_qty=1,
				qty_after_transaction=1,
				stock_value=100,
				stock_value_difference=100,
			),
			frappe._dict(
				name="SLE2",
				item_code="I",
				warehouse="W",
				company="C",
				voucher_type="Stock Entry",
				voucher_no="SE-2",
				posting_date="2026-01-02",
				posting_time="10:00:00",
				creation="b",
				actual_qty=0,
				qty_after_transaction=1,
				stock_value=50,  # should be 100+0=100
				stock_value_difference=0,
			),
		]
		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.coverage_verify._identity_sle_chain",
			return_value=rows,
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.coverage_verify.frappe.db.get_value",
			return_value=None,
		):
			out = cv.inspect_sle_chain("I", "W", company="C")
		types = {i["type"] for i in out["issues"]}
		self.assertIn("VALUATION_CONTINUITY", types)


class TestFastCoverEvidenceBased(unittest.TestCase):
	def test_bulk_skip_uses_verify_voucher_coverage(self):
		from erpnext_extensions.iran_accounting.historical_rebuild import fast_cover as fc

		src = Path(fc.__file__).read_text()
		self.assertIn("verify_voucher_coverage", src)
		self.assertIn("evidence_based", src)
		self.assertIn("apply_verified_skip", src)

	def test_coverage_audit_uses_evidence(self):
		from erpnext_extensions.iran_accounting.historical_rebuild import coverage_audit as ca

		src = Path(ca.__file__).read_text()
		self.assertIn("verify_voucher_coverage", src)
		self.assertIn("evidence_based", src)


class TestVoucherCoverageMultiIdentity(unittest.TestCase):
	def test_one_dirty_identity_fails_voucher(self):
		sles = [
			frappe._dict(
				item_code="A", warehouse="W1", posting_date="2026-01-01", posting_time="01:00:00", company="C"
			),
			frappe._dict(
				item_code="B", warehouse="W2", posting_date="2026-01-01", posting_time="01:00:00", company="C"
			),
		]
		ok_ev = {
			"ok": True,
			"covering_riv": "R1",
			"verification_class": VERIFIED_CLEAN,
			"reason": "verified:R1",
			"issues": [],
		}
		bad_ev = {
			"ok": False,
			"covering_riv": "R2",
			"verification_class": REPOST_REQUIRED,
			"reason": "sle_chain_dirty:1",
			"issues": [],
		}

		def _side(item_code, *args, **kwargs):
			return ok_ev if item_code == "A" else bad_ev

		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.coverage_verify.verify_voucher_basic",
			return_value={"ok": True, "detail": "sle=2"},
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.coverage_verify.frappe.db.sql",
			return_value=sles,
		), mock.patch.object(cv, "verify_identity_coverage", side_effect=_side):
			out = cv.verify_voucher_coverage("Stock Entry", "SE-1", company="C")
		self.assertFalse(out["ok"])
		self.assertIn("sle_chain_dirty", out["reason"])


class TestIdempotencyAndRestartContracts(unittest.TestCase):
	def test_executor_pre_verifies_before_process(self):
		from erpnext_extensions.iran_accounting.historical_rebuild import executor as ex

		src = Path(ex.__file__).read_text()
		self.assertIn("verify_voucher_coverage", src)
		self.assertIn("process_campaign_item", src)
		self.assertIn("_filter_nonconflicting", src)

	def test_reevaluate_resets_false_skipped(self):
		from erpnext_extensions.iran_accounting.historical_rebuild import reevaluate as re

		src = Path(re.__file__).read_text()
		self.assertIn("apply_repost_required", src)
		self.assertIn("apply_verified_skip", src)
		self.assertIn("Skipped Covered", Path(re.__file__).read_text()[:500] or "re-evaluate")


class TestVersion561(unittest.TestCase):
	def test_app_version(self):
		import erpnext_extensions

		self.assertEqual(erpnext_extensions.__version__, "5.6.1")

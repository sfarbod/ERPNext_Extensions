# Copyright (c) 2026, ERPNext Extensions contributors
"""Unit tests — SLE↔GL drift Historical Repair mode (v5.2.28)."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from frappe.utils import flt

from erpnext_extensions.iran_accounting.historical_stock import (
	CONFLICTING_RIV,
	EXPECTED_GL_ERROR,
	NO_DRIFT,
	READY_GL_ONLY,
	UNBALANCED_EXPECTED_GL,
	WAITING_SLE_REPAIR,
)
from erpnext_extensions.iran_accounting.historical_stock.sle_gl_drift import (
	aggregate_gl_nets,
	classify_sle_gl_drift,
	compare_gl_maps,
	inspect_sle_integrity,
	repair_sle_gl_drift_selected,
	sle_fingerprint,
)
from erpnext_extensions.iran_accounting.domain.irr_gl_precision_align import (
	align_irr_gl_map_to_currency_precision,
)
from erpnext_extensions.iran_accounting.domain.riv_valuation_scope import (
	get_riv_target_item_codes,
	is_sle_in_riv_blocking_scope,
)


def _sle(**kw):
	base = dict(
		name="SLE-1",
		item_code="ITEM-A",
		warehouse="WH-A",
		batch_no=None,
		actual_qty=-10,
		incoming_rate=0,
		outgoing_rate=100,
		valuation_rate=100,
		stock_value=0,
		stock_value_difference=-1000,
		qty_after_transaction=0,
		posting_datetime="2026-01-01 10:00:00",
		voucher_detail_no="row-1",
	)
	base.update(kw)
	return SimpleNamespace(**base)


def _gl(account, debit=0, credit=0, **extra):
	row = {
		"account": account,
		"party_type": "",
		"party": "",
		"against": "",
		"cost_center": "CC",
		"project": "",
		"finance_book": "",
		"account_currency": "IRR",
		"debit": debit,
		"credit": credit,
		"debit_in_account_currency": debit,
		"credit_in_account_currency": credit,
	}
	row.update(extra)
	return row


class TestSLEGLDriftCompare(unittest.TestCase):
	def test_compare_equal_maps(self):
		rows = [_gl("Inv", credit=100), _gl("Exp", debit=100)]
		cmp = compare_gl_maps(rows, list(rows))
		self.assertTrue(cmp["equal"])
		self.assertEqual(cmp["total_abs_net_delta"], 0)

	def test_compare_detects_stale_gl(self):
		expected = [_gl("Inv", credit=254811), _gl("Exp", debit=254811)]
		posted = [_gl("Inv", credit=135000), _gl("Exp", debit=135000)]
		cmp = compare_gl_maps(expected, posted)
		self.assertFalse(cmp["equal"])
		self.assertGreater(cmp["total_abs_net_delta"], 0)
		self.assertIn("Inv", cmp["affected_accounts"])


class TestSLEIntegrityGate(unittest.TestCase):
	@patch("erpnext_extensions.iran_accounting.historical_stock.sle_gl_drift.frappe")
	def test_i1_blocks(self, frappe):
		frappe.db.sql.return_value = [
			_sle(actual_qty=5, incoming_rate=-10, stock_value_difference=-50, valuation_rate=-10, stock_value=100)
		]
		out = inspect_sle_integrity("STE-1")
		self.assertFalse(out["healthy"])
		self.assertEqual(out["blocker"], "I1")

	@patch("erpnext_extensions.iran_accounting.historical_stock.sle_gl_drift.frappe")
	def test_i4_blocks(self, frappe):
		frappe.db.sql.return_value = [
			_sle(actual_qty=-5, qty_after_transaction=0, stock_value=999, stock_value_difference=-100)
		]
		out = inspect_sle_integrity("STE-1")
		self.assertFalse(out["healthy"])
		self.assertEqual(out["blocker"], "I4")

	@patch("erpnext_extensions.iran_accounting.historical_stock.sle_gl_drift.frappe")
	def test_large_irr_rate_not_blocked(self, frappe):
		"""Legitimate large IRR valuation must not be refused merely for magnitude."""
		# 1e11 is large but below established POISON_RATE (1e12)
		frappe.db.sql.return_value = [
			_sle(
				actual_qty=-1,
				valuation_rate=1e11,
				incoming_rate=0,
				stock_value_difference=-1e11,
				stock_value=0,
				qty_after_transaction=0,
			)
		]
		out = inspect_sle_integrity("STE-1")
		self.assertTrue(out["healthy"], out)

	@patch("erpnext_extensions.iran_accounting.historical_stock.sle_gl_drift.frappe")
	def test_non_finite_blocks(self, frappe):
		frappe.db.sql.return_value = [_sle(valuation_rate=float("nan"))]
		out = inspect_sle_integrity("STE-1")
		self.assertFalse(out["healthy"])
		self.assertEqual(out["blocker"], "NON_FINITE")


class TestClassifySLEGLDrift(unittest.TestCase):
	def _se(self, **kw):
		base = dict(
			name="STE-1",
			docstatus=1,
			company="C",
			purpose="Material Transfer for Manufacture",
			posting_date="2026-05-25",
			posting_time="18:02:50",
			creation="2026-05-25 18:02:50",
			modified="2026-05-25 18:02:50",
		)
		base.update(kw)
		return SimpleNamespace(**base)

	@patch("erpnext_extensions.iran_accounting.historical_stock.sle_gl_drift.find_conflicting_riv", return_value=[])
	@patch("erpnext_extensions.iran_accounting.historical_stock.sle_gl_drift.generate_expected_gl")
	@patch("erpnext_extensions.iran_accounting.historical_stock.sle_gl_drift.fetch_posted_gl")
	@patch("erpnext_extensions.iran_accounting.historical_stock.sle_gl_drift.inspect_sle_integrity")
	@patch("erpnext_extensions.iran_accounting.historical_stock.sle_gl_drift.sle_fingerprint", return_value="fp1")
	@patch("erpnext_extensions.iran_accounting.historical_stock.sle_gl_drift.classify_stock_entry_gl")
	@patch("erpnext_extensions.iran_accounting.historical_stock.sle_gl_drift.frappe")
	def test_healthy_stale_gl_ready(
		self, frappe, hist_gl, _fp, integrity, posted, expected, _riv
	):
		frappe.db.get_value.return_value = self._se()
		integrity.return_value = {"healthy": True, "violations": [], "items": ["ITEM-A"], "blocker": None, "reason": None}
		hist_gl.return_value = {"gl_class": "G0_HEALTHY", "eligible": False}
		expected.return_value = {
			"ok": True,
			"rows": [_gl("Inv", credit=254811), _gl("Exp", debit=254811)],
			"balanced": True,
			"postable": True,
			"debit": 254811,
			"credit": 254811,
		}
		posted.return_value = [_gl("Inv", credit=135000), _gl("Exp", debit=135000)]
		out = classify_sle_gl_drift("STE-1")
		self.assertEqual(out["drift_status"], READY_GL_ONLY)
		self.assertTrue(out["eligible"])
		self.assertEqual(out["historical_gl_class"], "G0_HEALTHY")

	@patch("erpnext_extensions.iran_accounting.historical_stock.sle_gl_drift.find_conflicting_riv", return_value=[])
	@patch("erpnext_extensions.iran_accounting.historical_stock.sle_gl_drift.generate_expected_gl")
	@patch("erpnext_extensions.iran_accounting.historical_stock.sle_gl_drift.fetch_posted_gl")
	@patch("erpnext_extensions.iran_accounting.historical_stock.sle_gl_drift.inspect_sle_integrity")
	@patch("erpnext_extensions.iran_accounting.historical_stock.sle_gl_drift.sle_fingerprint", return_value="fp1")
	@patch("erpnext_extensions.iran_accounting.historical_stock.sle_gl_drift.classify_stock_entry_gl")
	@patch("erpnext_extensions.iran_accounting.historical_stock.sle_gl_drift.frappe")
	def test_matching_gl_no_drift(
		self, frappe, hist_gl, _fp, integrity, posted, expected, _riv
	):
		frappe.db.get_value.return_value = self._se()
		integrity.return_value = {"healthy": True, "violations": [], "items": ["ITEM-A"], "blocker": None, "reason": None}
		hist_gl.return_value = {"gl_class": "G0_HEALTHY", "eligible": False}
		rows = [_gl("Inv", credit=100), _gl("Exp", debit=100)]
		expected.return_value = {
			"ok": True,
			"rows": rows,
			"balanced": True,
			"postable": True,
			"debit": 100,
			"credit": 100,
		}
		posted.return_value = list(rows)
		out = classify_sle_gl_drift("STE-1")
		self.assertEqual(out["drift_status"], NO_DRIFT)
		self.assertFalse(out["eligible"])

	@patch("erpnext_extensions.iran_accounting.historical_stock.sle_gl_drift.find_conflicting_riv", return_value=[])
	@patch("erpnext_extensions.iran_accounting.historical_stock.sle_gl_drift.generate_expected_gl")
	@patch("erpnext_extensions.iran_accounting.historical_stock.sle_gl_drift.fetch_posted_gl")
	@patch("erpnext_extensions.iran_accounting.historical_stock.sle_gl_drift.inspect_sle_integrity")
	@patch("erpnext_extensions.iran_accounting.historical_stock.sle_gl_drift.sle_fingerprint", return_value="fp1")
	@patch("erpnext_extensions.iran_accounting.historical_stock.sle_gl_drift.classify_stock_entry_gl")
	@patch("erpnext_extensions.iran_accounting.historical_stock.sle_gl_drift.frappe")
	def test_false_g0_transfer_still_ready(
		self, frappe, hist_gl, _fp, integrity, posted, expected, _riv
	):
		"""MAT-STE-2026-28111 pattern: transferish false G0 must still be READY_GL_ONLY."""
		frappe.db.get_value.return_value = self._se(purpose="Material Transfer for Manufacture")
		integrity.return_value = {"healthy": True, "violations": [], "items": ["13100134"], "blocker": None, "reason": None}
		hist_gl.return_value = {"gl_class": "G0_HEALTHY", "eligible": False}
		expected.return_value = {
			"ok": True,
			"rows": [_gl("WhA", debit=1000), _gl("WhB", credit=1000)],
			"balanced": True,
			"postable": True,
			"debit": 1000,
			"credit": 1000,
		}
		posted.return_value = [_gl("WhA", debit=500), _gl("WhB", credit=500)]
		out = classify_sle_gl_drift("MAT-STE-2026-28111")
		self.assertEqual(out["drift_status"], READY_GL_ONLY)
		self.assertTrue(out["eligible"])

	@patch("erpnext_extensions.iran_accounting.historical_stock.sle_gl_drift.inspect_sle_integrity")
	@patch("erpnext_extensions.iran_accounting.historical_stock.sle_gl_drift.sle_fingerprint", return_value="fp1")
	@patch("erpnext_extensions.iran_accounting.historical_stock.sle_gl_drift.classify_stock_entry_gl")
	@patch("erpnext_extensions.iran_accounting.historical_stock.sle_gl_drift.frappe")
	def test_i1_waiting_sle_repair(self, frappe, hist_gl, _fp, integrity):
		frappe.db.get_value.return_value = self._se()
		hist_gl.return_value = {"gl_class": "G4_BUILT_FROM_POISONED_SLE", "eligible": False}
		integrity.return_value = {
			"healthy": False,
			"violations": [{"blocker": "I1", "reason": "negative_incoming_rate"}],
			"items": ["X"],
			"blocker": "I1",
			"reason": "negative_incoming_rate",
		}
		out = classify_sle_gl_drift("STE-I1")
		self.assertEqual(out["drift_status"], WAITING_SLE_REPAIR)
		self.assertEqual(out["blocker"], "I1")
		self.assertFalse(out["eligible"])

	@patch("erpnext_extensions.iran_accounting.historical_stock.sle_gl_drift.inspect_sle_integrity")
	@patch("erpnext_extensions.iran_accounting.historical_stock.sle_gl_drift.sle_fingerprint", return_value="fp1")
	@patch("erpnext_extensions.iran_accounting.historical_stock.sle_gl_drift.classify_stock_entry_gl")
	@patch("erpnext_extensions.iran_accounting.historical_stock.sle_gl_drift.frappe")
	def test_i4_waiting_sle_repair(self, frappe, hist_gl, _fp, integrity):
		frappe.db.get_value.return_value = self._se()
		hist_gl.return_value = {"gl_class": "G4_BUILT_FROM_POISONED_SLE", "eligible": False}
		integrity.return_value = {
			"healthy": False,
			"violations": [{"blocker": "I4", "reason": "qty_after_zero_nonzero_value"}],
			"items": ["X"],
			"blocker": "I4",
			"reason": "qty_after_zero_nonzero_value",
		}
		out = classify_sle_gl_drift("STE-I4")
		self.assertEqual(out["drift_status"], WAITING_SLE_REPAIR)
		self.assertEqual(out["blocker"], "I4")

	@patch("erpnext_extensions.iran_accounting.historical_stock.sle_gl_drift.find_conflicting_riv", return_value=[])
	@patch("erpnext_extensions.iran_accounting.historical_stock.sle_gl_drift.generate_expected_gl")
	@patch("erpnext_extensions.iran_accounting.historical_stock.sle_gl_drift.inspect_sle_integrity")
	@patch("erpnext_extensions.iran_accounting.historical_stock.sle_gl_drift.sle_fingerprint", return_value="fp1")
	@patch("erpnext_extensions.iran_accounting.historical_stock.sle_gl_drift.classify_stock_entry_gl")
	@patch("erpnext_extensions.iran_accounting.historical_stock.sle_gl_drift.frappe")
	def test_expected_gl_error(self, frappe, hist_gl, _fp, integrity, expected, _riv):
		frappe.db.get_value.return_value = self._se()
		integrity.return_value = {"healthy": True, "violations": [], "items": [], "blocker": None, "reason": None}
		hist_gl.return_value = {"gl_class": "G1_BALANCED_BUT_ECONOMICALLY_WRONG", "eligible": True}
		expected.return_value = {"ok": False, "error": "boom", "rows": [], "balanced": False, "postable": False}
		out = classify_sle_gl_drift("STE-1")
		self.assertEqual(out["drift_status"], EXPECTED_GL_ERROR)
		self.assertFalse(out["eligible"])

	@patch("erpnext_extensions.iran_accounting.historical_stock.sle_gl_drift.find_conflicting_riv", return_value=[])
	@patch("erpnext_extensions.iran_accounting.historical_stock.sle_gl_drift.generate_expected_gl")
	@patch("erpnext_extensions.iran_accounting.historical_stock.sle_gl_drift.inspect_sle_integrity")
	@patch("erpnext_extensions.iran_accounting.historical_stock.sle_gl_drift.sle_fingerprint", return_value="fp1")
	@patch("erpnext_extensions.iran_accounting.historical_stock.sle_gl_drift.classify_stock_entry_gl")
	@patch("erpnext_extensions.iran_accounting.historical_stock.sle_gl_drift.frappe")
	def test_unbalanced_expected(self, frappe, hist_gl, _fp, integrity, expected, _riv):
		frappe.db.get_value.return_value = self._se()
		integrity.return_value = {"healthy": True, "violations": [], "items": [], "blocker": None, "reason": None}
		hist_gl.return_value = {"gl_class": "G1_BALANCED_BUT_ECONOMICALLY_WRONG", "eligible": True}
		expected.return_value = {
			"ok": True,
			"rows": [_gl("A", debit=100)],
			"balanced": False,
			"postable": False,
			"debit": 100,
			"credit": 0,
		}
		out = classify_sle_gl_drift("STE-1")
		self.assertEqual(out["drift_status"], UNBALANCED_EXPECTED_GL)
		self.assertFalse(out["eligible"])

	@patch(
		"erpnext_extensions.iran_accounting.historical_stock.sle_gl_drift.find_conflicting_riv",
		return_value=[SimpleNamespace(name="RIV-ACTIVE")],
	)
	@patch("erpnext_extensions.iran_accounting.historical_stock.sle_gl_drift.inspect_sle_integrity")
	@patch("erpnext_extensions.iran_accounting.historical_stock.sle_gl_drift.sle_fingerprint", return_value="fp1")
	@patch("erpnext_extensions.iran_accounting.historical_stock.sle_gl_drift.classify_stock_entry_gl")
	@patch("erpnext_extensions.iran_accounting.historical_stock.sle_gl_drift.frappe")
	def test_conflicting_riv(self, frappe, hist_gl, _fp, integrity, _riv):
		frappe.db.get_value.return_value = self._se()
		integrity.return_value = {"healthy": True, "violations": [], "items": ["A"], "blocker": None, "reason": None}
		hist_gl.return_value = {"gl_class": "G0_HEALTHY", "eligible": False}
		out = classify_sle_gl_drift("STE-1")
		self.assertEqual(out["drift_status"], CONFLICTING_RIV)
		self.assertFalse(out["eligible"])


class TestRepairExecutionGuards(unittest.TestCase):
	@patch("erpnext_extensions.iran_accounting.historical_stock.sle_gl_drift.finish_run")
	@patch("erpnext_extensions.iran_accounting.historical_stock.sle_gl_drift.start_run", return_value=None)
	@patch("erpnext_extensions.iran_accounting.historical_stock.sle_gl_drift.classify_sle_gl_drift")
	@patch("erpnext_extensions.iran_accounting.historical_stock.sle_gl_drift.frappe")
	def test_dry_run_never_writes(self, frappe, classify, _start, _finish):
		frappe.flags = MagicMock()
		classify.return_value = {
			"voucher": "STE-1",
			"topic": "SLE_GL_DRIFT",
			"repair_class": "SLE_GL_DRIFT",
			"drift_status": READY_GL_ONLY,
			"status": READY_GL_ONLY,
			"eligible": True,
			"posting_date": "2026-01-01",
			"posting_time": "10:00:00",
			"creation": "2026-01-01",
			"expected_debit": 100,
			"expected_credit": 100,
			"posted_debit": 50,
			"posted_credit": 50,
			"delta": 50,
			"affected_accounts": ["Inv"],
			"sle_fingerprint": "abc",
			"items": ["A"],
			"sle_integrity": "HEALTHY",
			"historical_gl_class": "G0_HEALTHY",
			"blocker": None,
			"recommended_action": "rebuild_gl_only_keep_sle",
			"message": "READY",
		}
		out = repair_sle_gl_drift_selected([{"voucher": "STE-1"}], dry_run=True)
		self.assertTrue(out["dry_run"])
		self.assertFalse(out.get("written"))
		frappe.db.commit.assert_not_called()

	@patch("erpnext_extensions.iran_accounting.historical_stock.sle_gl_drift._verify_post_repair")
	@patch("erpnext_extensions.iran_accounting.historical_stock.sle_gl_drift.apply_gl_rebuild_from_current_sle")
	@patch("erpnext_extensions.iran_accounting.historical_stock.snapshot.capture_identity_snapshot")
	@patch("erpnext_extensions.iran_accounting.historical_stock.sle_gl_drift.classify_sle_gl_drift")
	@patch("erpnext_extensions.iran_accounting.historical_stock.sle_gl_drift.frappe")
	def test_successful_repair_keeps_sle_fingerprint(
		self, frappe, classify, snap, rebuild, verify
	):
		from erpnext_extensions.iran_accounting.historical_stock.sle_gl_drift import apply_sle_gl_drift_voucher

		frappe.db.sql.return_value = None
		classify.return_value = {
			"voucher": "STE-1",
			"drift_status": READY_GL_ONLY,
			"eligible": True,
			"sle_fingerprint": "same-fp",
			"message": "READY",
		}
		snap.return_value = {"gl_before": [], "full_rollback_possible": True}
		rebuild.return_value = {"written": True, "blocked": False, "gl_rows": 2}
		verify.return_value = {"ok": True, "sle_fingerprint": "same-fp"}
		out = apply_sle_gl_drift_voucher("STE-1")
		self.assertTrue(out["written"])
		self.assertEqual(out["sle_fingerprint_before"], "same-fp")
		self.assertEqual(out["sle_fingerprint_after"], "same-fp")
		rebuild.assert_called_once()

	@patch("erpnext_extensions.iran_accounting.historical_stock.sle_gl_drift._restore_gl_from_snapshot")
	@patch("erpnext_extensions.iran_accounting.historical_stock.sle_gl_drift._verify_post_repair")
	@patch("erpnext_extensions.iran_accounting.historical_stock.sle_gl_drift.apply_gl_rebuild_from_current_sle")
	@patch("erpnext_extensions.iran_accounting.historical_stock.snapshot.capture_identity_snapshot")
	@patch("erpnext_extensions.iran_accounting.historical_stock.sle_gl_drift.classify_sle_gl_drift")
	@patch("erpnext_extensions.iran_accounting.historical_stock.sle_gl_drift.frappe")
	def test_post_repair_mismatch_fails(
		self, frappe, classify, snap, rebuild, verify, restore
	):
		from erpnext_extensions.iran_accounting.historical_stock.sle_gl_drift import apply_sle_gl_drift_voucher

		frappe.db.sql.return_value = None
		frappe.throw.side_effect = Exception("POST_REPAIR_GL_MISMATCH")
		classify.return_value = {
			"voucher": "STE-1",
			"drift_status": READY_GL_ONLY,
			"eligible": True,
			"sle_fingerprint": "fp",
			"message": "READY",
		}
		snap.return_value = {"gl_before": [_gl("A", debit=1)], "full_rollback_possible": True}
		rebuild.return_value = {"written": True, "blocked": False}
		verify.return_value = {"ok": False, "reason": "POST_REPAIR_GL_MISMATCH — posted GL still differs from expected"}
		with self.assertRaises(Exception):
			apply_sle_gl_drift_voucher("STE-1")
		restore.assert_called()


class TestV527RoundingEligible(unittest.TestCase):
	def test_34391_arithmetic_aligns_and_balances(self):
		"""v5.2.27 1-IRR case remains balanced → GL-only eligible arithmetic."""
		doc = MagicMock()
		doc.doctype = "Stock Entry"
		doc.name = "MAT-STE-2026-34391"
		doc.company = "اسپاد فارمد دارو"
		doc.purpose = "Manufacture"
		doc.posting_date = "2026-08-04"
		doc.get = lambda key, default=None: getattr(doc, key, default)
		gl_map = [
			{
				"account": "111701 - WIP",
				"debit": 0.0,
				"credit": 50_671_372.03,
				"debit_in_account_currency": 0.0,
				"credit_in_account_currency": 50_671_372.03,
				"cost_center": "CC",
			},
			{
				"account": "621301 - Stock Adj",
				"debit": 0.0,
				"credit": 0.48,
				"debit_in_account_currency": 0.0,
				"credit_in_account_currency": 0.48,
				"cost_center": "CC",
			},
			{
				"account": "111605 - Semi FG",
				"debit": 50_671_372.51,
				"credit": 0.0,
				"debit_in_account_currency": 50_671_372.51,
				"credit_in_account_currency": 0.0,
				"cost_center": "CC",
			},
		]
		with patch(
			"erpnext_extensions.iran_accounting.domain.irr_gl_precision_align.is_irr_company",
			return_value=True,
		), patch(
			"erpnext_extensions.iran_accounting.domain.irr_gl_precision_align.get_company_currency",
			return_value="IRR",
		), patch(
			"erpnext_extensions.iran_accounting.domain.irr_gl_precision_align.frappe.get_cached_value",
			side_effect=lambda *a, **k: "621301 - Stock Adj"
			if a and a[-1] == "stock_adjustment_account"
			else "CC",
		):
			align_irr_gl_map_to_currency_precision(doc, gl_map)
		net = sum(flt(e["debit"]) - flt(e["credit"]) for e in gl_map)
		self.assertEqual(flt(net, 6), 0.0)
		# Semantic compare would treat aligned map as balanced expected
		posted_stale = [
			_gl("111605 - Semi FG", debit=50_671_323.0),
			_gl("621301 - Stock Adj", credit=1.0),
			_gl("111701 - WIP", credit=50_671_322.0),
		]
		cmp = compare_gl_maps(gl_map, posted_stale)
		self.assertFalse(cmp["equal"])


class TestRegressionRIVScopeAndI1I4(unittest.TestCase):
	"""Existing v5.2.26 scope + I1/I4 contracts remain importable/unchanged."""

	def test_riv_scope_helpers_intact(self):
		engine = SimpleNamespace(
			repost_doc=SimpleNamespace(name="R", item_code="13100134", warehouse="W", items_to_be_repost=None),
			args=SimpleNamespace(item_code="13100134", warehouse="W", items_to_be_repost=None),
			company="C",
		)
		self.assertEqual(get_riv_target_item_codes(engine), {"13100134"})
		sle_target = SimpleNamespace(item_code="13100134", warehouse="W")
		self.assertTrue(is_sle_in_riv_blocking_scope(engine, sle_target))
		# Out-of-scope item must not be in the declared target set.
		self.assertNotIn("OTHER", get_riv_target_item_codes(engine))

	def test_i1_i4_modules_import(self):
		from erpnext_extensions.iran_accounting.historical_stock import i1_repair, i4_repair

		self.assertTrue(callable(i1_repair.classify_i1_voucher))
		self.assertTrue(callable(i4_repair.classify_i4_row))


class TestPlannerSLEGLDrift(unittest.TestCase):
	def test_planner_ready_gl_only(self):
		from erpnext_extensions.iran_accounting.historical_stock.planner import evaluate_row

		row = {
			"topic": "SLE_GL_DRIFT",
			"repair_class": "SLE_GL_DRIFT",
			"drift_status": READY_GL_ONLY,
			"eligible": True,
			"voucher": "STE-1",
		}
		with patch(
			"erpnext_extensions.iran_accounting.historical_stock.planner._gl_row_count",
			return_value=2,
		):
			d = evaluate_row(row)
		self.assertTrue(d["eligible"])
		self.assertEqual(d["planner_status"], READY_GL_ONLY)

	def test_planner_waiting_sle(self):
		from erpnext_extensions.iran_accounting.historical_stock.planner import evaluate_row

		d = evaluate_row(
			{
				"topic": "SLE_GL_DRIFT",
				"drift_status": WAITING_SLE_REPAIR,
				"blocker": "I1",
				"message": "WAITING_SLE_REPAIR — I1",
				"voucher": "STE-1",
			}
		)
		self.assertFalse(d["eligible"])
		self.assertIn("WAITING", d["planner_status"])


if __name__ == "__main__":
	unittest.main()

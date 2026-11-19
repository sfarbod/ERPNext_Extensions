# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.8.3 — ETO SCR valuation propagation orchestration + fixture patterns.

Patterns (voucher IDs are reference scenarios only; not hardcoded into business logic):
* A — MAT-SCR-2026-00016: service-cost correction then downstream Transfer/Manufacture
* B — MAT-SCR-2026-00011: SABE vs SED/SLE drift on Transfer
* C — MAT-SCR-2026-00023: deleted LCV must not remain an active dependency
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.eto_scr_valuation_propagation import (
	CONFIRM_APPLY,
)
from erpnext_extensions.iran_accounting.eto_scr_valuation_propagation import service as svc


def _scr_meta(**kwargs):
	base = {
		"name": "MAT-SCR-TEST",
		"company": "E",
		"posting_date": "2026-04-20",
		"posting_time": "18:00:00",
		"docstatus": 1,
		"is_return": 0,
		"supplier": "SUP-1",
	}
	base.update(kwargs)
	return frappe._dict(base)


def _fg(**kwargs):
	base = {
		"name": "fg1",
		"item_code": "30600001",
		"qty": 61.0,
		"rate": 1744604.0,
		"amount": 106420844.0,
		"rm_cost_per_qty": 1621817.0,
		"service_cost_per_qty": 122787.0,
		"additional_cost_per_qty": 0.0,
		"landed_cost_voucher_amount": 0.0,
		"batch_no": "BATCH-A",
		"warehouse": "WH-Q",
		"purchase_order": "PO-1",
		"subcontracting_order": "SCO-1",
	}
	base.update(kwargs)
	return frappe._dict(base)


class TestEtoPropagationHelpers(unittest.TestCase):
	def test_approx_rate_tolerance(self):
		self.assertTrue(svc._approx(2216580.0, 2216579.7))
		self.assertFalse(svc._approx(1744604.0, 2216580.0))

	def test_stage_marks_mismatch(self):
		row = svc._stage("Transfer SED", "STE-1", 61, 1744604.0, 106420844.0, 2216580.0)
		self.assertFalse(row["ok"])
		self.assertAlmostEqual(row["difference"], 1744604.0 - 2216580.0)


class TestPatternAServiceCostThenPropagate(unittest.TestCase):
	"""Pattern A — corrected SCR service cost must plan stock IW RIV."""

	@patch.object(svc, "require_privileged_user")
	@patch.object(svc, "_open_riv_conflicts", return_value=[])
	@patch.object(svc, "build_valuation_chain")
	def test_plan_includes_service_cost_and_iw_riv(self, build_chain, _conflicts, _priv):
		build_chain.return_value = {
			"api_version": "eto-scr-valuation-propagation-v1",
			"subcontracting_receipt": "SCR-A",
			"meta": _scr_meta(name="SCR-A"),
			"is_eto_subcontracting_receipt": True,
			"lcv": {"has_active_lcv": False, "deleted_lcv_names": [], "active_links": [], "cancelled_links": [], "deleted": []},
			"finished_goods": [
				{
					"item_code": "30600001",
					"batch_no": "BATCH-A",
					"warehouse": "WH-Q",
					"stages": [
						svc._stage("SCR SLE", "SCR-A", 61, 1744604.0, 106420844.0, 1744604.0),
						svc._stage("Transfer SED", "STE-X", 61, 1744604.0, 106420844.0, 1744604.0),
					],
					"downstream_stock_entries": [
						frappe._dict(
							{
								"name": "STE-X",
								"stock_entry_type": "Material Transfer for Manufacture",
								"t_warehouse": "WH-WIP",
								"posting_date": "2026-04-22",
								"posting_time": "10:00:00",
							}
						)
					],
					"sabe_sed_divergences": [],
				}
			],
			"chain_consistent": True,
			"first_incorrect_transition": None,
			"gl": [],
			"gl_balanced": True,
			"riv_history": [],
		}

		with patch(
			"erpnext_extensions.iran_accounting.subcontracting_receipt_service_cost.service.inspect_subcontracting_receipt",
			return_value={
				"eligible": True,
				"action": "correct",
				"items": [
					{
						"before": {"service_cost_per_qty": 122787.0, "rate": 1744604.0},
						"after": {"service_cost_per_qty": 594762.6229508197, "rate": 2216579.62295082},
					}
				],
				"allocation_checks": [],
			},
		):
			plan = svc.plan_propagation("SCR-A")

		self.assertTrue(plan["eligible"])
		self.assertEqual(plan["action"], "propagate")
		actions = [p["action"] for p in plan["plan"]]
		self.assertIn("correct_service_cost", actions)
		self.assertIn("repost_item_warehouse", actions)
		self.assertTrue(plan["include_service_cost_correction"])


class TestPatternBSabeSedDrift(unittest.TestCase):
	"""Pattern B — Transfer SED diverges from SABE/SCR SLE."""

	@patch.object(svc, "require_privileged_user")
	@patch.object(svc, "_open_riv_conflicts", return_value=[])
	@patch.object(svc, "build_valuation_chain")
	def test_plan_detects_drift_and_schedules_riv(self, build_chain, _conflicts, _priv):
		build_chain.return_value = {
			"api_version": "eto-scr-valuation-propagation-v1",
			"subcontracting_receipt": "SCR-B",
			"meta": _scr_meta(name="SCR-B"),
			"is_eto_subcontracting_receipt": True,
			"lcv": {"has_active_lcv": False, "deleted_lcv_names": ["MAT-LCV-X"], "active_links": [], "cancelled_links": [], "deleted": []},
			"finished_goods": [
				{
					"item_code": "30600001",
					"batch_no": "BATCH-B",
					"warehouse": "WH-Q",
					"stages": [
						svc._stage("SCR SLE", "SCR-B", 1905, 583416.0, 1111407480.0, 583416.0),
						svc._stage("Transfer SED", "STE-Y", 1854, 248127.0, 460027851.0, 583416.0),
					],
					"downstream_stock_entries": [],
					"sabe_sed_divergences": [
						{"stock_entry": "STE-Y", "sed_basic_rate": 248127.0, "sabe_rate": 583416.0}
					],
				}
			],
			"chain_consistent": False,
			"first_incorrect_transition": {
				"stage": "Transfer SED",
				"document": "STE-Y",
				"incoming_rate": 248127.0,
				"expected_rate": 583416.0,
			},
			"gl": [],
			"gl_balanced": True,
			"riv_history": [],
		}

		with patch(
			"erpnext_extensions.iran_accounting.subcontracting_receipt_service_cost.service.inspect_subcontracting_receipt",
			return_value={"eligible": False, "action": "skip_already_correct", "items": [], "allocation_checks": []},
		):
			plan = svc.plan_propagation("SCR-B", include_service_cost_correction=False)

		self.assertTrue(plan["eligible"])
		self.assertEqual(plan["first_incorrect_transition"]["document"], "STE-Y")
		self.assertIn("repost_item_warehouse", [p["action"] for p in plan["plan"]])
		self.assertFalse(plan["include_service_cost_correction"])


class TestPatternCDeletedLcvNotActive(unittest.TestCase):
	"""Pattern C — deleted LCV is not an active dependency; consistent chain is noop."""

	@patch.object(svc, "require_privileged_user")
	@patch.object(svc, "_open_riv_conflicts", return_value=[])
	@patch.object(svc, "build_valuation_chain")
	def test_deleted_lcv_consistent_chain_skips_when_svc_disabled(self, build_chain, _conflicts, _priv):
		build_chain.return_value = {
			"api_version": "eto-scr-valuation-propagation-v1",
			"subcontracting_receipt": "SCR-C",
			"meta": _scr_meta(name="SCR-C"),
			"is_eto_subcontracting_receipt": True,
			"lcv": {
				"has_active_lcv": False,
				"deleted_lcv_names": ["MAT-LCV-2026-00019"],
				"active_links": [],
				"cancelled_links": [],
				"deleted": [{"name": "MAT-LCV-2026-00019"}],
			},
			"finished_goods": [
				{
					"item_code": "30600001",
					"batch_no": "BATCH-C",
					"warehouse": "WH-Q",
					"landed_cost_voucher_amount": 0.0,
					"stages": [
						svc._stage("SCR SLE", "SCR-C", 1922, 607745.0, 1168085890.0, 607745.0),
						svc._stage("Transfer SED", "STE-Z", 1922, 607745.0, 1168085890.0, 607745.0),
						svc._stage("Manufacture consume SED", "STE-M", 1900, 607745.0, 1154715500.0, 607745.0),
					],
					"downstream_stock_entries": [],
					"sabe_sed_divergences": [],
				}
			],
			"chain_consistent": True,
			"first_incorrect_transition": None,
			"gl": [],
			"gl_balanced": True,
			"riv_history": [],
		}

		with patch(
			"erpnext_extensions.iran_accounting.subcontracting_receipt_service_cost.service.inspect_subcontracting_receipt",
			return_value={"eligible": True, "action": "correct", "items": [], "allocation_checks": []},
		):
			plan = svc.plan_propagation("SCR-C", include_service_cost_correction=False)

		self.assertFalse(plan["lcv"]["has_active_lcv"])
		self.assertIn("MAT-LCV-2026-00019", plan["lcv"]["deleted_lcv_names"])
		self.assertEqual(plan["action"], "noop_consistent")
		self.assertFalse(plan["eligible"])
		self.assertIn("service_cost_correction_explicitly_skipped", plan["reasons"])

	@patch.object(svc, "require_privileged_user")
	@patch.object(svc, "build_valuation_chain")
	def test_active_lcv_blocks_propagation(self, build_chain, _priv):
		build_chain.return_value = {
			"api_version": "eto-scr-valuation-propagation-v1",
			"subcontracting_receipt": "SCR-LCV",
			"meta": _scr_meta(name="SCR-LCV"),
			"is_eto_subcontracting_receipt": True,
			"lcv": {"has_active_lcv": True, "deleted_lcv_names": [], "active_links": [{"landed_cost_voucher": "LCV-1"}], "cancelled_links": [], "deleted": []},
			"finished_goods": [{"item_code": "30600001", "warehouse": "WH-Q", "downstream_stock_entries": [], "sabe_sed_divergences": []}],
			"chain_consistent": False,
			"first_incorrect_transition": None,
			"gl": [],
			"gl_balanced": True,
			"riv_history": [],
		}
		plan = svc.plan_propagation("SCR-LCV")
		self.assertEqual(plan["action"], "stop_active_lcv")
		self.assertTrue(plan.get("ambiguous"))
		self.assertEqual(plan["plan"], [])


class TestPropagationGuards(unittest.TestCase):
	@patch.object(svc, "require_privileged_user")
	def test_confirm_token_required(self, _priv):
		with patch.object(
			svc,
			"plan_propagation",
			return_value={"eligible": True, "finished_goods": [{"item_code": "X", "warehouse": "W"}], "meta": _scr_meta(), "include_service_cost_correction": False, "plan": []},
		):
			with self.assertRaises(frappe.ValidationError):
				svc.apply_propagation("SCR-X", dry_run=False, confirm="WRONG", execute_riv=False)

	@patch.object(svc, "require_privileged_user")
	def test_dry_run_does_not_require_confirm(self, _priv):
		with patch.object(
			svc,
			"plan_propagation",
			return_value={"eligible": True, "action": "propagate", "plan": []},
		):
			out = svc.apply_propagation("SCR-X", dry_run=True)
		self.assertTrue(out["dry_run"])
		self.assertFalse(out["applied"])

	@patch.object(svc, "require_privileged_user")
	@patch.object(svc, "_open_riv_conflicts", return_value=[{"name": "RIV-OPEN", "status": "Queued"}])
	@patch.object(svc, "build_valuation_chain")
	def test_open_riv_blocks_eligibility(self, build_chain, _conflicts, _priv):
		build_chain.return_value = {
			"api_version": "eto-scr-valuation-propagation-v1",
			"subcontracting_receipt": "SCR-BLOCK",
			"meta": _scr_meta(name="SCR-BLOCK"),
			"is_eto_subcontracting_receipt": True,
			"lcv": {"has_active_lcv": False, "deleted_lcv_names": [], "active_links": [], "cancelled_links": [], "deleted": []},
			"finished_goods": [
				{
					"item_code": "30600001",
					"warehouse": "WH-Q",
					"downstream_stock_entries": [],
					"sabe_sed_divergences": [{"stock_entry": "STE"}],
					"stages": [svc._stage("Transfer SED", "STE", 1, 1.0, 1.0, 2.0)],
				}
			],
			"chain_consistent": False,
			"first_incorrect_transition": {"document": "STE"},
			"gl": [],
			"gl_balanced": True,
			"riv_history": [],
		}
		with patch(
			"erpnext_extensions.iran_accounting.subcontracting_receipt_service_cost.service.inspect_subcontracting_receipt",
			return_value={"eligible": False, "action": "skip", "items": [], "allocation_checks": []},
		):
			plan = svc.plan_propagation("SCR-BLOCK", include_service_cost_correction=False)
		self.assertEqual(plan["action"], "blocked")
		self.assertIn("open_riv_conflict", plan["reasons"])


class TestNonIranAndNonEto(unittest.TestCase):
	@patch.object(svc, "require_privileged_user")
	@patch.object(svc, "build_valuation_chain")
	def test_non_eto_skipped(self, build_chain, _priv):
		build_chain.return_value = {
			"api_version": "eto-scr-valuation-propagation-v1",
			"subcontracting_receipt": "SCR-OTHER",
			"meta": _scr_meta(name="SCR-OTHER"),
			"is_eto_subcontracting_receipt": False,
			"lcv": {"has_active_lcv": False, "deleted_lcv_names": [], "active_links": [], "cancelled_links": [], "deleted": []},
			"finished_goods": [],
			"chain_consistent": True,
			"first_incorrect_transition": None,
			"gl": [],
			"gl_balanced": True,
			"riv_history": [],
		}
		plan = svc.plan_propagation("SCR-OTHER")
		self.assertEqual(plan["action"], "skip_not_eto")
		self.assertFalse(plan["eligible"])


class TestIdempotentPlan(unittest.TestCase):
	@patch.object(svc, "require_privileged_user")
	@patch.object(svc, "_open_riv_conflicts", return_value=[])
	@patch.object(svc, "build_valuation_chain")
	def test_repeated_plan_identical(self, build_chain, _conflicts, _priv):
		payload = {
			"api_version": "eto-scr-valuation-propagation-v1",
			"subcontracting_receipt": "SCR-IDEM",
			"meta": _scr_meta(name="SCR-IDEM"),
			"is_eto_subcontracting_receipt": True,
			"lcv": {"has_active_lcv": False, "deleted_lcv_names": [], "active_links": [], "cancelled_links": [], "deleted": []},
			"finished_goods": [
				{
					"item_code": "30600001",
					"warehouse": "WH-Q",
					"downstream_stock_entries": [],
					"sabe_sed_divergences": [],
					"stages": [svc._stage("SCR SLE", "SCR-IDEM", 1, 100.0, 100.0, 100.0)],
				}
			],
			"chain_consistent": True,
			"first_incorrect_transition": None,
			"gl": [],
			"gl_balanced": True,
			"riv_history": [],
		}
		build_chain.return_value = payload
		with patch(
			"erpnext_extensions.iran_accounting.subcontracting_receipt_service_cost.service.inspect_subcontracting_receipt",
			return_value={"eligible": False, "action": "skip_already_correct", "items": [], "allocation_checks": []},
		):
			a = svc.plan_propagation("SCR-IDEM", include_service_cost_correction=False)
			b = svc.plan_propagation("SCR-IDEM", include_service_cost_correction=False)
		self.assertEqual(a["action"], b["action"])
		self.assertEqual(a["plan"], b["plan"])


if __name__ == "__main__":
	unittest.main()

# Copyright (c) 2026, ERPNext Extensions contributors
"""WORKER_PREFLIGHT + RIV_SETTLEMENT_BARRIER unit tests (no live Redis required)."""

from __future__ import annotations

import sys
import unittest
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from erpnext_extensions.iran_accounting.historical_stock.worker_preflight import (
	WORKER_PREFLIGHT_BLOCKED,
	WORKER_PREFLIGHT_READY,
	run_worker_preflight,
)
from erpnext_extensions.iran_accounting.historical_stock.riv_settlement import (
	RIV_SETTLEMENT_BLOCKED,
	RIV_SETTLEMENT_COMPLETE,
	RIV_SETTLEMENT_FAILED,
	UNEXPECTED_SKIPPED_RIV,
	CampaignRivRegistry,
	assert_campaign_queue_quiescent,
	forbid_sql_riv_status_mutation,
	wait_for_rivs,
)


class TestWorkerPreflightV5344(FrappeTestCase):
	@patch(
		"erpnext_extensions.iran_accounting.historical_stock.worker_preflight._run_probe",
		return_value={"ok": True, "job_id": "j1"},
	)
	@patch(
		"erpnext_extensions.iran_accounting.historical_stock.worker_preflight.worker_queue_status",
		return_value={
			"queue": "long",
			"available": True,
			"workers_for_queue": 1,
			"workers_total": 1,
			"message": "1 worker(s)",
		},
	)
	@patch(
		"erpnext_extensions.iran_accounting.historical_stock.worker_preflight._redis_reachable",
		return_value={"ok": True, "error": None},
	)
	def test_ready_when_redis_listener_and_probe_ok(self, *_mocks):
		out = run_worker_preflight(queues=("long",), require_probe=True)
		self.assertEqual(out["status"], WORKER_PREFLIGHT_READY)
		self.assertTrue(out["ready"])

	@patch(
		"erpnext_extensions.iran_accounting.historical_stock.worker_preflight.worker_queue_status",
		return_value={
			"queue": "long",
			"available": False,
			"workers_for_queue": 0,
			"workers_total": 0,
			"message": "Background workers are paused or not listening to the long queue.",
		},
	)
	@patch(
		"erpnext_extensions.iran_accounting.historical_stock.worker_preflight._redis_reachable",
		return_value={"ok": True, "error": None},
	)
	def test_blocked_when_workers_unavailable(self, *_mocks):
		out = run_worker_preflight(queues=("long",), require_probe=False)
		self.assertEqual(out["status"], WORKER_PREFLIGHT_BLOCKED)
		self.assertFalse(out["ready"])
		self.assertTrue(any(b.get("code") == "WORKER_UNAVAILABLE" for b in out["blockers"]))

	@patch(
		"erpnext_extensions.iran_accounting.historical_stock.worker_preflight._redis_reachable",
		return_value={"ok": False, "error": "connection refused"},
	)
	def test_blocked_when_redis_down(self, *_mocks):
		out = run_worker_preflight(queues=("long",), require_probe=False)
		self.assertEqual(out["status"], WORKER_PREFLIGHT_BLOCKED)
		self.assertTrue(any(b.get("code") == "REDIS_UNREACHABLE" for b in out["blockers"]))


class TestRivSettlementV5344(FrappeTestCase):
	def test_forbid_sql_mutation_sentinel(self):
		with self.assertRaises(RuntimeError):
			forbid_sql_riv_status_mutation()

	def test_empty_registry_complete(self):
		out = wait_for_rivs([], timeout_s=1)
		self.assertEqual(out["status"], RIV_SETTLEMENT_COMPLETE)
		self.assertTrue(out["ready"])

	@patch(
		"erpnext_extensions.iran_accounting.historical_stock.riv_settlement.run_worker_preflight",
		return_value={"ready": True, "status": WORKER_PREFLIGHT_READY},
	)
	@patch(
		"erpnext_extensions.iran_accounting.historical_stock.riv_settlement._read_riv",
		return_value=frappe._dict(
			status="Completed",
			item_code="X",
			warehouse="W",
			modified="2026-10-02",
			error_log=None,
		),
	)
	def test_completed_settles(self, *_mocks):
		out = wait_for_rivs(["RIV-1"], timeout_s=5, poll_s=0.01)
		self.assertEqual(out["status"], RIV_SETTLEMENT_COMPLETE)
		self.assertTrue(out["ready"])

	@patch(
		"erpnext_extensions.iran_accounting.historical_stock.riv_settlement.run_worker_preflight",
		return_value={"ready": True, "status": WORKER_PREFLIGHT_READY},
	)
	@patch(
		"erpnext_extensions.iran_accounting.historical_stock.riv_settlement._read_riv",
		return_value=frappe._dict(
			status="Failed",
			item_code="X",
			warehouse="W",
			modified="2026-10-02",
			error_log="boom",
		),
	)
	def test_failed_stops(self, *_mocks):
		out = wait_for_rivs(["RIV-1"], timeout_s=5, poll_s=0.01)
		self.assertEqual(out["status"], RIV_SETTLEMENT_FAILED)
		self.assertFalse(out["ready"])

	@patch(
		"erpnext_extensions.iran_accounting.historical_stock.riv_settlement.run_worker_preflight",
		return_value={"ready": True, "status": WORKER_PREFLIGHT_READY},
	)
	@patch(
		"erpnext_extensions.iran_accounting.historical_stock.riv_settlement._read_riv",
		return_value=frappe._dict(
			status="Skipped",
			item_code="X",
			warehouse="W",
			modified="2026-10-02",
			error_log=None,
		),
	)
	def test_skipped_is_blocked_not_settled(self, *_mocks):
		out = wait_for_rivs(["RIV-1"], timeout_s=5, poll_s=0.01)
		self.assertEqual(out["status"], RIV_SETTLEMENT_BLOCKED)
		self.assertEqual(out["code"], UNEXPECTED_SKIPPED_RIV)

	@patch(
		"erpnext_extensions.iran_accounting.historical_stock.riv_settlement.run_worker_preflight",
		return_value={"ready": False, "status": WORKER_PREFLIGHT_BLOCKED, "blockers": []},
	)
	@patch(
		"erpnext_extensions.iran_accounting.historical_stock.riv_settlement._read_riv",
		return_value=frappe._dict(
			status="Queued",
			item_code="X",
			warehouse="W",
			modified="2026-10-02",
			error_log=None,
		),
	)
	def test_queued_without_workers_blocks(self, *_mocks):
		out = wait_for_rivs(["RIV-1"], timeout_s=2, poll_s=0.05, require_workers=True)
		self.assertIn(out["status"], (RIV_SETTLEMENT_BLOCKED, "RIV_SETTLEMENT_TIMEOUT"))
		self.assertFalse(out["ready"])

	def test_registry_tracks_names(self):
		reg = CampaignRivRegistry("op1")
		reg.register("RIV-A", item="1", warehouse="W")
		reg.register("RIV-B", item="2", warehouse="W")
		self.assertEqual(reg.names(), ["RIV-A", "RIV-B"])
		self.assertEqual(reg.to_manifest()["n"], 2)

	@patch("frappe.db.sql", return_value=[])
	def test_quiescence_ok(self, _sql):
		with patch(
			"erpnext_extensions.iran_accounting.historical_stock.riv_settlement._read_riv",
			return_value=frappe._dict(status="Completed"),
		):
			out = assert_campaign_queue_quiescent(["RIV-1"], also_forbid_global_open=True)
			self.assertTrue(out["ready"])
			self.assertEqual(out["status"], "CAMPAIGN_QUEUE_QUIESCENT")


def _no_sql_skipped_in_l6_source():
	from pathlib import Path

	p = Path(
		"/workspace/development/frappe-bench/apps/erpnext_extensions/"
		"erpnext_extensions/iran_accounting/historical_stock/_validation/sept30_l6_full_repost.py"
	)
	text = p.read_text(encoding="utf-8")
	assert "SET status='Skipped'" not in text
	assert "_settle_open_riv" not in text


class TestNoSqlSkippedInCampaignScripts(unittest.TestCase):
	def test_l6_has_no_sql_skipped(self):
		_no_sql_skipped_in_l6_source()

	def test_plan_has_no_sql_skipped(self):
		from pathlib import Path

		p = Path(
			"/workspace/development/frappe-bench/apps/erpnext_extensions/"
			"erpnext_extensions/iran_accounting/historical_stock/_validation/"
			"sept30_deterministic_plan_v1.py"
		)
		text = p.read_text(encoding="utf-8")
		self.assertNotIn("SET status='Skipped'", text)


def run_suite() -> dict:
	"""bench execute entrypoint for this module's tests."""
	import unittest as _ut

	suite = _ut.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
	result = _ut.TextTestRunner(verbosity=2).run(suite)
	return {
		"ok": result.wasSuccessful(),
		"tests": result.testsRun,
		"failures": len(result.failures),
		"errors": len(result.errors),
	}

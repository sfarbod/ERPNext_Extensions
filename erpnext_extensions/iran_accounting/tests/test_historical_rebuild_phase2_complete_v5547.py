# Copyright (c) 2026, ERPNext Extensions contributors
"""Phase-2 complete rebuild regressions: workers, backpressure, cover, recovery."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest import mock

import frappe

from erpnext_extensions.iran_accounting.historical_rebuild.constants import (
	MAX_OUTSTANDING_RIV,
)
from erpnext_extensions.iran_accounting.historical_rebuild.processor import (
	find_covering_riv,
)


class TestWorkerExtraConfig(unittest.TestCase):
	def test_hrc_workers_script_exists_and_targets_four_extras(self):
		# Bench root is three levels above this app package when installed in apps/
		bench_root = Path(frappe.utils.get_bench_path())
		script = bench_root / "scripts" / "hrc_workers.sh"
		self.assertTrue(script.is_file(), f"missing {script}")
		text = script.read_text()
		self.assertIn("hrc_long_4", text)
		self.assertIn("hrc_long_5", text)
		self.assertIn("hrc_sd_1", text)
		self.assertIn("hrc_sd_2", text)
		self.assertIn("short,default", text)
		self.assertIn('cd "$SITES"', text)

	def test_max_outstanding_riv_bounded(self):
		self.assertEqual(MAX_OUTSTANDING_RIV, 8)
		self.assertGreater(MAX_OUTSTANDING_RIV, 0)
		self.assertLessEqual(MAX_OUTSTANDING_RIV, 16)


class TestFindCoveringRivDatetime(unittest.TestCase):
	def test_same_day_later_riv_does_not_cover(self):
		"""RIV starting after voucher time on same date must not cover."""
		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.processor.frappe.db.sql",
			return_value=[],
		) as sql:
			out = find_covering_riv("ITEM", "WH", "2026-01-01", "10:00:00")
			self.assertIsNone(out)
			args = sql.call_args[0]
			# query binds: item, wh, date, date, time
			self.assertEqual(args[1][0], "ITEM")
			self.assertEqual(args[1][2], "2026-01-01")
			self.assertEqual(args[1][4], "10:00:00")
			self.assertIn("posting_time <=", args[0])

	def test_earlier_riv_covers(self):
		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.processor.frappe.db.sql",
			return_value=[("RIV-1",)],
		):
			self.assertEqual(find_covering_riv("ITEM", "WH", "2026-01-02", "00:00:00"), "RIV-1")


class TestIdentitySeedBackpressure(unittest.TestCase):
	def test_seed_respects_outstanding_cap(self):
		from erpnext_extensions.iran_accounting.historical_rebuild import identity_seed as seed

		with mock.patch.object(seed, "_outstanding_count", return_value=MAX_OUTSTANDING_RIV), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.identity_seed.frappe.get_doc"
		) as gd, mock.patch(
			"erpnext_extensions.iran_accounting.integration.bootstrap.apply"
		):
			gd.return_value = mock.Mock(company="C")
			out = seed.seed_identities("HRC-00001", limit=8)
		self.assertEqual(out["seeded"], 0)
		self.assertIn("backpressure", out.get("detail", ""))


class TestPauseStopSelectionOnly(unittest.TestCase):
	def test_pause_does_not_cancel_riv(self):
		"""PAUSE sets selection flags only — never deletes/cancels Core RIV."""
		from erpnext_extensions.iran_accounting.historical_rebuild import campaign as camp

		src = Path(camp.__file__).read_text()
		# pause/stop implementations must not cancel RIV
		self.assertNotIn("cancel(", camp.pause.__code__.co_names)
		self.assertNotIn("delete_doc", camp.pause.__code__.co_names)
		self.assertIn("pause_requested", src)
		self.assertIn("stop_requested", src)


class TestFastCoverRequiresVerification(unittest.TestCase):
	def test_bulk_skip_requires_verify_and_completed_cover(self):
		from erpnext_extensions.iran_accounting.historical_rebuild import fast_cover as fc

		src = Path(fc.__file__).read_text()
		# v5.6.1: evidence-based verify_voucher_coverage (Completed RIV alone insufficient)
		self.assertIn("verify_voucher_coverage", src)
		self.assertIn("apply_verified_skip", src)
		self.assertIn("evidence_based", src)


class TestRecoverUnscheduledScoped(unittest.TestCase):
	def test_recover_uses_execute_not_status_force(self):
		from erpnext_extensions.iran_accounting.historical_rebuild import campaign as camp

		src = Path(camp.__file__).read_text()
		self.assertIn("execute_reposting_entry", src)
		self.assertIn("outstanding_linked_rivs", src)
		# Must not call Redis purge APIs (docstring may mention "purge")
		self.assertNotIn("queue.Empty", src)
		self.assertNotIn(".purge(", src)
		self.assertNotIn("purge_queue", src)
		self.assertNotIn("delete_job", src)


class TestIdentityDrainApi(unittest.TestCase):
	def test_resume_identity_drain_exported(self):
		from erpnext_extensions.iran_accounting.historical_rebuild import campaign as camp

		self.assertTrue(callable(camp.resume_identity_drain))
		self.assertTrue(callable(camp.recover_unscheduled_rivs))

# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.6.1 — no-skip full historical rebuild coverage contracts."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

import frappe

from erpnext_extensions.iran_accounting.domain.historical_valuation_residual import (
	ACTIVE_HISTORICAL_VALUATION_GUARD,
	TRANSITIONAL_NON_BLOCKING,
)
from erpnext_extensions.iran_accounting.historical_rebuild import full_rebuild as fr
from erpnext_extensions.iran_accounting.historical_rebuild.integrity_types import (
	COVERED_BY_REPOSTED_AND_VERIFIED,
	DIRECT_REPOSTED_AND_VERIFIED,
	FINAL_SUCCESS_CLASSES,
	REPOST_PLANNED,
	SESSION_RIV_REMARK_PREFIX,
	VERIFIED_CLEAN,
)


class TestNoSkipPolicy(unittest.TestCase):
	def test_verified_clean_is_not_final_success(self):
		self.assertNotIn(VERIFIED_CLEAN, FINAL_SUCCESS_CLASSES)
		self.assertIn(DIRECT_REPOSTED_AND_VERIFIED, FINAL_SUCCESS_CLASSES)
		self.assertIn(COVERED_BY_REPOSTED_AND_VERIFIED, FINAL_SUCCESS_CLASSES)

	def test_session_remark_prefix(self):
		self.assertTrue(SESSION_RIV_REMARK_PREFIX.startswith("HRC561_SESSION"))

	def test_historical_completed_riv_rejected_without_session(self):
		"""Coverage requires session-stamped RIV, not bare Completed."""
		with mock.patch.object(fr, "get_session", return_value=None):
			self.assertIsNone(
				fr._session_cover_for_identity("ITEM", "WH", "2026-01-01", "00:00:00")
			)

	def test_session_cover_requires_registry_membership(self):
		sess = {
			"session_id": "TEST",
			"remark": f"{SESSION_RIV_REMARK_PREFIX}TEST",
			"riv_completed": ["RIV-SESSION-1"],
			"riv_created": ["RIV-SESSION-1"],
		}
		with mock.patch.object(fr, "get_session", return_value=sess), mock.patch.object(
			fr, "_save_session"
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.full_rebuild.frappe.db.sql",
			return_value=[("RIV-SESSION-1",)],
		) as sql:
			out = fr._session_cover_for_identity("ITEM", "WH", "2026-02-01", "12:00:00")
		self.assertEqual(out, "RIV-SESSION-1")
		# Query must constrain to session RIV names, not arbitrary Completed RIVs
		self.assertIn("RIV-SESSION-1", sql.call_args[0][1])

	def test_full_rebuild_module_exports(self):
		self.assertTrue(callable(fr.run_full_rebuild))
		self.assertTrue(callable(fr.reset_campaign_for_full_repost))
		self.assertTrue(callable(fr.verify_and_mark_covered_batch))
		src = Path(fr.__file__).read_text()
		self.assertIn("SESSION_RIV_REMARK_PREFIX", src)
		self.assertIn("COVERED_BY_REPOSTED_AND_VERIFIED", src)
		self.assertIn("no-skip", src.lower() or "no-skip")

	def test_transitional_guard_preserved(self):
		self.assertEqual(ACTIVE_HISTORICAL_VALUATION_GUARD, TRANSITIONAL_NON_BLOCKING)


class TestMarkCoveredRequiresCleanChain(unittest.TestCase):
	def test_dirty_chain_keeps_pending(self):
		items = [
			frappe._dict(
				name="CI-1",
				voucher_type="Stock Entry",
				voucher_no="SE-1",
				company="C",
				ordinal=1,
			)
		]
		sles = [
			frappe._dict(
				item_code="I",
				warehouse="W",
				posting_date="2026-02-01",
				posting_time="10:00:00",
				company="C",
			)
		]
		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.full_rebuild.frappe.get_all",
			return_value=items,
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.full_rebuild.verify_voucher_basic",
			return_value={"ok": True},
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.full_rebuild.frappe.db.sql",
			return_value=sles,
		), mock.patch.object(
			fr, "_session_cover_for_identity", return_value="RIV-1"
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.full_rebuild.frappe.db.get_value",
			return_value=frappe._dict(posting_date="2026-01-01", posting_time="00:00:00"),
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.full_rebuild.inspect_sle_chain",
			return_value={"dirty": True, "issue_count": 2, "issues": []},
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.full_rebuild.frappe.db.set_value"
		) as sv, mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.full_rebuild.frappe.db.commit"
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.full_rebuild.frappe.get_meta",
			return_value=mock.Mock(has_field=lambda *_: False),
		):
			out = fr.verify_and_mark_covered_batch("HRC-00001", limit=10)
		self.assertEqual(out["covered"], 0)
		self.assertEqual(out["still_pending"], 1)
		for call in sv.call_args_list:
			args = call[0]
			if len(args) >= 3 and isinstance(args[2], dict):
				self.assertNotEqual(
					args[2].get("verification_class"), COVERED_BY_REPOSTED_AND_VERIFIED
				)

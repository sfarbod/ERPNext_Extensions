# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

"""v5.5.53 hardening — core compatibility, permissions, cancel-vs-reverse policy."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import frappe
from frappe.utils import flt

from erpnext_extensions.asset_usage_depreciation import integration_hooks
from erpnext_extensions.asset_usage_depreciation.services import scrap_restore_vad as srv


ASSET = "3455"
SCRAP_DATE = "2026-09-23"
BASELINE_VAD = 108350037.0


class TestScrapRestoreHardeningV5553(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		cls.has_asset = bool(frappe.db.exists("Asset", ASSET))

	def test_01_missing_asset_throws(self):
		with self.assertRaises(frappe.ValidationError):
			srv.restore_asset_accounting("ASSET-DOES-NOT-EXIST-HARDEN")

	def test_02_restore_calls_core_restore_asset(self):
		"""Hardened path must invoke core restore_asset (not a full reimplementation)."""
		core_calls = []

		def fake_core(asset_name):
			core_calls.append(asset_name)
			# Simulate core calling the patched reverse hook
			from erpnext.assets.doctype.asset import depreciation as depr_mod

			asset = MagicMock()
			asset.disposal_date = None
			asset.get = lambda key, default=None: [] if key == "finance_books" else default
			depr_mod.reverse_depreciation_entry_made_on_disposal(asset)

		with (
			patch(
				"erpnext.assets.doctype.asset.depreciation.restore_asset",
				side_effect=fake_core,
			),
			patch.object(srv, "sync_asset_vad_from_finance_book", return_value={"changed": False}),
			patch("frappe.db.exists", return_value=True),
		):
			report = srv.restore_asset_accounting("FAKE-ASSET")

		self.assertEqual(core_calls, ["FAKE-ASSET"])
		self.assertTrue(report["core_restore_called"])

	def test_03_permission_denied_on_core_propagates(self):
		def deny(asset_name):
			frappe.throw("Not permitted", frappe.PermissionError)

		with (
			patch(
				"erpnext.assets.doctype.asset.depreciation.restore_asset",
				side_effect=deny,
			),
			patch("frappe.db.exists", return_value=True),
		):
			with self.assertRaises(frappe.PermissionError):
				srv.restore_asset_accounting("FAKE-ASSET")

	def test_04_period_lock_falls_back_to_reverse(self):
		disposal = frappe.utils.getdate("2026-09-23")

		class _Row:
			finance_book = None

		class _Asset:
			name = "A1"
			disposal_date = disposal

			def get(self, key, default=None):
				if key == "finance_books":
					return [_Row()]
				return default

		class _Schedule:
			schedule_date = disposal
			journal_entry = "JE-SCRAP-DAY"
			name = "SCH-1"

		class _ScheduleDoc:
			def get(self, key, default=None):
				if key == "depreciation_schedule":
					return [_Schedule()]
				return default

		je = MagicMock()
		je.docstatus = 1
		je.voucher_type = "Depreciation Entry"
		je.reversal_of = None
		je.name = "JE-SCRAP-DAY"
		je.cancel.side_effect = frappe.ValidationError("Books have been closed for this period")

		reverse_je = MagicMock()
		reverse_je.name = "JE-REVERSE"

		with (
			patch.object(srv, "get_asset_depr_schedule_doc", return_value=_ScheduleDoc()),
			patch.object(srv, "_should_unwind_schedule_row", return_value=True),
			patch("frappe.get_doc", return_value=je),
			patch.object(srv, "create_reverse_depreciation_entry", return_value=reverse_je) as rev,
			patch.object(srv, "update_value_after_depreciation_on_asset_restore") as upd,
			patch("frappe.msgprint"),
		):
			actions = srv.cancel_or_reverse_disposal_day_depreciation(_Asset())

		self.assertEqual(len(actions), 1)
		self.assertEqual(actions[0]["mode"], "reverse_fallback")
		self.assertEqual(actions[0]["reverse_journal_entry"], "JE-REVERSE")
		rev.assert_called_once()
		upd.assert_called_once()

	def test_05_permission_error_on_cancel_does_not_fallback(self):
		disposal = frappe.utils.getdate("2026-09-23")

		class _Row:
			finance_book = None

		class _Asset:
			name = "A1"
			disposal_date = disposal

			def get(self, key, default=None):
				if key == "finance_books":
					return [_Row()]
				return default

		class _Schedule:
			schedule_date = disposal
			journal_entry = "JE-SCRAP-DAY"
			name = "SCH-1"

		class _ScheduleDoc:
			def get(self, key, default=None):
				if key == "depreciation_schedule":
					return [_Schedule()]
				return default

		je = MagicMock()
		je.docstatus = 1
		je.voucher_type = "Depreciation Entry"
		je.reversal_of = None
		je.cancel.side_effect = frappe.PermissionError("No cancel permission")

		with (
			patch.object(srv, "get_asset_depr_schedule_doc", return_value=_ScheduleDoc()),
			patch.object(srv, "_should_unwind_schedule_row", return_value=True),
			patch("frappe.get_doc", return_value=je),
			patch.object(srv, "create_reverse_depreciation_entry") as rev,
		):
			with self.assertRaises(frappe.PermissionError):
				srv.cancel_or_reverse_disposal_day_depreciation(_Asset())
		rev.assert_not_called()

	def test_06_is_period_lock_detector(self):
		self.assertTrue(srv._is_period_or_books_lock_error(Exception("Period Closing Voucher exists")))
		self.assertTrue(srv._is_period_or_books_lock_error(Exception("Account is frozen")))
		self.assertFalse(srv._is_period_or_books_lock_error(frappe.PermissionError("no")))
		self.assertFalse(srv._is_period_or_books_lock_error(Exception("random failure")))

	def test_07_sync_uses_default_finance_book_idx(self):
		if not self.has_asset:
			self.skipTest(f"Asset {ASSET} not on site")
		asset = frappe.get_doc("Asset", ASSET)
		row = srv._default_finance_book_row(asset)
		self.assertIsNotNone(row)
		# Force divergence then sync
		frappe.db.set_value(
			"Asset", ASSET, "value_after_depreciation", BASELINE_VAD - 1000, update_modified=False
		)
		report = srv.sync_asset_vad_from_finance_book(ASSET)
		self.assertTrue(report["changed"])
		self.assertAlmostEqual(flt(report["asset_vad_after"]), flt(report["fb_vad"]), places=2)
		self.assertIn("finance_book_row", report)

	def test_08_guest_cannot_restore(self):
		if not self.has_asset:
			self.skipTest(f"Asset {ASSET} not on site")
		if frappe.db.get_value("Asset", ASSET, "status") == "Scrapped":
			frappe.set_user("Administrator")
			integration_hooks.restore_asset(ASSET)
			frappe.db.commit()

		frappe.set_user("Guest")
		try:
			with self.assertRaises((frappe.PermissionError, frappe.ValidationError, Exception)):
				integration_hooks.restore_asset(ASSET)
		finally:
			frappe.set_user("Administrator")

	def test_09_three_cycles_still_valuation_and_je_clean(self):
		if not self.has_asset:
			self.skipTest(f"Asset {ASSET} not on site")
		frappe.set_user("Administrator")
		if frappe.db.get_value("Asset", ASSET, "status") == "Scrapped":
			integration_hooks.restore_asset(ASSET)
			frappe.db.commit()
		# Align baseline
		srv.sync_asset_vad_from_finance_book(ASSET)
		fb = frappe.db.get_value("Asset Finance Book", {"parent": ASSET}, "value_after_depreciation")
		frappe.db.set_value("Asset", ASSET, "value_after_depreciation", fb, update_modified=False)
		frappe.db.commit()
		baseline = flt(frappe.db.get_value("Asset", ASSET, "value_after_depreciation"))

		legit = {
			"ACC-JV-1405-166537",
			"ACC-JV-1405-166538",
			"ACC-JV-1405-166539",
			"ACC-JV-1405-166540",
			"ACC-JV-1405-166541",
			"ACC-JV-1405-166542",
		}
		for i in range(3):
			integration_hooks.scrap_asset(ASSET, SCRAP_DATE)
			frappe.db.commit()
			report = integration_hooks.restore_asset(ASSET)
			frappe.db.commit()
			self.assertTrue(report.get("core_restore_called"))
			self.assertTrue(report.get("cancelled_scrap_day_jes") or report.get("reverse_fallback_jes"))
			a = flt(frappe.db.get_value("Asset", ASSET, "value_after_depreciation"))
			self.assertAlmostEqual(a, baseline, places=2, msg=f"cycle {i+1}")
			# No submitted non-legit scrap-cycle JEs when cancel mode used
			if report.get("cancelled_scrap_day_jes") and not report.get("reverse_fallback_jes"):
				rows = frappe.db.sql(
					"""
					select distinct je.name from `tabJournal Entry` je
					inner join `tabJournal Entry Account` jea on jea.parent=je.name
					where jea.reference_type='Asset' and jea.reference_name=%s and je.docstatus=1
					""",
					ASSET,
				)
				names = {r[0] for r in rows}
				self.assertEqual(names - legit, set(), f"leftover scrap-cycle JEs: {names - legit}")

		for name in legit:
			if frappe.db.exists("Journal Entry", name):
				self.assertEqual(frappe.db.get_value("Journal Entry", name, "docstatus"), 1)

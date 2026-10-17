# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

"""v5.5.53 — repeated Asset Scrap/Restore must not leak Asset-level VAD.

Uses site Asset 3455 when present (Espad development fixture). Avoids importing
ERPNext ``test_asset.create_asset`` which pulls heavy fixtures that can fail on
restored production clones.
"""

from __future__ import annotations

import unittest

import frappe
from frappe.utils import flt

from erpnext_extensions.asset_usage_depreciation import integration_hooks
from erpnext_extensions.asset_usage_depreciation.services.scrap_restore_vad import (
	assert_asset_fb_vad_parity,
	sync_asset_vad_from_finance_book,
)

ASSET = "3455"
SCRAP_DATE = "2026-09-23"
BASELINE_VAD = 108350037.0


def _vad_pair(asset_name: str) -> tuple[float, float]:
	asset = frappe.get_doc("Asset", asset_name)
	return flt(asset.value_after_depreciation), flt(asset.finance_books[0].value_after_depreciation)


class TestScrapRestoreVadV5553(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		cls.has_asset = bool(frappe.db.exists("Asset", ASSET))

	def _ready(self):
		if not self.has_asset:
			self.skipTest(f"Asset {ASSET} not on site")
		asset = frappe.get_doc("Asset", ASSET)
		if asset.status == "Scrapped":
			integration_hooks.restore_asset(ASSET)
			frappe.db.commit()
		# Ensure known baseline for cycle tests
		a, fb = _vad_pair(ASSET)
		if abs(a - BASELINE_VAD) > 1 or abs(fb - BASELINE_VAD) > 1:
			frappe.db.set_value(
				"Asset",
				ASSET,
				"value_after_depreciation",
				BASELINE_VAD,
				update_modified=False,
			)
			fb_name = frappe.db.get_value("Asset Finance Book", {"parent": ASSET}, "name")
			frappe.db.set_value(
				"Asset Finance Book",
				fb_name,
				"value_after_depreciation",
				BASELINE_VAD,
				update_modified=False,
			)
			frappe.db.commit()

	def test_01_sync_helper_repairs_forced_divergence(self):
		self._ready()
		asset = frappe.get_doc("Asset", ASSET)
		fb_vad = flt(asset.finance_books[0].value_after_depreciation)
		frappe.db.set_value(
			"Asset",
			ASSET,
			"value_after_depreciation",
			fb_vad - 129605,
			update_modified=False,
		)
		self.assertFalse(assert_asset_fb_vad_parity(ASSET)["in_sync"])
		report = sync_asset_vad_from_finance_book(ASSET)
		self.assertTrue(report["changed"])
		self.assertTrue(assert_asset_fb_vad_parity(ASSET)["in_sync"])
		self.assertAlmostEqual(_vad_pair(ASSET)[0], fb_vad, places=2)

	def test_02_first_scrap_restore_preserves_baseline(self):
		self._ready()
		before_a, before_fb = _vad_pair(ASSET)
		integration_hooks.scrap_asset(ASSET, SCRAP_DATE)
		frappe.db.commit()
		asset = frappe.get_doc("Asset", ASSET)
		self.assertEqual(asset.status, "Scrapped")
		self.assertTrue(assert_asset_fb_vad_parity(ASSET, context="after_scrap")["in_sync"])

		integration_hooks.restore_asset(ASSET)
		frappe.db.commit()
		after_a, after_fb = _vad_pair(ASSET)
		self.assertAlmostEqual(after_a, after_fb, places=2)
		self.assertAlmostEqual(after_a, before_a, places=2)
		self.assertFalse(frappe.db.get_value("Asset", ASSET, "journal_entry_for_scrap"))

	def test_03_three_cycles_valuation_neutral(self):
		self._ready()
		baseline_a, baseline_fb = _vad_pair(ASSET)
		scrap_vads = []
		for i in range(3):
			integration_hooks.scrap_asset(ASSET, SCRAP_DATE)
			frappe.db.commit()
			sa, sf = _vad_pair(ASSET)
			scrap_vads.append(sa)
			self.assertTrue(assert_asset_fb_vad_parity(ASSET)["in_sync"], f"scrap cycle {i+1}")
			integration_hooks.restore_asset(ASSET)
			frappe.db.commit()
			ra, rf = _vad_pair(ASSET)
			self.assertAlmostEqual(ra, rf, places=2, msg=f"restore parity cycle {i+1}")
			self.assertAlmostEqual(ra, baseline_a, places=2, msg=f"restore asset VAD cycle {i+1}")
			self.assertAlmostEqual(rf, baseline_fb, places=2, msg=f"restore FB VAD cycle {i+1}")
		self.assertAlmostEqual(scrap_vads[0], scrap_vads[1], places=2)
		self.assertAlmostEqual(scrap_vads[1], scrap_vads[2], places=2)

	def test_04_scrap_day_amount_stable(self):
		self._ready()
		amounts = []
		for _ in range(3):
			integration_hooks.scrap_asset(ASSET, SCRAP_DATE)
			frappe.db.commit()
			ads = frappe.db.get_value(
				"Asset Depreciation Schedule", {"asset": ASSET, "docstatus": 1}, "name"
			)
			amt = frappe.db.sql(
				"""
				select depreciation_amount from `tabDepreciation Schedule`
				where parent=%s and schedule_date=%s order by idx desc limit 1
				""",
				(ads, SCRAP_DATE),
			)[0][0]
			amounts.append(flt(amt))
			integration_hooks.restore_asset(ASSET)
			frappe.db.commit()
		self.assertAlmostEqual(amounts[0], amounts[1], places=2)
		self.assertAlmostEqual(amounts[1], amounts[2], places=2)

	def test_05_invalid_scrap_while_scrapped(self):
		self._ready()
		integration_hooks.scrap_asset(ASSET, SCRAP_DATE)
		frappe.db.commit()
		with self.assertRaises(frappe.ValidationError):
			integration_hooks.scrap_asset(ASSET, SCRAP_DATE)
		integration_hooks.restore_asset(ASSET)
		frappe.db.commit()

	def test_06_repeated_restore_while_active_is_vad_neutral(self):
		"""Core restore is idempotent when already active; VAD must not drift."""
		self._ready()
		integration_hooks.scrap_asset(ASSET, SCRAP_DATE)
		frappe.db.commit()
		integration_hooks.restore_asset(ASSET)
		frappe.db.commit()
		before_a, before_fb = _vad_pair(ASSET)
		integration_hooks.restore_asset(ASSET)
		frappe.db.commit()
		after_a, after_fb = _vad_pair(ASSET)
		self.assertAlmostEqual(after_a, before_a, places=2)
		self.assertAlmostEqual(after_fb, before_fb, places=2)
		self.assertNotEqual(frappe.db.get_value("Asset", ASSET, "status"), "Scrapped")

	def test_07_sync_skips_missing_asset(self):
		report = sync_asset_vad_from_finance_book("ASSET-DOES-NOT-EXIST-V5553")
		self.assertEqual(report.get("skipped"), "missing_asset")

# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""v5.7.40 — AVA life-final balancing after revaluation (asset 3997 class).

Root cause fixed: ``authoritative_depreciable_total`` must follow Finance Book
``value_after_depreciation − salvage`` (ERPNext schedule base), not locked
``net_purchase_amount``. Purchase/available date equal to revaluation date is
**incidental**, not causal.

Run::

    bench --site development.localhost run-tests \\
        --module erpnext_extensions.asset_usage_depreciation.tests.test_ava_life_final_reval_v5740 \\
        --skip-before-tests
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

import frappe
from frappe.utils import flt, random_string

from erpnext.assets.doctype.asset_depreciation_schedule.asset_depreciation_schedule import (
	get_asset_depr_schedule_doc,
)

from erpnext_extensions.asset_usage_depreciation.services.accounting_amounts import to_depr_amount
from erpnext_extensions.asset_usage_depreciation.services.ads_amount_normalize import (
	authoritative_depreciable_total,
	is_whole_irr_amount,
	normalize_ads_document,
	normalize_full_schedule_amounts,
)
from erpnext_extensions.asset_usage_depreciation.tests.replan_fixtures import ensure_replan_fixtures
from erpnext_extensions.asset_usage_depreciation.tests.test_asset_usage_replan import _make_sl_asset


def _fx() -> dict:
	return ensure_replan_fixtures()


def _unique(prefix: str) -> str:
	return f"{prefix}-{random_string(6)}"


def _ensure_diff_account(company: str) -> str:
	name_key = "_AUD AVA Reval Surplus 5741"
	existing = frappe.db.get_value("Account", {"account_name": name_key, "company": company}, "name")
	if existing:
		return existing
	parent = frappe.db.get_value(
		"Account", {"company": company, "root_type": "Equity", "is_group": 1}, "name"
	) or frappe.db.get_value(
		"Account", {"company": company, "root_type": "Income", "is_group": 1}, "name"
	)
	doc = frappe.get_doc(
		{
			"doctype": "Account",
			"account_name": name_key,
			"parent_account": parent,
			"company": company,
			"is_group": 0,
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


def _make_ava(asset, new_value: float, date: str):
	fx = _fx()
	asset.reload()
	doc = frappe.get_doc(
		{
			"doctype": "Asset Value Adjustment",
			"company": asset.company,
			"asset": asset.name,
			"date": date,
			"current_asset_value": flt(asset.value_after_depreciation),
			"new_asset_value": new_value,
			"cost_center": fx["cost_center"],
			"difference_account": _ensure_diff_account(asset.company),
			"finance_book": (asset.finance_books[0].finance_book if asset.finance_books else None),
		}
	)
	doc.insert(ignore_permissions=True)
	return doc


def _je_balanced(je_name: str) -> bool:
	debit, credit = frappe.db.sql(
		"""
		select coalesce(sum(debit),0), coalesce(sum(credit),0)
		from `tabGL Entry`
		where voucher_type='Journal Entry' and voucher_no=%s and is_cancelled=0
		""",
		je_name,
	)[0]
	return abs(flt(debit) - flt(credit)) < 0.01


def _mock_asset_fb(*, net_purchase: float, vad: float, opening: float = 0.0, salvage: float = 0.0):
	asset = MagicMock()
	asset.net_purchase_amount = net_purchase
	asset.opening_accumulated_depreciation = opening
	fb = MagicMock()
	fb.value_after_depreciation = vad
	fb.expected_value_after_useful_life = salvage
	return asset, fb


class TestAuthoritativeTotalAndNormalizeUnit(unittest.TestCase):
	"""Pure unit coverage for the economics bug (no DB)."""

	def test_auth_total_tracks_vad_not_purchase_after_upward_reval(self):
		# 3997-shaped numbers (scaled identity)
		net = 4_250_664_632_505.0
		vad_after = 4_545_567_146_822.363
		asset, fb = _mock_asset_fb(net_purchase=net, vad=vad_after, opening=0.0)
		self.assertEqual(authoritative_depreciable_total(asset, fb), to_depr_amount(vad_after))
		self.assertNotEqual(authoritative_depreciable_total(asset, fb), to_depr_amount(net))

	def test_auth_total_equals_purchase_identity_at_inception(self):
		net = 100_000_000.0
		opening = 10_000_000.0
		vad = net - opening
		asset, fb = _mock_asset_fb(net_purchase=net, vad=vad, opening=opening, salvage=1_000_000.0)
		self.assertEqual(
			authoritative_depreciable_total(asset, fb),
			to_depr_amount(vad - 1_000_000.0),
		)
		self.assertEqual(
			authoritative_depreciable_total(asset, fb),
			to_depr_amount(net - opening - 1_000_000.0),
		)

	def test_a_acquisition_equals_reval_date_unit_upward(self):
		"""Test A (unit): same-day acquisition/reval is fine when total follows VAD."""
		# purchase=reval=2026-07-23 is a date fact; economics are VAD-based.
		net = 4_250_664_632_505.0
		target = 4_545_567_146_822.363
		# Early rows large enough that N-1 rounded sum exceeds purchase-based total
		# (mirrors production 300-row overshoot), while still summing near target.
		rows = [
			{"schedule_date": f"d{i}", "depreciation_amount": 400_000_000_000.0}
			for i in range(11)
		] + [{"schedule_date": "d11", "depreciation_amount": target - 11 * 400_000_000_000.0}]
		with self.assertRaises(Exception) as ctx:
			normalize_full_schedule_amounts(
				[dict(r) for r in rows],
				depreciable_total=to_depr_amount(net),
			)
		self.assertIn("negative", str(ctx.exception).lower())
		out = normalize_full_schedule_amounts(
			[dict(r) for r in rows],
			depreciable_total=to_depr_amount(target),
		)
		self.assertEqual(sum(r["depreciation_amount"] for r in out), to_depr_amount(target))
		self.assertTrue(all(is_whole_irr_amount(r["depreciation_amount"]) for r in out))
		self.assertGreaterEqual(out[-1]["depreciation_amount"], 0)

	def test_g_upward_and_h_downward_unit(self):
		for vad in (150_000_000.0, 40_000_000.0):
			rows = [
				{"schedule_date": "a", "depreciation_amount": vad / 4 + 0.4},
				{"schedule_date": "b", "depreciation_amount": vad / 4 + 0.4},
				{"schedule_date": "c", "depreciation_amount": vad / 4 + 0.4},
				{"schedule_date": "d", "depreciation_amount": vad / 4},
			]
			out = normalize_full_schedule_amounts(
				rows, depreciable_total=to_depr_amount(vad)
			)
			self.assertEqual(sum(r["depreciation_amount"] for r in out), to_depr_amount(vad))
			self.assertGreaterEqual(out[-1]["depreciation_amount"], 0)


class TestAvaLifeFinalIntegrationV5740(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		ensure_replan_fixtures()
		_ensure_diff_account(_fx()["company"])

	def test_b_acquisition_earlier_than_reval_upward(self):
		asset = _make_sl_asset(
			asset_name=_unique("AUD-3997B"),
			purchase_date="2026-01-01",
			available_for_use_date="2026-01-01",
			depreciation_start_date="2026-01-31",
			net_purchase_amount=120_000,
			purchase_amount=120_000,
			total_number_of_depreciations=12,
			frequency_of_depreciation=1,
		)
		before_ads = get_asset_depr_schedule_doc(asset.name, "Active")
		ava = _make_ava(asset, new_value=200_000, date="2026-07-23")
		ava.submit()
		asset.reload()
		self.assertAlmostEqual(flt(asset.value_after_depreciation), 200_000, places=2)
		after_ads = get_asset_depr_schedule_doc(asset.name, "Active")
		self.assertIsNotNone(after_ads)
		self.assertNotEqual(before_ads.name, after_ads.name)
		self.assertTrue(ava.journal_entry)
		self.assertTrue(_je_balanced(ava.journal_entry))
		amounts = [flt(r.depreciation_amount) for r in after_ads.depreciation_schedule]
		self.assertTrue(all(a >= 0 for a in amounts))
		self.assertAlmostEqual(sum(amounts), 200_000, places=0)

	def test_c_reval_date_equals_first_depreciation_date(self):
		asset = _make_sl_asset(
			asset_name=_unique("AUD-3997C"),
			purchase_date="2026-01-01",
			available_for_use_date="2026-01-01",
			depreciation_start_date="2026-07-23",
			net_purchase_amount=96_000,
			purchase_amount=96_000,
			total_number_of_depreciations=12,
			frequency_of_depreciation=1,
		)
		ava = _make_ava(asset, new_value=120_000, date="2026-07-23")
		ava.submit()
		ads = get_asset_depr_schedule_doc(asset.name, "Active")
		self.assertTrue(all(flt(r.depreciation_amount) >= 0 for r in ads.depreciation_schedule))
		asset.reload()
		self.assertAlmostEqual(flt(asset.value_after_depreciation), 120_000, places=2)

	def test_e_one_remaining_period_after_reval(self):
		asset = _make_sl_asset(
			asset_name=_unique("AUD-3997E"),
			purchase_date="2026-01-01",
			available_for_use_date="2026-01-01",
			depreciation_start_date="2026-01-31",
			net_purchase_amount=50_000,
			purchase_amount=50_000,
			total_number_of_depreciations=1,
			frequency_of_depreciation=1,
			expected_value_after_useful_life=0,
		)
		ava = _make_ava(asset, new_value=80_000, date="2026-01-15")
		ava.submit()
		ads = get_asset_depr_schedule_doc(asset.name, "Active")
		self.assertEqual(len(ads.depreciation_schedule), 1)
		self.assertEqual(to_depr_amount(ads.depreciation_schedule[0].depreciation_amount), 80_000)
		self.assertGreaterEqual(flt(ads.depreciation_schedule[0].depreciation_amount), 0)

	def test_f_final_installment_rounding_whole(self):
		raw = [10.6, 10.6, 10.6, 10.2]
		rows = [{"schedule_date": f"d{i}", "depreciation_amount": a} for i, a in enumerate(raw)]
		normalize_full_schedule_amounts(rows, depreciable_total=42)
		self.assertEqual(rows[-1]["depreciation_amount"], 9)
		self.assertTrue(is_whole_irr_amount(rows[-1]["depreciation_amount"]))
		self.assertEqual(sum(r["depreciation_amount"] for r in rows), 42)

	def test_g_upward_revaluation_integration(self):
		asset = _make_sl_asset(
			asset_name=_unique("AUD-3997G"),
			net_purchase_amount=100_000,
			purchase_amount=100_000,
		)
		ava = _make_ava(asset, new_value=130_000, date="2026-03-15")
		ava.submit()
		self.assertGreater(flt(ava.difference_amount), 0)
		ads = get_asset_depr_schedule_doc(asset.name, "Active")
		self.assertAlmostEqual(
			sum(flt(r.depreciation_amount) for r in ads.depreciation_schedule),
			130_000,
			places=0,
		)

	def test_h_downward_revaluation_integration(self):
		asset = _make_sl_asset(
			asset_name=_unique("AUD-3997H"),
			net_purchase_amount=100_000,
			purchase_amount=100_000,
		)
		ava = _make_ava(asset, new_value=70_000, date="2026-03-15")
		ava.submit()
		self.assertLess(flt(ava.difference_amount), 0)
		asset.reload()
		self.assertAlmostEqual(flt(asset.value_after_depreciation), 70_000, places=2)
		ads = get_asset_depr_schedule_doc(asset.name, "Active")
		self.assertTrue(all(flt(r.depreciation_amount) >= 0 for r in ads.depreciation_schedule))

	def test_i_schedule_valid_after_reval(self):
		asset = _make_sl_asset(
			asset_name=_unique("AUD-3997I"),
			net_purchase_amount=120_000,
			purchase_amount=120_000,
			total_number_of_depreciations=6,
		)
		before = get_asset_depr_schedule_doc(asset.name, "Active")
		n_before = len(before.depreciation_schedule)
		ava = _make_ava(asset, new_value=150_000, date="2026-02-15")
		ava.submit()
		after = get_asset_depr_schedule_doc(asset.name, "Active")
		self.assertEqual(after.docstatus, 1)
		self.assertEqual(after.status, "Active")
		self.assertEqual(len(after.depreciation_schedule), n_before)
		before.reload()
		self.assertEqual(before.docstatus, 2)

	def test_j_cancellation_reverses(self):
		asset = _make_sl_asset(
			asset_name=_unique("AUD-3997J"),
			net_purchase_amount=90_000,
			purchase_amount=90_000,
		)
		before = flt(asset.value_after_depreciation)
		ava = _make_ava(asset, new_value=110_000, date="2026-04-01")
		ava.submit()
		je = ava.journal_entry
		ava.cancel()
		asset.reload()
		self.assertAlmostEqual(flt(asset.value_after_depreciation), before, places=2)
		active = frappe.db.count(
			"GL Entry",
			{"voucher_type": "Journal Entry", "voucher_no": je, "is_cancelled": 0},
		)
		self.assertEqual(active, 0)
		self.assertIsNotNone(get_asset_depr_schedule_doc(asset.name, "Active"))

	def test_d_zero_remaining_useful_life_is_config_not_silent_extend(self):
		"""Test D: VAD == salvage → depreciable total 0 (no silent life extension)."""
		asset, fb = _mock_asset_fb(
			net_purchase=60_000,
			vad=60_000,
			opening=0.0,
			salvage=60_000,
		)
		self.assertEqual(authoritative_depreciable_total(asset, fb), 0)
		rows = [{"schedule_date": "only", "depreciation_amount": 0}]
		normalize_full_schedule_amounts(rows, depreciable_total=0)
		self.assertEqual(rows[0]["depreciation_amount"], 0)

	def test_3997_shaped_fixture_reproduces_then_passes(self):
		"""Production-shaped economics: purchase==reval date, upward reval >> net purchase."""
		# Use smaller but proportionally similar figures to keep the suite fast.
		net = 4_250_664
		target = 4_545_567
		asset = _make_sl_asset(
			asset_name=_unique("AUD-3997P"),
			purchase_date="2026-07-23",
			available_for_use_date="2026-07-23",
			depreciation_start_date="2026-09-22",
			net_purchase_amount=net,
			purchase_amount=net,
			total_number_of_depreciations=24,
			frequency_of_depreciation=1,
			daily_prorata_based=1,
		)
		self.assertEqual(str(asset.purchase_date), "2026-07-23")
		self.assertEqual(str(asset.available_for_use_date), "2026-07-23")

		old_asset, old_fb = _mock_asset_fb(
			net_purchase=4_250_664_632_505.0,
			vad=4_545_567_146_822.363,
		)
		purchase_based = to_depr_amount(4_250_664_632_505.0)
		vad_based = authoritative_depreciable_total(old_asset, old_fb)
		self.assertGreater(vad_based, purchase_based)
		# N-1 rounded mass above purchase-based total (production overshoot shape)
		rows = [
			{"schedule_date": f"r{i}", "depreciation_amount": 400_000_000_000.0}
			for i in range(11)
		] + [
			{
				"schedule_date": "r11",
				"depreciation_amount": float(vad_based) - 11 * 400_000_000_000.0,
			}
		]
		with self.assertRaises(Exception) as ctx:
			normalize_full_schedule_amounts(
				[dict(r) for r in rows], depreciable_total=purchase_based
			)
		self.assertIn("negative", str(ctx.exception).lower())

		# Integration: AVA on same-day acquisition asset must succeed with VAD-based total.
		ava = _make_ava(asset, new_value=float(target), date="2026-07-23")
		ava.submit()
		asset.reload()
		self.assertAlmostEqual(flt(asset.value_after_depreciation), float(target), places=2)
		ads = get_asset_depr_schedule_doc(asset.name, "Active")
		self.assertTrue(all(flt(r.depreciation_amount) >= 0 for r in ads.depreciation_schedule))
		self.assertEqual(
			to_depr_amount(sum(flt(r.depreciation_amount) for r in ads.depreciation_schedule)),
			to_depr_amount(target),
		)
		self.assertTrue(_je_balanced(ava.journal_entry))
		# Dates preserved
		self.assertEqual(str(asset.purchase_date), "2026-07-23")
		self.assertEqual(str(asset.available_for_use_date), "2026-07-23")
		self.assertEqual(cint_calc(asset.calculate_depreciation), 1)


def cint_calc(v) -> int:
	return int(v or 0)


class TestNormalizeAdsDocumentPostedTail(unittest.TestCase):
	def test_normalize_ads_document_uses_vad_for_full_unposted(self):
		"""Exercise normalize_ads_document write-back with a fake ADS doc."""
		fx = ensure_replan_fixtures()
		from erpnext_extensions.asset_usage_depreciation.services.ads_amount_normalize import (
			company_requires_whole_ads,
		)

		if not company_requires_whole_ads(fx["company"]):
			self.skipTest("replan fixture company is not IRR")

		asset = _make_sl_asset(
			asset_name=_unique("AUD-3997N"),
			net_purchase_amount=100_000,
			purchase_amount=100_000,
			total_number_of_depreciations=4,
		)
		fb_name = asset.finance_books[0].name
		frappe.db.set_value("Asset Finance Book", fb_name, "value_after_depreciation", 160_000)
		frappe.db.set_value("Asset", asset.name, "value_after_depreciation", 160_000)

		ads = get_asset_depr_schedule_doc(asset.name, "Active")
		fake = frappe.copy_doc(ads)
		fake.name = None
		for row in fake.depreciation_schedule:
			row.journal_entry = None
			row.depreciation_amount = 160_000 / max(len(fake.depreciation_schedule), 1)
		normalize_ads_document(fake)
		total = sum(flt(r.depreciation_amount) for r in fake.depreciation_schedule)
		self.assertEqual(to_depr_amount(total), 160_000)
		self.assertTrue(all(flt(r.depreciation_amount) >= 0 for r in fake.depreciation_schedule))


class TestAsset3997DevCanaryV5740(unittest.TestCase):
	"""Rollback-only canary against development Asset 3997 (IRR). Never commits."""

	ASSET = "3997"
	TARGET = 4_545_567_146_822.363

	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		cls.available = bool(frappe.db.exists("Asset", cls.ASSET))

	def test_3997_reschedule_after_upward_reval_no_negative_final(self):
		if not self.available:
			self.skipTest("Asset 3997 not on this site")

		from erpnext.assets.doctype.asset_depreciation_schedule.asset_depreciation_schedule import (
			reschedule_depreciation,
		)

		frappe.db.begin()
		try:
			asset = frappe.get_doc("Asset", self.ASSET)
			self.assertEqual(str(asset.purchase_date), "2026-07-23")
			self.assertEqual(str(asset.available_for_use_date), "2026-07-23")
			self.assertEqual(int(asset.calculate_depreciation or 0), 1)

			ads = get_asset_depr_schedule_doc(self.ASSET, "Active")
			self.assertIsNotNone(ads)
			# Mimic production cleanup: depr JEs cancelled → JE links cleared
			for row in ads.depreciation_schedule:
				if row.journal_entry:
					frappe.db.sql(
						"update `tabDepreciation Schedule` set journal_entry=NULL where name=%s",
						row.name,
					)

			fb = asset.finance_books[0]
			fb.value_after_depreciation = self.TARGET
			fb.db_update()
			asset.value_after_depreciation = self.TARGET
			asset.db_update()
			asset.reload()

			self.assertEqual(
				authoritative_depreciable_total(asset, asset.finance_books[0]),
				to_depr_amount(self.TARGET),
			)

			reschedule_depreciation(asset, "v5.7.40 canary — rollback only")
			new = get_asset_depr_schedule_doc(self.ASSET, "Active")
			self.assertIsNotNone(new)
			amounts = [flt(r.depreciation_amount) for r in new.depreciation_schedule]
			self.assertTrue(all(a >= 0 for a in amounts))
			self.assertEqual(to_depr_amount(sum(amounts)), to_depr_amount(self.TARGET))
			self.assertTrue(is_whole_irr_amount(amounts[-1]))
		finally:
			frappe.db.rollback()

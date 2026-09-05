# Copyright (c) 2026, ERPNext Extensions contributors
"""IRR rate-first RIV preservation (3.8.4) — NEW documents only.

Mandatory fixtures:
  A) qty=1245, raw=2207006.162248996 → rate 2207006, amount 2747722470; RIV no drift
  B) MAT-STE-2026-03766 pattern; RIV ×2 idempotent

Also covers upgrade guard, wrapper install, non-IRR passthrough shape.
"""

from __future__ import annotations

import inspect
import unittest
from decimal import Decimal
from unittest import mock

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.domain.riv_rate_guard import (
	_FN_FINGERPRINTS,
	assert_erpnext_riv_rate_patch_supported,
	collect_fingerprint_report,
	make_update_rate_on_stock_entry_wrapper,
	normalize_callable_signature,
	normalize_function_source,
	source_sha256,
)
from erpnext_extensions.iran_accounting.e2e_bootstrap import (
	enable_perpetual_inventory,
	ensure_test_item,
	get_irr_company,
	get_second_warehouse,
	get_warehouse,
)
from erpnext_extensions.iran_accounting.tests.hardening.builders import (
	make_issue,
	make_manufacture,
	make_repack,
	make_transfer,
	run_riv,
	submit_receipt,
)


def _snap(se_name: str) -> dict:
	se = frappe.get_doc("Stock Entry", se_name)
	items = [
		{
			"basic_rate": flt(r.basic_rate),
			"basic_amount": flt(r.basic_amount),
			"amount": flt(r.amount),
			"valuation_rate": flt(r.valuation_rate),
			"additional_cost": flt(r.additional_cost),
			"landed_cost_voucher_amount": flt(r.landed_cost_voucher_amount),
			"qty": flt(r.qty),
		}
		for r in se.items
	]
	sle = frappe.get_all(
		"Stock Ledger Entry",
		filters={"voucher_no": se_name, "is_cancelled": 0},
		fields=["warehouse", "actual_qty", "stock_value_difference", "valuation_rate"],
	)
	gl = frappe.get_all(
		"GL Entry",
		filters={"voucher_no": se_name, "is_cancelled": 0},
		fields=["account", "debit", "credit"],
	)
	return {
		"items": items,
		"value_difference": flt(se.value_difference),
		"total_incoming": flt(se.total_incoming_value),
		"total_outgoing": flt(se.total_outgoing_value),
		"sle": [
			{
				"warehouse": r.warehouse,
				"actual_qty": flt(r.actual_qty),
				"stock_value_difference": flt(r.stock_value_difference),
				"valuation_rate": flt(r.valuation_rate),
			}
			for r in sle
		],
		"gl": [
			{"account": r.account, "debit": flt(r.debit), "credit": flt(r.credit)} for r in gl
		],
		"gl_debit": sum(flt(r.debit) for r in gl),
		"gl_credit": sum(flt(r.credit) for r in gl),
	}


class TestRivRateGuardUnit(unittest.TestCase):
	def test_fingerprints_match_allow_list(self):
		assert_erpnext_riv_rate_patch_supported()
		report = collect_fingerprint_report()
		self.assertIn(
			report["erpnext_major_minor"], {"16.29", "16.30", "16.31", "16.32", "16.33", "16.34"}
		)
		self.assertIn(report["frappe_major_minor"], {"16.29", "16.30", "16.31", "16.32", "16.33"})
		for name, expected in _FN_FINGERPRINTS.items():
			got = report["methods"][name]
			self.assertEqual(got["signature"], expected["signature"], name)
			accepted = {expected["source_sha256"], *expected.get("source_sha256_alternates", ())}
			self.assertIn(got["source_sha256"], accepted, name)

	def test_wrapper_skips_set_value_for_irr(self):
		calls = []

		def original(self, sle, outgoing_rate):
			calls.append(("original", outgoing_rate))

		engine = mock.Mock()
		engine.company = "IRR-CO"
		engine.is_manufacture_entry_with_sabb = mock.Mock(return_value=False)
		engine.recalculate_amounts_in_stock_entry = mock.Mock()
		sle = mock.Mock()
		sle.voucher_detail_no = "row-1"
		sle.voucher_no = "STE-1"
		sle.dependant_sle_voucher_detail_no = "dep"
		sle.company = "IRR-CO"

		with (
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.currency.is_irr_company",
				return_value=True,
			),
			mock.patch("frappe.db.get_value", return_value=frappe._dict(name="row-1", basic_rate=70154)),
			mock.patch("frappe.db.set_value") as set_value,
		):
			wrapped = make_update_rate_on_stock_entry_wrapper(original)
			wrapped(engine, sle, 70147.0)
			set_value.assert_not_called()
			engine.recalculate_amounts_in_stock_entry.assert_not_called()
			self.assertEqual(calls, [])

	def test_wrapper_non_irr_calls_original(self):
		calls = []

		def original(self, sle, outgoing_rate):
			calls.append(outgoing_rate)

		engine = mock.Mock()
		engine.company = "USD-CO"
		sle = mock.Mock()
		sle.voucher_detail_no = "row-1"
		sle.company = "USD-CO"
		with mock.patch(
			"erpnext_extensions.iran_accounting.domain.currency.is_irr_company",
			return_value=False,
		):
			wrapped = make_update_rate_on_stock_entry_wrapper(original)
			wrapped(engine, sle, 12.34)
		self.assertEqual(calls, [12.34])

	def test_fail_closed_on_missing_basic_rate(self):
		engine = mock.Mock()
		engine.company = "IRR-CO"
		sle = mock.Mock()
		sle.voucher_detail_no = "row-1"
		sle.voucher_no = "STE-1"
		sle.company = "IRR-CO"
		with (
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.currency.is_irr_company",
				return_value=True,
			),
			mock.patch(
				"frappe.db.get_value",
				return_value=frappe._dict(name="row-1", basic_rate=None),
			),
		):
			wrapped = make_update_rate_on_stock_entry_wrapper(lambda *a, **k: None)
			with self.assertRaises(Exception):
				wrapped(engine, sle, 1.0)

	def test_upgrade_guard_blocks_bad_hash(self):
		import erpnext.stock.stock_ledger as sl

		with mock.patch.dict(
			"erpnext_extensions.iran_accounting.domain.riv_rate_guard._FN_FINGERPRINTS",
			{
				"update_rate_on_stock_entry": {
					"signature": "(self, sle, outgoing_rate)",
					"source_sha256": "0" * 64,
					"must_contain": ("basic_rate",),
				},
				"recalculate_amounts_in_stock_entry": _FN_FINGERPRINTS[
					"recalculate_amounts_in_stock_entry"
				],
				"is_manufacture_entry_with_sabb": _FN_FINGERPRINTS["is_manufacture_entry_with_sabb"],
			},
			clear=False,
		):
			# force use of live original for hash check
			saved = getattr(sl.update_entries_after, "_iran_original_update_rate_on_stock_entry", None)
			try:
				if saved is not None:
					# temporarily clear so we still compare original body via saved
					pass
				with self.assertRaises(RuntimeError) as ctx:
					assert_erpnext_riv_rate_patch_supported()
				self.assertIn("fingerprint mismatch", str(ctx.exception).lower())
			finally:
				pass

	def test_annotation_variants_normalize_identically(self):
		"""Shared normalize contract: (doc) / -> None / -> 'None' / param ann."""
		import importlib.util
		import tempfile
		from pathlib import Path

		def load(src: str):
			path = Path(tempfile.mkdtemp()) / "riv_ann.py"
			path.write_text(src)
			spec = importlib.util.spec_from_file_location("riv_ann_stub", path)
			mod = importlib.util.module_from_spec(spec)
			assert spec.loader is not None
			spec.loader.exec_module(mod)
			return mod.f

		variants = [
			load("def f(doc):\n\tpass\n"),
			load("def f(doc) -> None:\n\tpass\n"),
			load('def f(doc) -> "None":\n\tpass\n'),
			load("def f(doc: object) -> None:\n\tpass\n"),
		]
		self.assertEqual(str(inspect.signature(variants[0])), "(doc)")
		self.assertEqual(str(inspect.signature(variants[2])), "(doc) -> 'None'")
		for fn in variants:
			self.assertEqual(normalize_callable_signature(fn), "(doc)")
		self.assertEqual(len({source_sha256(fn) for fn in variants}), 1)

	def test_executable_and_default_changes_still_fail_closed(self):
		import importlib.util
		import tempfile
		from pathlib import Path

		def load(src: str):
			path = Path(tempfile.mkdtemp()) / "riv_exec.py"
			path.write_text(src)
			spec = importlib.util.spec_from_file_location("riv_exec_stub", path)
			mod = importlib.util.module_from_spec(spec)
			assert spec.loader is not None
			spec.loader.exec_module(mod)
			return mod.f

		plain = load("def f(doc):\n\tpass\n")
		returned = load("def f(doc):\n\treturn doc\n")
		with_default = load("def f(doc=None):\n\tpass\n")
		annassign_plain = load("def f(doc):\n\tx = 1\n\treturn x\n")
		annassign_typed = load("def f(doc):\n\tx: int = 1\n\treturn x\n")

		self.assertNotEqual(source_sha256(plain), source_sha256(returned))
		self.assertNotEqual(
			normalize_callable_signature(plain), normalize_callable_signature(with_default)
		)
		self.assertEqual(
			normalize_function_source(annassign_plain),
			normalize_function_source(annassign_typed),
		)


class TestRivRateFirstIntegration(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		from erpnext_extensions.iran_accounting.integration.bootstrap import apply

		apply()
		frappe.set_user("Administrator")
		cls.company = get_irr_company("ESPAD")
		enable_perpetual_inventory(cls.company)
		cls.wh = get_warehouse(cls.company)
		cls.wh2 = get_second_warehouse(cls.company, cls.wh)

	def test_patch_installed_once(self):
		import erpnext.stock.stock_ledger as sl

		from erpnext_extensions.iran_accounting.integration.monkey_patches import (
			apply_monkey_patches,
		)

		apply_monkey_patches()
		apply_monkey_patches()
		fn = sl.update_entries_after.update_rate_on_stock_entry
		self.assertTrue(getattr(fn, "_iran_riv_rate_wrapper", False))
		self.assertTrue(getattr(sl, "_iran_patched_update_rate_on_stock_entry", False))
		orig = sl.update_entries_after._iran_original_update_rate_on_stock_entry
		self.assertEqual(str(inspect.signature(orig)), "(self, sle, outgoing_rate)")
		self.assertFalse(getattr(orig, "_iran_riv_rate_wrapper", False))

	def test_fixture_large_qty_rate_preserved_through_riv(self):
		"""Production fixture: 1245 × 2207006.162248996 → 2207006 / 2747722470."""
		qty = Decimal("1245")
		raw = Decimal("2207006.162248996")
		expected_rate = 2207006.0
		expected_amount = 2747722470.0
		item = ensure_test_item(f"RIV-FIX-A-{frappe.generate_hash(length=6)}", self.company)
		# Seed with integer rate close to raw so MA has residual potential after align
		submit_receipt(self.company, item, float(qty) + 10, float(raw), self.wh)
		se = make_transfer(self.company, item, qty, raw, self.wh, self.wh2)
		se.reload()
		row = se.items[0]
		self.assertEqual(flt(row.basic_rate), expected_rate)
		self.assertEqual(flt(row.basic_amount), expected_amount)
		self.assertEqual(flt(row.amount), expected_amount)
		before = _snap(se.name)
		run_riv(self.company, "Stock Entry", se.name)
		se.reload()
		after = _snap(se.name)
		self.assertEqual(after["items"][0]["basic_rate"], expected_rate)
		self.assertEqual(after["items"][0]["amount"], expected_amount)
		self.assertEqual(after["items"][0]["basic_amount"], expected_amount)
		self.assertEqual(before["items"], after["items"])
		self.assertEqual(before["gl_debit"], after["gl_debit"])
		run_riv(self.company, "Stock Entry", se.name)
		after2 = _snap(se.name)
		self.assertEqual(after["items"], after2["items"])
		self.assertEqual(after["gl_debit"], after2["gl_debit"])

	def test_fixture_03766_pattern_riv_x2(self):
		"""MAT-STE-2026-03766 pattern: fractional MA source → integer submit → RIV×2 stable."""
		qty = Decimal("3")
		raw = Decimal("70153.64912280702")
		expected_rate = 70154.0
		expected_amount = 210462.0
		item = ensure_test_item(f"RIV-FIX-B-{frappe.generate_hash(length=6)}", self.company)
		submit_receipt(self.company, item, 60, float(raw), self.wh)
		# Force fractional bin residual similar to production
		bin_name = frappe.db.get_value("Bin", {"item_code": item, "warehouse": self.wh}, "name")
		if bin_name:
			# stock_value slightly off vs qty*integer so MA would derive a different outgoing rate
			frappe.db.set_value(
				"Bin",
				bin_name,
				{"stock_value": 60 * float(raw), "valuation_rate": expected_rate},
				update_modified=False,
			)
			frappe.db.commit()
		se = make_transfer(self.company, item, qty, raw, self.wh, self.wh2)
		se.reload()
		self.assertEqual(flt(se.items[0].basic_rate), expected_rate)
		self.assertEqual(flt(se.items[0].amount), expected_amount)
		before = _snap(se.name)
		run_riv(self.company, "Stock Entry", se.name)
		mid = _snap(se.name)
		self.assertEqual(mid["items"], before["items"])
		self.assertEqual(mid["gl_debit"], before["gl_debit"])
		self.assertEqual(mid["sle"], before["sle"])
		run_riv(self.company, "Stock Entry", se.name)
		after = _snap(se.name)
		self.assertEqual(after["items"], before["items"])
		self.assertEqual(after["gl_debit"], before["gl_debit"])
		self.assertEqual(after["sle"], before["sle"])

	def test_mtfm_preserves_through_riv(self):
		item = ensure_test_item(f"RIV-MTFM-{frappe.generate_hash(length=6)}", self.company)
		se = make_transfer(
			self.company,
			item,
			Decimal("5"),
			Decimal("100.4"),
			self.wh,
			self.wh2,
			purpose="Material Transfer for Manufacture",
		)
		before = _snap(se.name)
		self.assertEqual(before["items"][0]["basic_rate"], 100.0)
		run_riv(self.company, "Stock Entry", se.name)
		after = _snap(se.name)
		self.assertEqual(after["items"], before["items"])

	def test_material_issue_preserves_through_riv(self):
		item = ensure_test_item(f"RIV-ISS-{frappe.generate_hash(length=6)}", self.company)
		se = make_issue(self.company, item, Decimal("4"), Decimal("250.6"), self.wh)
		before = _snap(se.name)
		run_riv(self.company, "Stock Entry", se.name)
		after = _snap(se.name)
		self.assertEqual(after["items"][0]["basic_rate"], before["items"][0]["basic_rate"])
		self.assertEqual(after["items"][0]["amount"], before["items"][0]["amount"])

	def test_manufacture_with_additional_cost_preserves(self):
		rm = ensure_test_item(f"RIV-RM-{frappe.generate_hash(length=6)}", self.company)
		fg = ensure_test_item(f"RIV-FG-{frappe.generate_hash(length=6)}", self.company)
		se, _oh = make_manufacture(
			self.company,
			rm_item=rm,
			fg_item=fg,
			rm_warehouse=self.wh,
			fg_warehouse=self.wh2,
			rm_qty=Decimal("10"),
			rm_rate=Decimal("1000.4"),
			fg_qty=Decimal("10"),
			additional_cost=Decimal("500"),
		)
		before = _snap(se.name)
		# RM rate integer; additional cost preserved through RIV
		run_riv(self.company, "Stock Entry", se.name)
		after = _snap(se.name)
		self.assertEqual(len(after["items"]), len(before["items"]))
		for b, a in zip(before["items"], after["items"], strict=True):
			self.assertEqual(a["basic_rate"], b["basic_rate"])
			self.assertEqual(a["amount"], b["amount"])
			self.assertEqual(a["additional_cost"], b["additional_cost"])

	def test_lcv_amount_preserved_through_riv(self):
		"""LCV composition on SE rows must survive RIV (no MA basic_rate overwrite side-effect)."""
		from erpnext_extensions.iran_accounting.domain.qty_rate_amount import (
			align_stock_entry_item_amounts,
		)

		item = ensure_test_item(f"RIV-LCV-{frappe.generate_hash(length=6)}", self.company)
		se = make_transfer(self.company, item, Decimal("5"), Decimal("1000"), self.wh, self.wh2)
		se.reload()
		row = se.items[0]
		row.landed_cost_voucher_amount = 250
		align_stock_entry_item_amounts(se)
		se.set_total_incoming_outgoing_value()
		for r in se.items:
			r.db_update()
		se.db_set(
			{
				"total_incoming_value": se.total_incoming_value,
				"total_outgoing_value": se.total_outgoing_value,
				"value_difference": se.value_difference,
			},
			update_modified=False,
		)
		frappe.db.commit()
		before = _snap(se.name)
		self.assertEqual(before["items"][0]["landed_cost_voucher_amount"], 250.0)
		self.assertEqual(before["items"][0]["amount"], before["items"][0]["basic_amount"] + 250.0)
		run_riv(self.company, "Stock Entry", se.name)
		after = _snap(se.name)
		self.assertEqual(after["items"][0]["basic_rate"], before["items"][0]["basic_rate"])
		self.assertEqual(
			after["items"][0]["landed_cost_voucher_amount"],
			before["items"][0]["landed_cost_voucher_amount"],
		)
		self.assertEqual(after["items"][0]["amount"], before["items"][0]["amount"])
	def test_repack_preserves_through_riv(self):
		item_in = ensure_test_item(f"RIV-RPIN-{frappe.generate_hash(length=6)}", self.company)
		item_out = ensure_test_item(f"RIV-RPOUT-{frappe.generate_hash(length=6)}", self.company)
		se = make_repack(
			self.company,
			item_in=item_in,
			item_out=item_out,
			warehouse=self.wh,
			qty_in=Decimal("8"),
			rate_in=Decimal("111.6"),
			qty_out=Decimal("8"),
		)
		before = _snap(se.name)
		run_riv(self.company, "Stock Entry", se.name)
		after = _snap(se.name)
		for b, a in zip(before["items"], after["items"], strict=True):
			self.assertEqual(a["basic_rate"], b["basic_rate"])
			self.assertEqual(a["amount"], b["amount"])


def run_riv_rate_first_suite():
	"""bench execute entrypoint."""
	from erpnext_extensions.iran_accounting.integration.bootstrap import apply

	apply()
	suite = unittest.defaultTestLoader.loadTestsFromTestCase(TestRivRateGuardUnit)
	suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(TestRivRateFirstIntegration))
	result = unittest.TextTestRunner(verbosity=2).run(suite)
	return {
		"ok": result.wasSuccessful(),
		"tests": result.testsRun,
		"failures": len(result.failures),
		"errors": len(result.errors),
	}

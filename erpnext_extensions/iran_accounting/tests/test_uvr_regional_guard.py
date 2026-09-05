# Copyright (c) 2026, ERPNext Extensions contributors
"""UVR regional upgrade guard (3.8.7) — fail-closed twin of riv_rate_guard.

Covers allow-list, fingerprints, missing symbols, install-once, bootstrap
RuntimeError, original preservation, and live PR / LCV / Return / PI / non-IRR.
"""

from __future__ import annotations

import inspect
import unittest
from unittest import mock

import frappe
from frappe.utils import flt, nowdate, nowtime

from erpnext_extensions.iran_accounting.domain.uvr_regional_guard import (
	_FN_FINGERPRINTS,
	assert_erpnext_uvr_regional_patch_supported,
	collect_fingerprint_report,
	normalize_callable_signature,
	normalize_function_source,
	source_sha256,
)


def _reset_uvr_patch():
	"""Restore vanilla regional symbol and clear install flag (test isolation)."""
	import erpnext.controllers.buying_controller as buying_controller

	saved = getattr(buying_controller, "_iran_original_regional_valuation_rate", None)
	if saved is not None:
		buying_controller.update_regional_item_valuation_rate = saved
	buying_controller._iran_patched_regional_valuation_rate = False
	if hasattr(buying_controller, "_iran_original_regional_valuation_rate"):
		delattr(buying_controller, "_iran_original_regional_valuation_rate")


def _ensure_uvr_patch():
	from erpnext_extensions.iran_accounting.integration.monkey_patches import (
		_patch_buying_regional_valuation_rate,
	)

	_patch_buying_regional_valuation_rate()


class TestUvrRegionalGuardUnit(unittest.TestCase):
	def tearDown(self):
		# Keep site usable for later tests / suite siblings.
		try:
			_ensure_uvr_patch()
		except Exception:
			pass

	def test_supported_version_passes(self):
		assert_erpnext_uvr_regional_patch_supported()
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
		self.assertTrue(report["methods"]["update_valuation_rate"]["calls_regional_hook"])

	def test_unsupported_version_blocks(self):
		with mock.patch(
			"erpnext_extensions.iran_accounting.domain.uvr_regional_guard.major_minor",
			side_effect=lambda v: "99.0",
		):
			with self.assertRaises(RuntimeError) as ctx:
				assert_erpnext_uvr_regional_patch_supported()
			msg = str(ctx.exception).lower()
			self.assertIn("upgrade guard", msg)
			self.assertIn("uvr integerization patch not installed", msg)

	def test_hash_mismatch_blocks(self):
		bad = {
			"update_valuation_rate": {
				"signature": _FN_FINGERPRINTS["update_valuation_rate"]["signature"],
				"source_sha256": "0" * 64,
				"must_contain": ("update_regional_item_valuation_rate",),
			},
			"update_regional_item_valuation_rate": _FN_FINGERPRINTS[
				"update_regional_item_valuation_rate"
			],
		}
		with mock.patch.dict(
			"erpnext_extensions.iran_accounting.domain.uvr_regional_guard._FN_FINGERPRINTS",
			bad,
			clear=True,
		):
			with self.assertRaises(RuntimeError) as ctx:
				assert_erpnext_uvr_regional_patch_supported()
			self.assertIn("fingerprint", str(ctx.exception).lower())
			self.assertIn("uvr integerization patch not installed", str(ctx.exception).lower())

	def test_missing_update_valuation_rate_blocks(self):
		from erpnext.controllers.buying_controller import BuyingController

		real_hasattr = hasattr

		def fake_hasattr(obj, name):
			if obj is BuyingController and name == "update_valuation_rate":
				return False
			return real_hasattr(obj, name)

		with mock.patch("builtins.hasattr", side_effect=fake_hasattr):
			with self.assertRaises(RuntimeError) as ctx:
				assert_erpnext_uvr_regional_patch_supported()
			self.assertIn("update_valuation_rate missing", str(ctx.exception).lower())

	def test_missing_regional_hook_blocks(self):
		import erpnext.controllers.buying_controller as buying_controller

		real_hasattr = hasattr

		def fake_hasattr(obj, name):
			if obj is buying_controller and name == "update_regional_item_valuation_rate":
				return False
			return real_hasattr(obj, name)

		with mock.patch("builtins.hasattr", side_effect=fake_hasattr):
			with self.assertRaises(RuntimeError) as ctx:
				assert_erpnext_uvr_regional_patch_supported()
			self.assertIn("update_regional_item_valuation_rate missing", str(ctx.exception).lower())

	def test_wrapper_installed_once(self):
		import erpnext.controllers.buying_controller as buying_controller

		from erpnext_extensions.iran_accounting.integration.monkey_patches import (
			_patch_buying_regional_valuation_rate,
			apply_monkey_patches,
		)

		_reset_uvr_patch()
		_patch_buying_regional_valuation_rate()
		first = buying_controller.update_regional_item_valuation_rate
		_patch_buying_regional_valuation_rate()
		apply_monkey_patches()
		second = buying_controller.update_regional_item_valuation_rate
		self.assertIs(first, second)
		self.assertTrue(getattr(buying_controller, "_iran_patched_regional_valuation_rate", False))
		self.assertEqual(first.__module__, "erpnext_extensions.iran_accounting.buying_selling")
		orig = buying_controller._iran_original_regional_valuation_rate
		self.assertIsNotNone(orig)
		self.assertNotEqual(orig.__module__, first.__module__)
		from erpnext_extensions.iran_accounting.domain.uvr_regional_guard import (
			normalize_callable_signature,
		)

		self.assertEqual(normalize_callable_signature(orig), "(doc)")

	def test_wrapper_not_installed_on_failure(self):
		import erpnext.controllers.buying_controller as buying_controller

		from erpnext_extensions.iran_accounting.integration.monkey_patches import (
			_patch_buying_regional_valuation_rate,
		)

		_reset_uvr_patch()
		vanilla = buying_controller.update_regional_item_valuation_rate
		with mock.patch(
			"erpnext_extensions.iran_accounting.domain.uvr_regional_guard.major_minor",
			side_effect=lambda v: "99.0",
		):
			with self.assertRaises(RuntimeError) as ctx:
				_patch_buying_regional_valuation_rate()
			self.assertIn("uvr integerization patch not installed", str(ctx.exception).lower())
		self.assertFalse(getattr(buying_controller, "_iran_patched_regional_valuation_rate", False))
		self.assertIs(buying_controller.update_regional_item_valuation_rate, vanilla)
		self.assertFalse(hasattr(buying_controller, "_iran_original_regional_valuation_rate"))

	def test_bootstrap_runtime_error(self):
		from erpnext_extensions.iran_accounting.integration.monkey_patches import (
			_patch_buying_regional_valuation_rate,
		)

		_reset_uvr_patch()
		with mock.patch(
			"erpnext_extensions.iran_accounting.domain.uvr_regional_guard._SUPPORTED_ERPNEXT_MINOR",
			frozenset({"0.0"}),
		):
			with self.assertRaises(RuntimeError) as ctx:
				_patch_buying_regional_valuation_rate()
			msg = str(ctx.exception)
			self.assertIn("IRR Upgrade Guard", msg)
			self.assertIn("UVR integerization patch not installed", msg)

	def test_original_function_preserved(self):
		import erpnext.controllers.buying_controller as buying_controller

		_reset_uvr_patch()
		_ensure_uvr_patch()
		orig = buying_controller._iran_original_regional_valuation_rate
		self.assertTrue(getattr(orig, "__wrapped__", None) or orig.__module__.startswith("erpnext."))
		from erpnext_extensions.iran_accounting.domain.uvr_regional_guard import (
			normalize_function_source,
		)
		import hashlib

		digest = hashlib.sha256(normalize_function_source(orig).encode()).hexdigest()
		self.assertEqual(
			digest, _FN_FINGERPRINTS["update_regional_item_valuation_rate"]["source_sha256"]
		)


class TestUvrFingerprintAnnotationInsensitive(unittest.TestCase):
	"""Regression: type hints must not diverge UVR regional fingerprints."""

	@staticmethod
	def _load_fn(source: str, name: str = "update_regional_item_valuation_rate"):
		"""Compile *source* to a real function with inspectable source lines."""
		import importlib.util
		import tempfile
		from pathlib import Path

		path = Path(tempfile.mkdtemp()) / "regional_stub.py"
		path.write_text(source)
		spec = importlib.util.spec_from_file_location(f"regional_stub_{path.stem}", path)
		mod = importlib.util.module_from_spec(spec)
		assert spec.loader is not None
		spec.loader.exec_module(mod)
		return getattr(mod, name)

	def test_doc_and_doc_arrow_none_same_fingerprint(self):
		header = "def allow_regional(fn):\n\treturn fn\n\n"
		plain = self._load_fn(
			header
			+ "@allow_regional\n"
			+ "def update_regional_item_valuation_rate(doc):\n"
			+ "\tpass\n"
		)
		ret_none = self._load_fn(
			header
			+ "@allow_regional\n"
			+ "def update_regional_item_valuation_rate(doc) -> None:\n"
			+ "\tpass\n"
		)
		ret_str_none = self._load_fn(
			header
			+ "@allow_regional\n"
			+ 'def update_regional_item_valuation_rate(doc) -> "None":\n'
			+ "\tpass\n"
		)
		param_ann = self._load_fn(
			header
			+ "@allow_regional\n"
			+ "def update_regional_item_valuation_rate(doc: object) -> None:\n"
			+ "\tpass\n"
		)

		self.assertEqual(str(inspect.signature(plain)), "(doc)")
		self.assertEqual(str(inspect.signature(ret_none)), "(doc) -> None")
		self.assertEqual(str(inspect.signature(ret_str_none)), "(doc) -> 'None'")

		for fn in (plain, ret_none, ret_str_none, param_ann):
			self.assertEqual(normalize_callable_signature(fn), "(doc)")

		digests = {source_sha256(fn) for fn in (plain, ret_none, ret_str_none, param_ann)}
		self.assertEqual(len(digests), 1, digests)
		norms = {normalize_function_source(fn) for fn in (plain, ret_none, ret_str_none, param_ann)}
		self.assertEqual(len(norms), 1, norms)
		self.assertIn("allow_regional", next(iter(norms)))
		self.assertIn("pass", next(iter(norms)))
		self.assertNotIn("->", next(iter(norms)))

	def test_executable_body_change_still_diverges(self):
		header = "def allow_regional(fn):\n\treturn fn\n\n"
		vanilla = self._load_fn(
			header
			+ "@allow_regional\n"
			+ "def update_regional_item_valuation_rate(doc) -> None:\n"
			+ "\tpass\n"
		)
		changed = self._load_fn(
			header
			+ "@allow_regional\n"
			+ "def update_regional_item_valuation_rate(doc) -> None:\n"
			+ "\treturn doc\n"
		)
		self.assertEqual(normalize_callable_signature(vanilla), normalize_callable_signature(changed))
		self.assertNotEqual(source_sha256(vanilla), source_sha256(changed))

	def test_guard_accepts_return_annotation_variant(self):
		"""Simulate production (doc) -> 'None' against allow-list signature (doc)."""
		header = "def allow_regional(fn):\n\treturn fn\n\n"
		annotated = self._load_fn(
			header
			+ "@allow_regional\n"
			+ 'def update_regional_item_valuation_rate(doc) -> "None":\n'
			+ "\tpass\n"
		)
		expected = _FN_FINGERPRINTS["update_regional_item_valuation_rate"]["signature"]
		self.assertEqual(normalize_callable_signature(annotated), expected)
		# Raw inspect still shows the production-shaped divergence.
		self.assertEqual(str(inspect.signature(annotated)), "(doc) -> 'None'")
		self.assertNotEqual(str(inspect.signature(annotated)), expected)

	def test_default_and_param_name_changes_diverge(self):
		header = "def allow_regional(fn):\n\treturn fn\n\n"
		base = self._load_fn(
			header + "@allow_regional\ndef update_regional_item_valuation_rate(doc):\n\tpass\n"
		)
		with_default = self._load_fn(
			header
			+ "@allow_regional\n"
			+ "def update_regional_item_valuation_rate(doc=None):\n"
			+ "\tpass\n"
		)
		renamed = self._load_fn(
			header + "@allow_regional\ndef update_regional_item_valuation_rate(document):\n\tpass\n"
		)
		self.assertEqual(normalize_callable_signature(base), "(doc)")
		self.assertEqual(normalize_callable_signature(with_default), "(doc=None)")
		self.assertEqual(normalize_callable_signature(renamed), "(document)")
		self.assertNotEqual(
			normalize_callable_signature(base), normalize_callable_signature(with_default)
		)
		self.assertNotEqual(
			normalize_callable_signature(base), normalize_callable_signature(renamed)
		)

	def test_annassign_annotation_only_normalizes_to_assign(self):
		plain = self._load_fn(
			"def sample(doc):\n"
			"\tx = 1\n"
			"\treturn x\n",
			name="sample",
		)
		annotated = self._load_fn(
			"def sample(doc):\n"
			"\tx: int = 1\n"
			"\treturn x\n",
			name="sample",
		)
		self.assertEqual(normalize_function_source(plain), normalize_function_source(annotated))
		self.assertEqual(source_sha256(plain), source_sha256(annotated))

	def test_live_allowlist_digest_unchanged_for_unannotated_regional(self):
		"""Existing ERPNext stub without annotations must keep allow-list digest."""
		import erpnext.controllers.buying_controller as buying_controller

		from erpnext_extensions.iran_accounting.domain.uvr_regional_guard import (
			_resolve_vanilla_regional,
		)

		regional = _resolve_vanilla_regional(buying_controller)
		self.assertEqual(normalize_callable_signature(regional), "(doc)")
		self.assertEqual(
			source_sha256(regional),
			_FN_FINGERPRINTS["update_regional_item_valuation_rate"]["source_sha256"],
		)


class TestUvrRegionalGuardIntegration(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		from erpnext_extensions.iran_accounting.integration.bootstrap import apply

		apply()
		frappe.set_user("Administrator")
		# Prefer the primary IRR production company on this bench; fall back to any IRR.
		cls.company = (
			"اسپاد فارمد دارو"
			if frappe.db.exists("Company", "اسپاد فارمد دارو")
			else (
				frappe.db.get_value("Company", {"default_currency": "IRR", "name": "test"}, "name")
				or frappe.db.get_value("Company", {"default_currency": "IRR"}, "name")
			)
		)
		if not cls.company:
			raise unittest.SkipTest("No IRR company")
		cls.wh = frappe.db.get_value(
			"Warehouse", {"company": cls.company, "is_group": 0}, "name"
		)
		cls.supplier = frappe.db.get_value("Supplier", {"disabled": 0}, "name")
		cls.uom = frappe.db.get_single_value("Stock Settings", "stock_uom") or "Nos"
		cls.ig = frappe.db.get_single_value("Stock Settings", "item_group") or frappe.db.get_value(
			"Item Group", {"is_group": 0}, "name"
		)
		cls.cc = frappe.db.get_value("Company", cls.company, "cost_center")
		cls.exp = frappe.db.get_value("Company", cls.company, "default_expense_account")
		if not cls.exp:
			cls.exp = frappe.db.get_value(
				"Account",
				{"company": cls.company, "root_type": "Expense", "is_group": 0},
				"name",
			)
		# Site Server Script requires this exact department on PR/PI item rows.
		cls.dept = (
			"واحد انبار - E"
			if frappe.db.exists("Department", "واحد انبار - E")
			else (
				frappe.db.get_value("Department", {"company": cls.company}, "name")
				or frappe.db.get_value("Department", {}, "name")
			)
		)

	def _item(self, prefix: str) -> str:
		code = f"{prefix}-{frappe.generate_hash(length=5)}"
		doc = frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": code,
				"item_name": code,
				"item_group": self.ig,
				"stock_uom": self.uom,
				"is_stock_item": 1,
			}
		).insert(ignore_permissions=True)
		# Stock Settings may use Naming Series — return the persisted Item name.
		return doc.name

	def _assert_integer_vr(self, parent: str, doctype_item: str = "Purchase Receipt Item"):
		vr = frappe.db.get_value(doctype_item, {"parent": parent}, "valuation_rate")
		self.assertIsNotNone(vr)
		self.assertEqual(flt(vr), float(int(flt(vr))), msg=f"non-integer VR {vr} on {parent}")

	def test_pr_submit_integerizes_valuation_rate(self):
		_ensure_uvr_patch()
		code = self._item("UVR-PR")
		pr = frappe.new_doc("Purchase Receipt")
		pr.company = self.company
		pr.supplier = self.supplier
		pr.currency = "IRR"
		pr.conversion_rate = 1
		pr.posting_date = nowdate()
		pr.posting_time = nowtime()
		pr.set_posting_time = 1
		if self.dept:
			pr.department = self.dept
		pr.append(
			"items",
			{
				"item_code": code,
				"qty": 10,
				"rate": 1000000,
				"uom": self.uom,
				"stock_uom": self.uom,
				"conversion_factor": 1,
				"warehouse": self.wh,
				"department": self.dept,
				"cost_center": self.cc,
			},
		)
		pr.insert()
		pr.submit()
		frappe.db.commit()
		self._assert_integer_vr(pr.name)

	def test_lcv_integerizes_valuation_rate(self):
		_ensure_uvr_patch()
		code = self._item("UVR-LCV")
		pr = frappe.new_doc("Purchase Receipt")
		pr.company = self.company
		pr.supplier = self.supplier
		pr.currency = "IRR"
		pr.conversion_rate = 1
		pr.posting_date = nowdate()
		pr.posting_time = nowtime()
		pr.set_posting_time = 1
		if self.dept:
			pr.department = self.dept
		pr.append(
			"items",
			{
				"item_code": code,
				"qty": 10,
				"rate": 1000000,
				"uom": self.uom,
				"stock_uom": self.uom,
				"conversion_factor": 1,
				"warehouse": self.wh,
				"department": self.dept,
				"cost_center": self.cc,
			},
		)
		pr.insert()
		pr.submit()
		frappe.db.commit()

		lcv = frappe.new_doc("Landed Cost Voucher")
		lcv.company = self.company
		lcv.posting_date = nowdate()
		lcv.append(
			"purchase_receipts",
			{"receipt_document_type": "Purchase Receipt", "receipt_document": pr.name},
		)
		lcv.append(
			"taxes",
			{"expense_account": self.exp, "description": "UVR-GUARD-F", "amount": 1},
		)
		lcv.get_items_from_purchase_receipts()
		lcv.insert()
		lcv.submit()
		frappe.db.commit()
		self._assert_integer_vr(pr.name)
		vr = flt(frappe.db.get_value("Purchase Receipt Item", {"parent": pr.name}, "valuation_rate"))
		self.assertEqual(vr, 1000000.0)

	def test_purchase_return_integerizes(self):
		_ensure_uvr_patch()
		code = self._item("UVR-RET")
		pr = frappe.new_doc("Purchase Receipt")
		pr.company = self.company
		pr.supplier = self.supplier
		pr.currency = "IRR"
		pr.conversion_rate = 1
		pr.posting_date = nowdate()
		pr.posting_time = nowtime()
		pr.set_posting_time = 1
		if self.dept:
			pr.department = self.dept
		pr.append(
			"items",
			{
				"item_code": code,
				"qty": 5,
				"rate": 100000,
				"uom": self.uom,
				"stock_uom": self.uom,
				"conversion_factor": 1,
				"warehouse": self.wh,
				"department": self.dept,
				"cost_center": self.cc,
			},
		)
		pr.insert()
		pr.submit()
		frappe.db.commit()

		ret = frappe.new_doc("Purchase Receipt")
		ret.company = self.company
		ret.supplier = self.supplier
		ret.currency = "IRR"
		ret.conversion_rate = 1
		ret.posting_date = nowdate()
		ret.posting_time = nowtime()
		ret.set_posting_time = 1
		ret.is_return = 1
		ret.return_against = pr.name
		if self.dept:
			ret.department = self.dept
		ret.append(
			"items",
			{
				"item_code": code,
				"qty": -2,
				"rate": 100000,
				"uom": self.uom,
				"stock_uom": self.uom,
				"conversion_factor": 1,
				"warehouse": self.wh,
				"department": self.dept,
				"cost_center": self.cc,
				"purchase_receipt_item": frappe.db.get_value(
					"Purchase Receipt Item", {"parent": pr.name}, "name"
				),
			},
		)
		ret.insert()
		ret.submit()
		frappe.db.commit()
		self._assert_integer_vr(ret.name)

	def test_pi_update_stock_integerizes(self):
		_ensure_uvr_patch()
		code = self._item("UVR-PI")
		pi = frappe.new_doc("Purchase Invoice")
		pi.company = self.company
		pi.supplier = self.supplier
		pi.currency = "IRR"
		pi.conversion_rate = 1
		pi.posting_date = nowdate()
		pi.update_stock = 1
		pi.set_posting_time = 1
		pi.posting_time = nowtime()
		pi.remarks = "UVR regional guard PI fixture"
		if self.dept:
			pi.department = self.dept
		pi.append(
			"items",
			{
				"item_code": code,
				"qty": 3,
				"rate": 50000,
				"uom": self.uom,
				"stock_uom": self.uom,
				"conversion_factor": 1,
				"warehouse": self.wh,
				"department": self.dept,
				"cost_center": self.cc,
				"expense_account": self.exp,
			},
		)
		pi.insert()
		pi.submit()
		frappe.db.commit()
		self._assert_integer_vr(pi.name, "Purchase Invoice Item")

	def test_non_irr_passthrough(self):
		"""Regional Iran binding is installed, but align no-ops for non-IRR companies."""
		from erpnext_extensions.iran_accounting.buying_selling import (
			update_regional_item_valuation_rate,
		)

		non_irr = frappe.db.get_value(
			"Company", {"default_currency": ("!=", "IRR")}, "name"
		)
		if not non_irr:
			self.skipTest("No non-IRR company")

		class _R:
			valuation_rate = 12.345

			def get(self, k, default=None):
				return getattr(self, k, default)

		class _D:
			company = non_irr
			currency = frappe.db.get_value("Company", non_irr, "default_currency")
			items = None

			def __init__(self):
				self.items = [_R()]

			def get(self, k, default=None):
				return getattr(self, k, default)

		doc = _D()
		update_regional_item_valuation_rate(doc)
		self.assertEqual(doc.items[0].valuation_rate, 12.345)


def run_uvr_regional_guard_suite():
	from erpnext_extensions.iran_accounting.integration.bootstrap import apply

	apply()
	suite = unittest.defaultTestLoader.loadTestsFromTestCase(TestUvrRegionalGuardUnit)
	suite.addTests(
		unittest.defaultTestLoader.loadTestsFromTestCase(TestUvrFingerprintAnnotationInsensitive)
	)
	suite.addTests(
		unittest.defaultTestLoader.loadTestsFromTestCase(TestUvrRegionalGuardIntegration)
	)
	result = unittest.TextTestRunner(verbosity=2).run(suite)
	return {
		"ok": result.wasSuccessful(),
		"tests": result.testsRun,
		"failures": len(result.failures),
		"errors": len(result.errors),
	}

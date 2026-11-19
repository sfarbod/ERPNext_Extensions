# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.8.0 corrective — Iran make_entry must forward Core from_reversal (16.54+).

Covers:
  A) Wrapper signature + forward to original
  B) make_reverse_gl_entries without TypeError
  C/D) Exercised by RIV / UVR integration suites after this fix
"""

from __future__ import annotations

import inspect
import unittest
from unittest import mock

import erpnext
import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.domain.riv_rate_guard import (
	normalize_callable_signature,
	source_sha256,
)
from erpnext_extensions.iran_accounting.integration.monkey_patches import (
	_MAKE_ENTRY_FINGERPRINT,
	assert_make_entry_patch_supported,
)


class TestMakeEntryFingerprintGuard(unittest.TestCase):
	def test_live_core_fingerprint_allow_listed(self):
		from erpnext.accounts import general_ledger as gl

		# Prefer saved vanilla original after bootstrap; else live Core.
		from erpnext_extensions.iran_accounting.integration.bootstrap import apply

		apply()
		orig = getattr(gl, "_iran_original_make_entry", None) or gl.make_entry
		# Unwrap Iran wrapper if somehow still live
		if getattr(orig, "_iran_make_entry_from_reversal", False):
			orig = gl._iran_original_make_entry
		assert_make_entry_patch_supported(orig)
		sig = normalize_callable_signature(orig)
		self.assertIn(sig, _MAKE_ENTRY_FINGERPRINT["signatures"])
		accepted = {
			_MAKE_ENTRY_FINGERPRINT["source_sha256"],
			*(_MAKE_ENTRY_FINGERPRINT.get("source_sha256_alternates") or ()),
		}
		self.assertIn(source_sha256(orig), accepted)

	def test_unsupported_version_blocks(self):
		fn = mock.Mock()
		fn.__name__ = "make_entry"
		with mock.patch.object(erpnext, "__version__", "16.56.0"), mock.patch.object(
			frappe, "__version__", "16.60.2"
		):
			with self.assertRaises(RuntimeError) as ctx:
				assert_make_entry_patch_supported(fn)
			self.assertIn("16.56.0", str(ctx.exception))

	def test_bad_signature_blocks(self):
		def make_entry(args, adv_adj, update_outstanding, from_repost=False, extra=None):
			return None

		with mock.patch(
			"erpnext_extensions.iran_accounting.integration.monkey_patches.source_sha256",
			create=True,
		):
			# Patch helpers used inside assert via riv_rate_guard imports
			with mock.patch(
				"erpnext_extensions.iran_accounting.domain.riv_rate_guard.source_sha256",
				return_value=_MAKE_ENTRY_FINGERPRINT["source_sha256"],
			), mock.patch(
				"erpnext_extensions.iran_accounting.domain.riv_rate_guard.normalize_function_source",
				return_value="def make_entry(): GL Entry from_repost",
			):
				with self.assertRaises(RuntimeError) as ctx:
					assert_make_entry_patch_supported(make_entry)
				self.assertIn("signature", str(ctx.exception).lower())


class TestMakeEntryWrapperForward(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		from erpnext_extensions.iran_accounting.integration.bootstrap import apply

		apply()

	def test_wrapper_signature_has_from_reversal_kwonly(self):
		from erpnext.accounts import general_ledger as gl

		sig = inspect.signature(gl.make_entry)
		self.assertIn("from_reversal", sig.parameters)
		self.assertEqual(sig.parameters["from_reversal"].kind, inspect.Parameter.KEYWORD_ONLY)
		self.assertTrue(getattr(gl.make_entry, "_iran_make_entry_from_reversal", False))

	def test_from_repost_positional_and_from_reversal_forwarded(self):
		from erpnext.accounts import general_ledger as gl

		calls = []

		def _orig(args, adv_adj, update_outstanding, from_repost=False, *, from_reversal=False):
			calls.append(
				{
					"args": args,
					"adv_adj": adv_adj,
					"update_outstanding": update_outstanding,
					"from_repost": from_repost,
					"from_reversal": from_reversal,
				}
			)
			return "OK"

		# Bypass Iran zero-value short-circuit by omitting company
		args = frappe._dict(debit=1, credit=0)
		with mock.patch.object(gl, "_iran_original_make_entry", _orig):
			# Positional from_repost still works
			gl.make_entry(args, False, "Yes", True)
			self.assertEqual(calls[-1]["from_repost"], True)
			self.assertEqual(calls[-1]["from_reversal"], False)
			# Keyword-only from_reversal forwarded
			gl.make_entry(args, False, "Yes", False, from_reversal=True)
			self.assertEqual(calls[-1]["from_repost"], False)
			self.assertEqual(calls[-1]["from_reversal"], True)
			self.assertEqual(calls[-1]["update_outstanding"], "Yes")

	def test_legacy_core_without_from_reversal_rejects_true_flag(self):
		from erpnext.accounts import general_ledger as gl

		def _legacy(args, adv_adj, update_outstanding, from_repost=False):
			return "LEGACY"

		args = frappe._dict(debit=1, credit=0)
		with mock.patch.object(gl, "_iran_original_make_entry", _legacy):
			self.assertEqual(gl.make_entry(args, False, "Yes", False), "LEGACY")
			with self.assertRaises(TypeError) as ctx:
				gl.make_entry(args, False, "Yes", False, from_reversal=True)
			self.assertIn("from_reversal", str(ctx.exception))


class TestMakeReverseGlEntriesCompat(unittest.TestCase):
	"""B: make_reverse_gl_entries must not TypeError on from_reversal."""

	@classmethod
	def setUpClass(cls):
		from erpnext_extensions.iran_accounting.integration.bootstrap import apply

		apply()
		frappe.set_user("Administrator")
		cls.company = (
			"اسپاد فارمد دارو"
			if frappe.db.exists("Company", "اسپاد فارمد دارو")
			else frappe.db.get_value("Company", {"default_currency": "IRR"}, "name")
		)
		if not cls.company:
			raise unittest.SkipTest("No IRR company")

	def test_make_reverse_gl_entries_accepts_from_reversal_path(self):
		"""Call Core make_reverse_gl_entries against a synthetic active GL pair.

		Uses an isolated Journal Entry (balance-sheet accounts + cost center) so
		we do not touch mutable canaries or P&L cost-center requirements.
		"""
		from erpnext.accounts.general_ledger import make_reverse_gl_entries

		company = self.company
		cc = frappe.db.get_value("Company", company, "cost_center") or frappe.db.get_value(
			"Cost Center", {"company": company, "is_group": 0}, "name"
		)
		# Two balance-sheet accounts avoid P&L cost-center enforcement ambiguity.
		assets = frappe.get_all(
			"Account",
			filters={"company": company, "root_type": "Asset", "is_group": 0},
			pluck="name",
			limit=5,
		)
		if len(assets) < 2 or not cc:
			self.skipTest("Need ≥2 asset accounts and a non-group cost center")
		debit_acct, credit_acct = assets[0], assets[1]

		frappe.db.savepoint("v580_gl_reverse")
		try:
			je = frappe.new_doc("Journal Entry")
			je.company = company
			je.posting_date = frappe.utils.nowdate()
			je.voucher_type = "Journal Entry"
			je.user_remark = "v5.8.0 make_entry from_reversal regression"
			je.append(
				"accounts",
				{
					"account": debit_acct,
					"debit_in_account_currency": 1,
					"credit_in_account_currency": 0,
					"cost_center": cc,
				},
			)
			je.append(
				"accounts",
				{
					"account": credit_acct,
					"debit_in_account_currency": 0,
					"credit_in_account_currency": 1,
					"cost_center": cc,
				},
			)
			je.insert()
			je.submit()
			before = frappe.get_all(
				"GL Entry",
				filters={"voucher_no": je.name, "is_cancelled": 0},
				fields=["name", "debit", "credit"],
			)
			self.assertTrue(before)
			# Must not raise TypeError(... from_reversal ...)
			make_reverse_gl_entries(voucher_type="Journal Entry", voucher_no=je.name)
			active = frappe.get_all(
				"GL Entry",
				filters={"voucher_no": je.name, "is_cancelled": 0},
				fields=["name", "debit", "credit"],
			)
			# After reverse, net active movement should cancel (all cancelled or net zero)
			net = sum(flt(r.debit) - flt(r.credit) for r in active)
			self.assertEqual(flt(net), 0.0)
		finally:
			frappe.db.rollback(save_point="v580_gl_reverse")


if __name__ == "__main__":
	unittest.main(verbosity=2)

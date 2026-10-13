# Copyright (c) 2026, ERPNext Extensions contributors
"""API security / dry-run tests for PR billing refresh (#59476)."""

from __future__ import annotations

import unittest

import frappe

from erpnext_extensions.iran_accounting.pr_billing_refresh import api as billing_api
from erpnext_extensions.iran_accounting.pr_billing_refresh.service import (
	MAX_ALLOWLIST,
	BillingRefreshError,
	_parse_allowlist,
	fingerprint_pr,
	refresh_allowlist,
	require_post_for_mutation,
	require_privileged_user,
)


class TestAllowlistParsing(unittest.TestCase):
	def test_requires_allowlist(self):
		with self.assertRaises(BillingRefreshError):
			_parse_allowlist(None)
		with self.assertRaises(BillingRefreshError):
			_parse_allowlist([])
		with self.assertRaises(BillingRefreshError):
			_parse_allowlist("")

	def test_dedupe_and_max(self):
		names = _parse_allowlist(["A", "B", "A"])
		self.assertEqual(names, ["A", "B"])
		with self.assertRaises(BillingRefreshError):
			_parse_allowlist([f"PR-{i}" for i in range(MAX_ALLOWLIST + 1)])


class TestPrivilegeAndPostGuard(unittest.TestCase):
	def test_guest_denied(self):
		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.PermissionError):
				require_privileged_user()
		finally:
			frappe.set_user("Administrator")

	def test_post_required_for_mutation(self):
		class Req:
			method = "GET"

		frappe.local.request = Req()
		with self.assertRaises(frappe.PermissionError):
			require_post_for_mutation(dry_run=False)
		# dry-run may use GET
		require_post_for_mutation(dry_run=True)
		frappe.local.request = type("R", (), {"method": "POST"})()
		require_post_for_mutation(dry_run=False)


class TestApiDryRunOnRestoredDocs(unittest.TestCase):
	"""Uses restored Development docs when present; otherwise skips."""

	PR = "MAT-PRE-2026-01416"

	def setUp(self):
		frappe.set_user("Administrator")
		if not frappe.db.exists("Purchase Receipt", self.PR):
			self.skipTest(f"{self.PR} not on this site")

	def test_fingerprint_stable(self):
		pr = frappe.get_doc("Purchase Receipt", self.PR)
		fp1 = fingerprint_pr(pr)
		fp2 = fingerprint_pr(frappe.get_doc("Purchase Receipt", self.PR))
		self.assertEqual(fp1, fp2)
		res = billing_api.get_purchase_receipt_billing_fingerprint(self.PR)
		self.assertEqual(res["fingerprint"], fp1)

	def test_dry_run_idempotent_after_repair(self):
		res = billing_api.dry_run_purchase_receipt_billing_refresh([self.PR])
		self.assertTrue(res["ok"])
		row = res["results"][0]
		self.assertEqual(row["status"], "dry_run")
		# After Dev repair, expected delta should be ~0
		self.assertLessEqual(abs(row["server_expected_delta"]), 1.0)

	def test_fingerprint_mismatch_blocks_mutation(self):
		frappe.local.request = type("R", (), {"method": "POST"})()
		res = refresh_allowlist(
			[self.PR],
			dry_run=False,
			fingerprints={self.PR: "deadbeef" * 8},
		)
		self.assertEqual(res["results"][0]["status"], "fingerprint_mismatch")

	def test_mutation_defaults_dry_run(self):
		# Explicit dry_run=1 path via whitelist wrapper
		res = billing_api.refresh_purchase_receipt_billing([self.PR], dry_run=1)
		self.assertTrue(res["summary"]["dry_run"])
		self.assertEqual(res["results"][0]["status"], "dry_run")

	def test_rejects_client_financial_authority(self):
		# service signature has no expected_diff/amount args — only allowlist + fingerprints
		import inspect

		sig = inspect.signature(refresh_allowlist)
		self.assertNotIn("expected_diff", sig.parameters)
		self.assertNotIn("expected_amount", sig.parameters)
		self.assertNotIn("amount_difference", sig.parameters)

	def test_batch_reports_fingerprint_mismatch_explicitly(self):
		frappe.local.request = type("R", (), {"method": "POST"})()
		res = refresh_allowlist(
			[self.PR],
			dry_run=False,
			fingerprints={self.PR: "0" * 64},
		)
		self.assertFalse(res["ok"])
		self.assertIn(self.PR, res["summary"]["fingerprint_mismatch_prs"])
		self.assertEqual(res["summary"]["statuses"].get("fingerprint_mismatch"), 1)


class TestNoArbitraryExecution(unittest.TestCase):
	def test_api_surface_is_narrow(self):
		public = [n for n in dir(billing_api) if not n.startswith("_")]
		# Only the three whitelisted entrypoints should be the public contract
		for name in public:
			obj = getattr(billing_api, name)
			if callable(obj) and getattr(obj, "__module__", "").endswith("pr_billing_refresh.api"):
				self.assertIn(
					name,
					{
						"get_purchase_receipt_billing_fingerprint",
						"dry_run_purchase_receipt_billing_refresh",
						"refresh_purchase_receipt_billing",
					},
				)

# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.7.10 — Historical Rebuild campaign-scoped REST API security and scope."""

from __future__ import annotations

import unittest
from unittest import mock

import frappe

from erpnext_extensions.iran_accounting.historical_rebuild import api as hrc_api
from erpnext_extensions.iran_accounting.historical_rebuild import api_service as svc
from erpnext_extensions.iran_accounting.historical_rebuild.discovery import manifest_checksum
from erpnext_extensions.iran_accounting.historical_stock.permissions import require_repair


class TestParseAndGuardsV5710(unittest.TestCase):
	def test_parse_voucher_list_rejects_empty(self):
		with self.assertRaises(frappe.ValidationError):
			svc.parse_voucher_list([])

	def test_parse_voucher_list_rejects_unsupported_type(self):
		with self.assertRaises(frappe.ValidationError):
			svc.parse_voucher_list([{"voucher_type": "Journal Entry", "voucher_no": "X"}])

	def test_parse_voucher_list_accepts_stock_entry(self):
		out = svc.parse_voucher_list(
			[{"voucher_type": "Stock Entry", "voucher_no": "MAT-STE-1"}]
		)
		self.assertEqual(out, [("Stock Entry", "MAT-STE-1")])

	def test_guest_cannot_repair(self):
		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.PermissionError):
				require_repair()
		finally:
			frappe.set_user("Administrator")

	def test_create_requires_confirm_token(self):
		frappe.set_user("Administrator")
		with self.assertRaises(frappe.ValidationError):
			svc.create_exact_campaign(
				company="X",
				vouchers=[{"voucher_type": "Stock Entry", "voucher_no": "A"}],
				identities_allowlist=[{"item_code": "I", "warehouse": "W"}],
				expected_checksum="abc",
				confirm="NOPE",
			)

	def test_start_requires_confirm_token(self):
		frappe.set_user("Administrator")
		with self.assertRaises(frappe.ValidationError):
			svc.start_sequential_campaign(
				campaign="HRC-00099",
				expected_checksum="abc",
				confirm="NOPE",
			)


class TestChecksumAndAllowlistV5710(unittest.TestCase):
	def test_manifest_checksum_stable(self):
		rows = [
			{
				"doctype": "Stock Entry",
				"name": "A",
				"posting_date": "2026-01-01",
				"posting_time": "10:00:00",
				"creation": "2026-01-01 10:00:00",
				"company": "C",
			},
			{
				"doctype": "Stock Entry",
				"name": "B",
				"posting_date": "2026-01-02",
				"posting_time": "11:00:00",
				"creation": "2026-01-02 11:00:00",
				"company": "C",
			},
		]
		a = manifest_checksum(rows)
		b = manifest_checksum(list(rows))
		self.assertEqual(a, b)
		self.assertEqual(len(a), 64)

	def test_out_of_scope_allowlist_rejected(self):
		from erpnext_extensions.iran_accounting.historical_rebuild.scoped_campaign import (
			create_exact_voucher_campaign,
		)

		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.scoped_campaign.expand_identities_from_vouchers",
			return_value=[("ITEM1", "WH1")],
		), mock.patch(
			"frappe.db.get_value",
			return_value=frappe._dict(
				company="C",
				posting_date="2026-01-01",
				posting_time="00:00:00",
				creation="2026-01-01",
			),
		), mock.patch(
			"frappe.db.get_single_value", return_value="C"
		):
			with self.assertRaises(Exception) as ctx:
				create_exact_voucher_campaign(
					company="C",
					vouchers=[{"voucher_type": "Stock Entry", "voucher_no": "SE1"}],
					identities_allowlist=[{"item_code": "OTHER", "warehouse": "WH1"}],
					expected_checksum=None,
				)
			self.assertIn("out-of-scope", str(ctx.exception).lower())

	def test_checksum_mismatch_rejected(self):
		from erpnext_extensions.iran_accounting.historical_rebuild.scoped_campaign import (
			create_exact_voucher_campaign,
		)

		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.scoped_campaign.expand_identities_from_vouchers",
			return_value=[("ITEM1", "WH1")],
		), mock.patch(
			"frappe.db.get_value",
			return_value=frappe._dict(
				company="C",
				posting_date="2026-01-01",
				posting_time="00:00:00",
				creation="2026-01-01",
			),
		):
			with self.assertRaises(Exception) as ctx:
				create_exact_voucher_campaign(
					company="C",
					vouchers=[{"voucher_type": "Stock Entry", "voucher_no": "SE1"}],
					identities_allowlist=[{"item_code": "ITEM1", "warehouse": "WH1"}],
					expected_checksum="0" * 64,
				)
			self.assertIn("checksum", str(ctx.exception).lower())


class TestApiSurfaceV5710(unittest.TestCase):
	def test_status_endpoint_read_only_shape(self):
		frappe.set_user("Administrator")
		out = hrc_api.get_rebuild_api_status()
		self.assertTrue(out.get("ok"))
		self.assertTrue(out.get("read_only"))
		self.assertEqual(out.get("api_version"), "1.0.0")
		caps = out.get("capabilities") or {}
		self.assertTrue(caps.get("create_exact_campaign"))
		self.assertFalse(caps.get("prepare_clean_start_exposed"))
		self.assertFalse(caps.get("site_wide_riv_exposed"))

	def test_whitelist_methods_exist(self):
		for name in (
			"get_rebuild_api_status",
			"inspect_vouchers",
			"discover_dependencies",
			"create_exact_campaign",
			"preflight_campaign",
			"start_sequential_campaign",
			"get_campaign_status",
			"request_campaign_stop",
			"validate_campaign_results",
		):
			self.assertTrue(callable(getattr(hrc_api, name)))


class TestSixVoucherAcceptanceFixtureV5710(unittest.TestCase):
	"""Acceptance seeds for the SA Manufacture set (no Production mutation)."""

	SEEDS = (
		"MAT-STE-2026-40332",
		"MAT-STE-2026-40149",
		"MAT-STE-2026-36826",
		"MAT-STE-2026-36928",
		"MAT-STE-2026-40902",
		"MAT-STE-2026-43589",
	)

	def test_seed_list_length(self):
		self.assertEqual(len(self.SEEDS), 6)

	def test_discover_on_dev_when_present(self):
		frappe.set_user("Administrator")
		company = frappe.db.get_value("Company", {"name": ("like", "%اسپاد%")}, "name")
		if not company:
			self.skipTest("Espad company not on this site")
		present = [n for n in self.SEEDS if frappe.db.exists("Stock Entry", n)]
		if len(present) < 6:
			self.skipTest(f"only {len(present)}/6 seed SEs present on Dev")
		vouchers = [{"voucher_type": "Stock Entry", "voucher_no": n} for n in present]
		out = hrc_api.discover_dependencies(company=company, vouchers=vouchers, mode="exact")
		self.assertTrue(out.get("ok"))
		self.assertEqual(out.get("mode"), "exact")
		self.assertEqual(out.get("manifest_total"), 6)
		self.assertTrue(out.get("checksum"))
		self.assertGreater(out.get("identity_count") or 0, 0)
		self.assertEqual(out.get("expected_riv_count"), out.get("identity_count"))


if __name__ == "__main__":
	unittest.main()

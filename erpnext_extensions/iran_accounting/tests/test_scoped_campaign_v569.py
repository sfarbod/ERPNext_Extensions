# Copyright (c) 2026, ERPNext Extensions contributors
"""Scoped HRC Manifest construction contracts (v5.6.9)."""

from __future__ import annotations

import unittest
from unittest import mock

from erpnext_extensions.iran_accounting.historical_rebuild.scoped_campaign import (
	SCOPED_REMARK_PREFIX,
	_normalize_seed_identities,
	_normalize_seed_vouchers,
	build_scoped_manifest,
)


class TestScopedCampaignNormalizeV569(unittest.TestCase):
	def test_normalize_vouchers_dedupes(self):
		rows = _normalize_seed_vouchers(
			[
				{"voucher_type": "Stock Entry", "voucher_no": "A"},
				("Stock Entry", "A"),
				{"doctype": "Stock Entry", "name": "B"},
			]
		)
		self.assertEqual(rows, [("Stock Entry", "A"), ("Stock Entry", "B")])

	def test_normalize_identities_dedupes(self):
		rows = _normalize_seed_identities(
			[
				{"item_code": "1", "warehouse": "W"},
				("1", "W"),
				{"item_code": "2", "warehouse": "W"},
			]
		)
		self.assertEqual(rows, [("1", "W"), ("2", "W")])

	def test_build_scoped_manifest_expands_and_orders(self):
		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.scoped_campaign.expand_identities_from_vouchers",
			return_value=[("I1", "W1")],
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.scoped_campaign.collect_vouchers_for_identities",
			return_value=[
				{
					"doctype": "Stock Entry",
					"name": "SE-2",
					"company": "C",
					"posting_date": "2026-05-02",
					"posting_time": "10:00:00",
					"creation": "2026-05-02 10:00:00",
					"ordinal": 1,
				},
				{
					"doctype": "Stock Entry",
					"name": "SE-1",
					"company": "C",
					"posting_date": "2026-05-01",
					"posting_time": "09:00:00",
					"creation": "2026-05-01 09:00:00",
					"ordinal": 2,
				},
			],
		):
			# collect returns unsorted on purpose; build must re-order.
			built = build_scoped_manifest(
				company="C",
				seed_vouchers=[("Stock Entry", "SE-1")],
				seed_identities=[("I2", "W2")],
			)
		self.assertEqual(built["total"], 2)
		self.assertEqual(built["rows"][0]["name"], "SE-1")
		self.assertEqual(built["rows"][1]["name"], "SE-2")
		self.assertEqual(built["identity_count"], 2)
		self.assertTrue(built["checksum"])

	def test_scoped_remark_prefix(self):
		self.assertTrue(SCOPED_REMARK_PREFIX.startswith("SCOPED_HRC_TEST:"))

	def test_is_scoped_survives_seq_session_prefix(self):
		from erpnext_extensions.iran_accounting.historical_rebuild import scoped_campaign as sc

		with mock.patch.object(
			sc.frappe.db,
			"get_value",
			return_value=f"SEQ_SESSION=20260101010101\n{SCOPED_REMARK_PREFIX}5.6.9-validation",
		):
			self.assertTrue(sc.is_scoped_campaign("HRC-TEST"))


if __name__ == "__main__":
	unittest.main()

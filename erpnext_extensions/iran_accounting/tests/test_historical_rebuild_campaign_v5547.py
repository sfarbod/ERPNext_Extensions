# Copyright (c) 2026, ERPNext Extensions contributors
"""Unit / integration tests for Historical Rebuild Campaign (5.5.47)."""

from __future__ import annotations

import unittest
from unittest import mock

import frappe

from erpnext_extensions.iran_accounting.historical_rebuild import campaign as camp
from erpnext_extensions.iran_accounting.historical_rebuild.constants import (
	ITEM_PENDING,
	STATUS_PAUSED,
	STATUS_READY,
	STATUS_RUNNING,
	STATUS_STOPPED,
)
from erpnext_extensions.iran_accounting.historical_rebuild.discovery import (
	build_source_manifest,
	manifest_checksum,
)


class TestHistoricalRebuildDiscovery(unittest.TestCase):
	def test_manifest_deterministic_order_and_checksum(self):
		rows = [
			{
				"doctype": "Stock Entry",
				"name": "B",
				"posting_date": "2026-01-02",
				"posting_time": "10:00:00",
				"creation": "2026-01-02 10:00:00",
				"company": "C",
			},
			{
				"doctype": "Stock Entry",
				"name": "A",
				"posting_date": "2026-01-01",
				"posting_time": "09:00:00",
				"creation": "2026-01-01 09:00:00",
				"company": "C",
			},
		]
		# Sort like discovery
		rows.sort(
			key=lambda r: (
				str(r["posting_date"]),
				str(r.get("posting_time") or ""),
				str(r.get("creation") or ""),
				str(r["doctype"]),
				str(r["name"]),
			)
		)
		for i, r in enumerate(rows, start=1):
			r["ordinal"] = i
		self.assertEqual(rows[0]["name"], "A")
		c1 = manifest_checksum(rows)
		c2 = manifest_checksum(rows)
		self.assertEqual(c1, c2)
		self.assertEqual(len(c1), 64)

	def test_manifest_dedup_identity(self):
		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.discovery.frappe.db.exists",
			return_value=True,
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.discovery.frappe.get_meta",
		) as gm, mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.discovery.frappe.get_all",
		) as ga:
			gm.return_value = mock.Mock(has_field=lambda f: True)
			# Return duplicate names from one doctype source
			ga.return_value = [
				mock.Mock(
					name="X1",
					company="C",
					posting_date="2026-01-01",
					posting_time="01:00:00",
					creation="2026-01-01 01:00:00",
				),
				mock.Mock(
					name="X1",
					company="C",
					posting_date="2026-01-01",
					posting_time="01:00:00",
					creation="2026-01-01 01:00:00",
				),
			]
			# Limit SOURCE_SPECS to one via patching constants used in module
			with mock.patch(
				"erpnext_extensions.iran_accounting.historical_rebuild.discovery.SOURCE_SPECS",
				({"doctype": "Stock Entry", "filters": {"docstatus": 1}},),
			):
				rows = build_source_manifest(company="C")
		names = [(r["doctype"], r["name"]) for r in rows]
		self.assertEqual(len(names), len(set(names)))


class TestCampaignLifecycleFlags(unittest.TestCase):
	def test_pause_stop_flags_block_selection(self):
		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.campaign.frappe.db.get_value"
		) as gv:
			gv.return_value = mock.Mock(status=STATUS_PAUSED, pause_requested=1, stop_requested=0)
			self.assertEqual(camp._flags_block_selection("HRC"), "paused")
			gv.return_value = mock.Mock(status=STATUS_STOPPED, pause_requested=0, stop_requested=1)
			self.assertEqual(camp._flags_block_selection("HRC"), "stopped")
			gv.return_value = mock.Mock(status=STATUS_RUNNING, pause_requested=0, stop_requested=0)
			self.assertIsNone(camp._flags_block_selection("HRC"))


class TestSourceRebuildDiagnose(unittest.TestCase):
	def test_non_stock_entry_not_required(self):
		from erpnext_extensions.iran_accounting.historical_rebuild.source_rebuild import (
			diagnose_source_needs,
		)

		out = diagnose_source_needs("Delivery Note", "DN-1")
		self.assertFalse(out["required"])


class TestTvaReconcile(unittest.TestCase):
	def test_non_stock_entry_na(self):
		from erpnext_extensions.iran_accounting.historical_rebuild.tva_reconcile import (
			reconcile_tva_for_voucher,
		)

		out = reconcile_tva_for_voucher("Purchase Receipt", "PR-1")
		self.assertEqual(out["status"], "Not Applicable")

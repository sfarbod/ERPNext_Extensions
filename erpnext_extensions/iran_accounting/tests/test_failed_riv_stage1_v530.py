# Copyright (c) 2026, ERPNext Extensions contributors
"""Unit tests — Failed RIV Stage-1 cheap screen (v5.3.0)."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from erpnext_extensions.iran_accounting.historical_stock import SLE_HEALTHY, SLE_POISONED_CHAIN
from erpnext_extensions.iran_accounting.historical_stock.failed_riv import stage1_screen_failed_rivs
from erpnext_extensions.iran_accounting.historical_stock.riv_preflight import (
	classify_failed_riv_current_impact,
)


def _doc(**kw):
	base = dict(
		name="RIV-1",
		item_code="ITEM",
		warehouse="WH",
		voucher_no="STE-1",
		posting_date="2026-01-01",
		error_log="",
		creation="2026-01-01 00:00:00",
	)
	base.update(kw)
	return base


class TestFailedRIVStage1(unittest.TestCase):
	@patch(
		"erpnext_extensions.iran_accounting.historical_stock.failed_riv.frappe.db.sql",
		return_value=[],
	)
	@patch(
		"erpnext_extensions.iran_accounting.historical_stock.sle_bin.classify_identity",
		return_value=SLE_HEALTHY,
	)
	def test_healthy_identity_is_historical_only_cheap(self, _id, _sql):
		out = stage1_screen_failed_rivs([_doc(), _doc(name="RIV-2")])
		self.assertEqual(out["cheap_count"], 2)
		self.assertEqual(len(out["expensive_docs"]), 0)
		self.assertEqual(out["historical_only"], 2)
		self.assertEqual(out["cheap_rows"][0]["riv_reconcile_status"], "HISTORICAL_ONLY")
		self.assertFalse(out["cheap_rows"][0]["actionable"])
		self.assertEqual(out["cheap_rows"][0]["stage"], "cheap")

	@patch(
		"erpnext_extensions.iran_accounting.historical_stock.failed_riv.frappe.db.sql",
		return_value=[
			SimpleNamespace(
				item_code="ITEM",
				warehouse="WH",
				name="RIV-OK",
				creation="2026-06-01 00:00:00",
			)
		],
	)
	@patch(
		"erpnext_extensions.iran_accounting.historical_stock.sle_bin.classify_identity",
		return_value=SLE_HEALTHY,
	)
	def test_later_completed_is_superseded(self, _id, _sql):
		out = stage1_screen_failed_rivs([_doc(creation="2026-01-01 00:00:00")])
		self.assertEqual(out["superseded"], 1)
		self.assertEqual(out["cheap_rows"][0]["riv_reconcile_status"], "SUPERSEDED_BY_SUCCESSFUL_REPAIR")
		self.assertFalse(out["cheap_rows"][0]["actionable"])

	@patch(
		"erpnext_extensions.iran_accounting.historical_stock.failed_riv.frappe.db.sql",
		return_value=[],
	)
	@patch(
		"erpnext_extensions.iran_accounting.historical_stock.sle_bin.classify_identity",
		return_value=SLE_POISONED_CHAIN,
	)
	def test_poisoned_identity_goes_to_stage2(self, _id, _sql):
		out = stage1_screen_failed_rivs([_doc()])
		self.assertEqual(len(out["expensive_docs"]), 1)
		self.assertEqual(out["cheap_count"], 0)
		self.assertEqual(out["current_impact_cheap"], 1)


class TestFailedRIVHealthySkipPreflight(unittest.TestCase):
	@patch(
		"erpnext_extensions.iran_accounting.historical_stock.riv_preflight.riv_preflight_gate"
	)
	@patch(
		"erpnext_extensions.iran_accounting.historical_stock.riv_preflight.frappe.db.sql",
		return_value=[],
	)
	@patch(
		"erpnext_extensions.iran_accounting.historical_stock.sle_bin.classify_identity",
		return_value=SLE_HEALTHY,
	)
	def test_healthy_skips_preflight_by_default(self, _id, _sql, preflight):
		out = classify_failed_riv_current_impact(_doc())
		self.assertEqual(out["riv_reconcile_status"], "HISTORICAL_ONLY")
		self.assertEqual(out["stage"], "cheap")
		preflight.assert_not_called()

	@patch(
		"erpnext_extensions.iran_accounting.historical_stock.riv_preflight.riv_preflight_gate",
		return_value={"eligible": True},
	)
	@patch(
		"erpnext_extensions.iran_accounting.historical_stock.riv_preflight.frappe.db.sql",
		return_value=[],
	)
	@patch(
		"erpnext_extensions.iran_accounting.historical_stock.sle_bin.classify_identity",
		return_value=SLE_HEALTHY,
	)
	def test_opt_in_expensive_preflight(self, _id, _sql, preflight):
		out = classify_failed_riv_current_impact(_doc(), skip_preflight_when_healthy=False)
		self.assertEqual(out["riv_reconcile_status"], "HISTORICAL_ONLY")
		self.assertEqual(out["stage"], "expensive")
		preflight.assert_called_once()


class TestWorkerQueueMatch(unittest.TestCase):
	def test_prefixed_queue_matches_bare_name(self):
		from erpnext_extensions.iran_accounting.historical_stock.metrics_snapshot import (
			_queue_name_matches,
		)

		self.assertTrue(_queue_name_matches("long", "long"))
		self.assertTrue(_queue_name_matches("workspace-development-frappe-bench:long", "long"))
		self.assertFalse(_queue_name_matches("workspace-development-frappe-bench:default", "long"))
		self.assertFalse(_queue_name_matches("", "long"))


if __name__ == "__main__":
	unittest.main()

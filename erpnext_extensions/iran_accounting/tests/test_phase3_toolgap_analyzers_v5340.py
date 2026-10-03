# Copyright (c) 2026, ERPNext Extensions contributors
"""Phase-3 tool-gap analyzers + CROSS_TIME independent-flow contract."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from erpnext_extensions.iran_accounting.historical_stock.failed_riv_causal import (
	RESOLVED_BY_UPSTREAM,
	SAFE_TO_RETRY_AFTER_ROOT_FIX,
	analyze_failed_riv_row,
)
from erpnext_extensions.iran_accounting.historical_stock.next_downtime.cross_time import (
	INDEPENDENT_PRODUCTION_FLOWS,
	classify_cross_time,
)
from erpnext_extensions.iran_accounting.historical_stock.poisoned_opening import (
	PRECISION_DUST,
	analyze_identity_opening,
)


class TestI4CheckpointRepairMetadata(unittest.TestCase):
	def test_checkpoint_helper_requires_healthy_prior(self):
		from erpnext_extensions.iran_accounting.historical_stock.i4_repair import (
			_checkpoint_replay_clears_tip,
		)

		with patch(
			"erpnext_extensions.iran_accounting.historical_stock.i4_repair._last_healthy_zero_checkpoint",
			return_value=None,
		):
			out = _checkpoint_replay_clears_tip(
				"I",
				"W",
				SimpleNamespace(posting_datetime="2026-08-22 18:06:35", name="SLE"),
			)
		self.assertIsNone(out)


class TestIndependentProductionFlows(unittest.TestCase):
	def test_distinct_work_orders_never_cross_time_repair(self):
		row = {
			"detection": "CROSS_TIME",
			"status": "CROSS_TIME_REPAIRABLE",
			"outbound_work_order": "MFG-WO-2026-00731",
			"inbound_work_order": "MFG-WO-2026-00727",
			"outbound_document": "MAT-STE-2026-33163",
			"inbound_document": "MAT-STE-2026-33338",
			"batch": "938-13100057-2218002482",
		}
		ct = classify_cross_time(row)
		self.assertEqual(ct["class"], INDEPENDENT_PRODUCTION_FLOWS)
		self.assertFalse(ct["repair"])


class TestFailedRivCausal(unittest.TestCase):
	def test_valuation_integrity_with_i4_tip_waits_for_root(self):
		with patch(
			"erpnext_extensions.iran_accounting.historical_stock.failed_riv_causal._tip_state",
			return_value=SimpleNamespace(q=0.0, sv=5000.0, vr=0.0, voucher_no="X"),
		):
			out = analyze_failed_riv_row(
				{
					"name": "RIV-1",
					"item_code": "I",
					"warehouse": "W",
					"riv_reconcile_status": "CURRENT_LEDGER_IMPACT",
					"error_class": "VALUATION_INTEGRITY",
				}
			)
		self.assertEqual(out["status"], SAFE_TO_RETRY_AFTER_ROOT_FIX)
		self.assertFalse(out["safe_to_retry_now"])

	def test_healthy_tip_marks_resolved_by_upstream(self):
		with patch(
			"erpnext_extensions.iran_accounting.historical_stock.failed_riv_causal._tip_state",
			return_value=SimpleNamespace(q=10.0, sv=1000.0, vr=100.0, voucher_no="X"),
		):
			out = analyze_failed_riv_row(
				{
					"name": "RIV-2",
					"item_code": "I",
					"warehouse": "W",
					"riv_reconcile_status": "CURRENT_LEDGER_IMPACT",
					"error_class": "VALUATION_INTEGRITY",
				}
			)
		self.assertEqual(out["status"], RESOLVED_BY_UPSTREAM)


class TestPoisonedOpening(unittest.TestCase):
	def test_dust_opening_classified(self):
		row = SimpleNamespace(
			name="SLE1",
			voucher_no="SR-1",
			voucher_type="Stock Reconciliation",
			batch_no=None,
			aq=0,
			q=0,
			ir=0,
			ogr=0,
			vr=0,
			sv=0.01,
			svd=0.01,
			posting_datetime="2026-01-01",
		)
		term = SimpleNamespace(
			name="SLE1",
			voucher_no="SR-1",
			q=0,
			sv=0.01,
			vr=0,
			posting_datetime="2026-01-01",
		)
		with patch("frappe.db.sql", side_effect=[[row], [term]]):
			out = analyze_identity_opening("I", "W")
		self.assertEqual(out["status"], PRECISION_DUST)
		self.assertFalse(out["repairable_auto"])

	def test_coherent_opening_healthy_tip_is_legitimate(self):
		from erpnext_extensions.iran_accounting.historical_stock.poisoned_opening import (
			LEGITIMATE,
		)

		opening = SimpleNamespace(
			name="SLE1",
			voucher_no="STE-1",
			voucher_type="Stock Entry",
			batch_no=None,
			aq=100,
			q=100,
			ir=10,
			ogr=0,
			vr=10,
			sv=1000,
			svd=1000,
			posting_datetime="2026-01-01",
		)
		term = SimpleNamespace(
			name="SLE9",
			voucher_no="STE-9",
			q=50,
			sv=500,
			vr=10,
			posting_datetime="2026-06-01",
		)
		with patch("frappe.db.sql", side_effect=[[opening], [term]]):
			out = analyze_identity_opening("I", "W")
		self.assertEqual(out["status"], LEGITIMATE)
		self.assertEqual(out["origin"], "mid_chain_historical_residue_tip_healthy")


if __name__ == "__main__":
	unittest.main()

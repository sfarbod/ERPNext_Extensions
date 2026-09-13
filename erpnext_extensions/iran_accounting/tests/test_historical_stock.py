# Copyright (c) 2026, ERPNext Extensions contributors
"""Unit tests for 5.2.7 historical stock integrity (no operator writes)."""

from __future__ import annotations

import unittest
from unittest import mock

from erpnext_extensions.iran_accounting.historical_stock import (
	CONFIDENCE_AMBIGUOUS,
	CONFIDENCE_EXACT,
	G0_HEALTHY,
	G1_ECONOMICALLY_WRONG,
	STATUS_DEPENDENCY_REPAIR_REQUIRED,
	Z0_LEGITIMATE_ZERO,
	Z1_HISTORICAL_RATE_LOST,
)
from erpnext_extensions.iran_accounting.historical_stock.patient_zero import find_patient_zero, transition_is_invalid
from erpnext_extensions.iran_accounting.historical_stock.util import last_nonzero_from_version, parse_numeric, version_row_rates
from erpnext_extensions.iran_accounting.stock_posting_order.replay import replay_series, sle_poison_reason


def _sle(**kw):
	return kw


class TestParseVersion(unittest.TestCase):
	def test_parse_rial_rate(self):
		self.assertEqual(parse_numeric("ریال 40,378"), 40378)
		self.assertEqual(parse_numeric(0.0), 0)
		self.assertEqual(parse_numeric("118700"), 118700)

	def test_version_row_lost_rate(self):
		blob = {
			"row_changed": [
				[
					"items",
					4,
					"b690fm0e60",
					[
						["basic_rate", "ریال 40,378", 0],
						["amount", "ریال 1,090,206", 0],
					],
				]
			]
		}
		mapped = version_row_rates(blob)
		self.assertEqual(last_nonzero_from_version(mapped["b690fm0e60"]), 40378)


class TestPatientZero(unittest.TestCase):
	def test_nonzero_to_zero_incoming(self):
		prev = _sle(
			voucher_no="A",
			actual_qty=10,
			incoming_rate=100,
			valuation_rate=100,
			stock_value=1000,
			stock_value_difference=1000,
			qty_after_transaction=10,
		)
		row = _sle(
			name="s2",
			voucher_no="B",
			item_code="X",
			warehouse="W",
			actual_qty=5,
			incoming_rate=0,
			valuation_rate=0,
			stock_value=1000,
			stock_value_difference=0,
			qty_after_transaction=15,
			posting_datetime="2026-09-03 12:00:00",
		)
		self.assertEqual(transition_is_invalid(prev, row), "nonzero_to_zero_incoming")
		pz = find_patient_zero([prev, row])
		self.assertEqual(pz["voucher_no"], "B")

	def test_healthy_chain_no_patient(self):
		a = _sle(
			voucher_no="A",
			actual_qty=10,
			incoming_rate=100,
			valuation_rate=100,
			stock_value=1000,
			stock_value_difference=1000,
			qty_after_transaction=10,
		)
		b = _sle(
			voucher_no="B",
			actual_qty=-2,
			incoming_rate=0,
			valuation_rate=100,
			stock_value=800,
			stock_value_difference=-200,
			qty_after_transaction=8,
		)
		self.assertIsNone(find_patient_zero([a, b]))


class TestReplayDoesNotUseLiveBin(unittest.TestCase):
	def test_replay_uses_incoming_rate_not_bin(self):
		rows = [
			_sle(
				name="1",
				voucher_no="IN",
				actual_qty=10,
				incoming_rate=50,
				valuation_rate=50,
				stock_value=500,
				stock_value_difference=500,
			),
			_sle(
				name="2",
				voucher_no="OUT",
				actual_qty=-4,
				incoming_rate=0,
				valuation_rate=50,
				stock_value=300,
				stock_value_difference=-200,
			),
		]
		series = replay_series(rows, 0, 0)
		self.assertEqual(float(series[-1]["qty_after_transaction"]), 6)
		self.assertEqual(float(series[-1]["stock_value"]), 300)

	def test_zero_incoming_falls_back_to_running_average(self):
		rows = [
			_sle(name="1", voucher_no="IN", actual_qty=10, incoming_rate=40, valuation_rate=40, stock_value=400, stock_value_difference=400),
			_sle(name="2", voucher_no="IN2", actual_qty=10, incoming_rate=0, valuation_rate=0, stock_value=400, stock_value_difference=0),
		]
		series = replay_series(rows, 0, 0)
		# second inbound at 0 uses running avg 40
		self.assertAlmostEqual(float(series[1]["stock_value_difference"]), 400)


class TestPoisonReasons(unittest.TestCase):
	def test_negative_incoming(self):
		self.assertEqual(
			sle_poison_reason(_sle(actual_qty=1, incoming_rate=-5, valuation_rate=1, stock_value=1, stock_value_difference=-5, qty_after_transaction=1)),
			"negative_incoming_rate",
		)

	def test_exploded(self):
		self.assertEqual(
			sle_poison_reason(
				_sle(actual_qty=1, incoming_rate=1, valuation_rate=1e13, stock_value=1, stock_value_difference=1, qty_after_transaction=1)
			),
			"exploded_rate",
		)


class TestRuntimeGuardEvidence(unittest.TestCase):
	def test_no_evidence_allows_true_zero(self):
		from erpnext_extensions.iran_accounting.historical_stock.runtime_guard import evidence_for_zero_outgoing

		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_stock.runtime_guard._batch_inward_rate",
			return_value=0,
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_stock.runtime_guard._previous_healthy_sle_rate",
			return_value=0,
		):
			rate, src = evidence_for_zero_outgoing("X", "W", "B", "2026-01-01", "12:00:00")
			self.assertEqual(rate, 0)
			self.assertIsNone(src)

	def test_batch_evidence(self):
		from erpnext_extensions.iran_accounting.historical_stock.runtime_guard import evidence_for_zero_outgoing

		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_stock.runtime_guard._batch_inward_rate",
			return_value=40378,
		):
			rate, src = evidence_for_zero_outgoing("X", "W", "B", "2026-01-01", "12:00:00")
			self.assertEqual(rate, 40378)
			self.assertEqual(src, "batch_inward")


def _classify_with(row, **extra):
	from erpnext_extensions.iran_accounting.historical_stock.zero_rate import classify_zero_row

	mod = "erpnext_extensions.iran_accounting.historical_stock.zero_rate"
	version = extra.get("version", (0, 0))
	if not isinstance(version, tuple):
		version = (version, 0)
	with mock.patch(f"{mod}._version_rate", return_value=version), mock.patch(
		f"{mod}._batch_inward_rate", return_value=extra.get("batch", 0)
	), mock.patch(
		f"{mod}._previous_healthy_sle_rate", return_value=extra.get("prev", 0)
	), mock.patch(
		f"{mod}._source_transfer_sle_rate", return_value=extra.get("transfer", 0)
	), mock.patch(
		f"{mod}._patient_for_identity", return_value=extra.get("patient", None)
	), mock.patch(
		f"{mod}._identity_poisoned", return_value=extra.get("poisoned", False)
	):
		return classify_zero_row(row)


class TestClassifyZeroLogic(unittest.TestCase):
	def test_allow_zero_is_z0(self):
		row = {
			"name": "d1",
			"parent": "SE-1",
			"purpose": "Material Transfer",
			"item_code": "X",
			"qty": 1,
			"s_warehouse": "W",
			"t_warehouse": "T",
			"basic_rate": 0,
			"allow_zero_valuation_rate": 1,
			"posting_date": "2026-01-01",
			"posting_time": "10:00:00",
		}
		out = _classify_with(row)
		self.assertEqual(out["zero_class"], Z0_LEGITIMATE_ZERO)
		self.assertFalse(out["eligible"])

	def test_stock_reconciliation_is_authoritative(self):
		row = {
			"name": "d1",
			"parent": "SR-1",
			"purpose": "Stock Reconciliation",
			"item_code": "X",
			"qty": 1,
			"t_warehouse": "W",
			"basic_rate": 0,
			"allow_zero_valuation_rate": 0,
		}
		out = _classify_with(row)
		self.assertEqual(out["zero_class"], Z0_LEGITIMATE_ZERO)
		self.assertEqual(out["source_of_truth"], "document_authoritative")
		self.assertFalse(out["eligible"])

	def test_version_plus_batch_exact(self):
		row = {
			"name": "b690fm0e60",
			"parent": "MAT-STE-2026-25741",
			"purpose": "Material Transfer for Manufacture",
			"item_code": "13200473",
			"qty": 27,
			"s_warehouse": "WIP",
			"t_warehouse": "PKG",
			"basic_rate": 0,
			"allow_zero_valuation_rate": 0,
			"batch_no": "B473",
			"posting_date": "2026-09-06",
			"posting_time": "17:18:00",
		}
		out = _classify_with(
			row,
			version=(40378, 1090206),
			batch=40377,
			patient={"voucher_no": "MAT-STE-2026-25741"},
		)
		self.assertEqual(out["confidence"], CONFIDENCE_EXACT)
		self.assertEqual(out["zero_class"], Z1_HISTORICAL_RATE_LOST)
		self.assertTrue(out["eligible"])

	def test_version_only_is_likely(self):
		row = {
			"name": "d1",
			"parent": "SE",
			"purpose": "Material Transfer",
			"item_code": "X",
			"qty": 1,
			"s_warehouse": "W",
			"basic_rate": 0,
			"allow_zero_valuation_rate": 0,
			"posting_date": "2026-01-01",
			"posting_time": "10:00:00",
		}
		out = _classify_with(row, version=(5000, 0), patient={"voucher_no": "SE"})
		self.assertEqual(out["confidence"], "LIKELY")
		self.assertFalse(out["eligible"])

	def test_source_transfer_sle_exact(self):
		row = {
			"name": "d1",
			"parent": "SE-T",
			"purpose": "Material Transfer",
			"item_code": "X",
			"qty": 4,
			"s_warehouse": "W",
			"t_warehouse": "T",
			"basic_rate": 0,
			"allow_zero_valuation_rate": 0,
			"posting_date": "2026-01-01",
			"posting_time": "10:00:00",
		}
		out = _classify_with(row, transfer=5000, patient={"voucher_no": "SE-T"})
		self.assertEqual(out["confidence"], CONFIDENCE_EXACT)
		self.assertEqual(out["source_of_truth"], "source_transfer_sle")
		self.assertTrue(out["eligible"])

	def test_component_scrap_issued_rate(self):
		row = {
			"name": "d1",
			"parent": "SE-MFG",
			"purpose": "Manufacture",
			"item_code": "SCRAP",
			"qty": 1,
			"t_warehouse": "W",
			"basic_rate": 0,
			"is_scrap_item": 1,
			"allow_zero_valuation_rate": 0,
			"posting_date": "2026-01-01",
			"posting_time": "10:00:00",
		}
		mod = "erpnext_extensions.iran_accounting.historical_stock.zero_rate"
		with mock.patch(f"{mod}.is_scrap_row", return_value=True), mock.patch(
			f"{mod}._issued_rate_for_component", return_value=118700
		), mock.patch(f"{mod}.frappe.get_doc", return_value=mock.Mock()), mock.patch(
			f"{mod}._finished_item", return_value="FG"
		), mock.patch(f"{mod}.is_product_reject", return_value=False):
			out = _classify_with(row, patient={"voucher_no": "SE-MFG"})
		self.assertEqual(out["confidence"], CONFIDENCE_EXACT)
		self.assertEqual(out["source_of_truth"], "same_voucher_issued_rate")
		self.assertTrue(out["eligible"])

	def test_dependency_blocks_downstream(self):
		row = {
			"name": "d1",
			"parent": "MAT-STE-2026-25741",
			"purpose": "Material Transfer for Manufacture",
			"item_code": "13200254",
			"qty": 47,
			"s_warehouse": "WIP",
			"basic_rate": 0,
			"allow_zero_valuation_rate": 0,
			"batch_no": "B",
			"posting_date": "2026-09-06",
			"posting_time": "17:18:00",
		}
		out = _classify_with(
			row,
			version=(118700, 5578900),
			batch=118700,
			patient={"voucher_no": "MAT-STE-2026-25407"},
		)
		self.assertEqual(out["status"], STATUS_DEPENDENCY_REPAIR_REQUIRED)
		self.assertFalse(out["eligible"])

	def test_ambiguous_114(self):
		row = {
			"name": "d9",
			"parent": "MAT-STE-2026-25741",
			"purpose": "Material Transfer for Manufacture",
			"item_code": "13200114",
			"qty": 7,
			"s_warehouse": "WIP",
			"basic_rate": 0,
			"allow_zero_valuation_rate": 0,
			"batch_no": "B114",
			"posting_date": "2026-09-06",
			"posting_time": "17:18:00",
		}
		out = _classify_with(
			row,
			version=(1904795, 13333565),
			batch=1899873,
			patient={"voucher_no": "MAT-STE-2026-25407"},
		)
		self.assertEqual(out["confidence"], CONFIDENCE_AMBIGUOUS)
		self.assertFalse(out["eligible"])


class TestNoGlobalRepost(unittest.TestCase):
	def test_repost_module_has_no_repost_all(self):
		from erpnext_extensions.iran_accounting.historical_stock import repost

		self.assertFalse(hasattr(repost, "repost_all"))
		self.assertFalse(hasattr(repost, "repost_all_stock"))
		self.assertIn("never global", (repost.__doc__ or "").lower())


class TestGLClasses(unittest.TestCase):
	def test_g0_balanced_matching(self):
		from erpnext_extensions.iran_accounting.historical_stock.gl_integrity import classify_stock_entry_gl

		se = mock.Mock(
			name="SE",
			docstatus=1,
			company="C",
			posting_date="2026-01-01",
			total_outgoing_value=100,
			total_incoming_value=100,
		)
		gl = [
			mock.Mock(account="A", debit=100, credit=0, cost_center="CC", is_cancelled=0),
			mock.Mock(account="B", debit=0, credit=100, cost_center="CC", is_cancelled=0),
		]
		fake_frappe = mock.Mock()
		fake_frappe.db.get_value.return_value = se
		fake_frappe.db.sql.return_value = gl
		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_stock.gl_integrity.frappe",
			fake_frappe,
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_stock.gl_integrity._voucher_has_poison_sle",
			return_value=False,
		):
			out = classify_stock_entry_gl("SE")
			self.assertEqual(out["gl_class"], G0_HEALTHY)
			self.assertFalse(out["eligible"])

	def test_g1_balanced_wrong_amount(self):
		from erpnext_extensions.iran_accounting.historical_stock.gl_integrity import classify_stock_entry_gl

		se = mock.Mock(
			name="SE",
			docstatus=1,
			total_outgoing_value=45589065,
			total_incoming_value=45589065,
		)
		gl = [
			mock.Mock(account="A", debit=10346959, credit=0, cost_center="CC", is_cancelled=0),
			mock.Mock(account="B", debit=0, credit=10346959, cost_center="CC", is_cancelled=0),
		]
		fake_frappe = mock.Mock()
		fake_frappe.db.get_value.return_value = se
		fake_frappe.db.sql.return_value = gl
		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_stock.gl_integrity.frappe",
			fake_frappe,
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_stock.gl_integrity._voucher_has_poison_sle",
			return_value=False,
		):
			out = classify_stock_entry_gl("SE")
			self.assertEqual(out["gl_class"], G1_ECONOMICALLY_WRONG)

	def test_rebuild_preserves_g0(self):
		from erpnext_extensions.iran_accounting.historical_stock.gl_integrity import rebuild_gl_for_voucher

		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_stock.gl_integrity.classify_stock_entry_gl",
			return_value={"gl_class": G0_HEALTHY, "status": G0_HEALTHY, "voucher": "SE"},
		):
			out = rebuild_gl_for_voucher("SE", dry_run=False)
			self.assertFalse(out["written"])
			self.assertEqual(out["reason"], "G0 preserved")


class TestFailedRIVPolicy(unittest.TestCase):
	def test_poison_is_unsafe_or_waiting(self):
		from erpnext_extensions.iran_accounting.historical_stock.failed_riv import classify_failed_riv

		doc = mock.Mock(
			item_code="X",
			warehouse="W",
			voucher_no="SE",
			error_log="Stock valuation integrity (I1).",
			posting_date="2026-01-01",
		)
		doc.name = "riv1"
		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_stock.failed_riv.classify_identity",
			return_value="POISONED_CHAIN",
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_stock.failed_riv._has_zero_rate_dependency",
			return_value=False,
		):
			out = classify_failed_riv(doc)
			self.assertIn(out["riv_status"], ("WAITING_FOR_SLE_REPAIR", "UNSAFE"))
			self.assertFalse(out["eligible"])

	def test_deadlock_healthy_is_safe_to_retry(self):
		from erpnext_extensions.iran_accounting.historical_stock import RIV_SAFE_TO_RETRY
		from erpnext_extensions.iran_accounting.historical_stock.failed_riv import classify_failed_riv

		doc = mock.Mock(
			item_code="X",
			warehouse="W",
			voucher_no="SE",
			error_log="Deadlock found when trying to get lock",
			posting_date="2026-01-01",
		)
		doc.name = "riv2"
		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_stock.failed_riv.classify_identity",
			return_value="HEALTHY",
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_stock.failed_riv._has_zero_rate_dependency",
			return_value=False,
		):
			out = classify_failed_riv(doc)
			self.assertEqual(out["riv_status"], RIV_SAFE_TO_RETRY)
			self.assertTrue(out["eligible"])


class TestRepairRefusesAmbiguous(unittest.TestCase):
	def test_repair_zero_requires_exact(self):
		from erpnext_extensions.iran_accounting.historical_stock.reconstruct import repair_zero_rate_selected

		fake_frappe = mock.MagicMock()
		fake_frappe.ValidationError = type("ValidationError", (Exception,), {})
		fake_frappe.flags = {}
		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_stock.reconstruct.start_run",
			return_value=None,
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_stock.reconstruct.finish_run",
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_stock.reconstruct._load_detail",
			return_value={"name": "x"},
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_stock.reconstruct.classify_zero_row",
			return_value={
				"confidence": "AMBIGUOUS",
				"eligible": False,
				"voucher": "SE",
				"status": "MANUAL_REVIEW",
			},
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_stock.reconstruct.frappe",
			fake_frappe,
		):
			out = repair_zero_rate_selected([{"voucher_detail": "x"}], dry_run=True)
			self.assertEqual(len(out["blocked"]), 1)
			self.assertEqual(out["applied"], [])


if __name__ == "__main__":
	unittest.main()

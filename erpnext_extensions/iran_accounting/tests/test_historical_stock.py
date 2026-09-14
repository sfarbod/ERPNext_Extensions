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
			self.assertTrue(out["blocked"])
			self.assertIn("G0", out.get("reason") or "")


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


class TestWrongRateContract(unittest.TestCase):
	def test_outgoing_zero_with_svd_is_wrong(self):
		from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import classify_rate_flags

		flags = classify_rate_flags(qty=-1899, outgoing_rate=0, svd=-6330732319)
		self.assertIn("WRONG_OUTGOING_RATE", flags)

	def test_incoming_differs_from_source(self):
		from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import classify_rate_flags

		flags = classify_rate_flags(qty=1899, incoming_rate=3338300, svd=6330732319, expected=3333718.97)
		self.assertIn("WRONG_VS_RECONSTRUCTED_SOURCE", flags)

	def test_basic_vs_zero_valuation(self):
		from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import classify_rate_flags

		flags = classify_rate_flags(qty=1, basic_rate=118700, valuation_rate=0, amount=118700)
		self.assertIn("WRONG_VALUATION_RATE", flags)

	def test_reconstruction_never_uses_bin(self):
		from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import pick_reconstruction

		picked = pick_reconstruction({"bin": 1, "transfer_source": 3333719, "version": 3333719})
		self.assertEqual(picked["confidence"], "EXACT")
		self.assertNotIn("bin", picked["sources_tried"])
		self.assertIn("transfer_source", picked["source"])

	def test_version_alone_is_likely(self):
		from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import pick_reconstruction

		picked = pick_reconstruction({"version": 40378})
		self.assertEqual(picked["confidence"], "LIKELY")

	def test_manual_when_empty(self):
		from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import pick_reconstruction

		picked = pick_reconstruction({})
		self.assertEqual(picked["confidence"], "MANUAL")

	def test_selective_requires_scope(self):
		from erpnext_extensions.iran_accounting.historical_stock.selective import require_scope

		with self.assertRaises(Exception):
			require_scope({})

	def test_disagreeing_sources_are_not_exact(self):
		from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import pick_reconstruction

		picked = pick_reconstruction({"version": 118700, "purchase_receipt": 118650})
		self.assertNotEqual(picked["confidence"], "EXACT")


class TestExpectedRateAnalysis(unittest.TestCase):
	def test_scan_columns_current_expected_difference(self):
		from erpnext_extensions.iran_accounting.historical_stock.expected import attach_rate_analysis

		row = attach_rate_analysis(
			{
				"qty": 10,
				"current_rate": 0,
				"proposed_rate": 118700,
				"proposed_amount": 1187000,
				"eligible": True,
				"confidence": "EXACT",
				"source_of_truth": "version+batch_inward",
				"t_warehouse": "Stores",
				"reconstruction_sources": {"version": 118700, "batch_inward": 118700},
			},
			{"basic_rate": 0, "valuation_rate": 0, "amount": 0, "qty": 10, "t_warehouse": "Stores"},
		)
		self.assertEqual(row["current_basic_rate"], 0)
		self.assertEqual(row["expected_basic_rate"], 118700)
		self.assertEqual(row["difference"], 118700)
		self.assertEqual(row["rate_source"], "version+batch_inward")
		self.assertTrue(row["repair_required"])
		self.assertFalse(row["sources_disagree"])

	def test_preview_shows_alternatives_and_disagreement(self):
		from erpnext_extensions.iran_accounting.historical_stock.expected import preview_reconstruction

		out = preview_reconstruction(
			{
				"current": 0,
				"reconstruction_sources": {"version": 118700, "purchase_receipt": 118650, "batch_inward": 118700},
			}
		)
		self.assertTrue(out["sources_disagree"])
		self.assertNotEqual(out["confidence"], "EXACT")
		self.assertGreaterEqual(len(out["alternative_sources"]), 2)
		self.assertFalse(out["bin_used"])

	def test_bin_never_in_preview(self):
		from erpnext_extensions.iran_accounting.historical_stock.expected import preview_reconstruction

		out = preview_reconstruction({"current": 1, "reconstruction_sources": {"bin": 99, "transfer_source": 50}})
		self.assertFalse(out["bin_used"])
		self.assertEqual(out["chosen_source"], "transfer_source")


class TestImpactAndRollback(unittest.TestCase):
	def test_impact_text_requires_confirmation_and_backup(self):
		from erpnext_extensions.iran_accounting.historical_stock.impact import format_impact

		text = format_impact(
			{
				"repairing": ["MAT-STE-2026-25407"],
				"stock_entries": 12,
				"sle": 51,
				"sabb": 17,
				"sbe": 17,
				"bin": 3,
				"gl": 5,
				"failed_riv": 2,
				"estimated_replay_seconds": 14,
				"estimated_sql_updates": 93,
				"estimated_replay_depth": 4,
				"replay_chain": ["25407", "25715", "25716", "25741", "25912", "25911"],
				"full_rollback_possible": False,
			}
		)
		self.assertIn("DATABASE BACKUP REQUIRED", text)
		self.assertIn("25407", text)
		self.assertIn("51 SLE", text)
		self.assertIn("Nothing executes before operator confirmation", text)

	def test_zero_sql_updates_disables_repair_in_impact_text(self):
		from erpnext_extensions.iran_accounting.historical_stock.impact import format_impact

		text = format_impact(
			{
				"repairing": ["MAT-STE-2026-26156"],
				"stock_entries": 2,
				"sle": 5,
				"sabb": 5,
				"sbe": 5,
				"bin": 1,
				"gl": 4,
				"failed_riv": 7,
				"estimated_replay_seconds": 1,
				"estimated_sql_updates": 0,
				"estimated_replay_depth": 0,
				"replay_chain": ["MAT-STE-2026-26135-1"],
				"aborted": True,
				"abort_reasons": [
					{"voucher": "MAT-STE-2026-26156", "reason": "VALUATION_POISON_DEPENDENCY: 30300014 (negative_incoming_rate)"}
				],
				"skip_reason": "VALUATION_POISON_DEPENDENCY: 30300014 (negative_incoming_rate)",
				"full_rollback_possible": True,
			}
		)
		self.assertIn("SQL updates planned: 0. Repair Selected is disabled.", text)
		self.assertIn("VALUATION_POISON_DEPENDENCY", text)
		self.assertIn("ABORTED", text)

	def test_wrong_rate_abort_does_not_write_sle(self):
		from erpnext_extensions.iran_accounting.historical_stock import reconstruct as rec

		row = {
			"surface": "SLE",
			"voucher_detail": "detail-1",
			"voucher": "MAT-STE-2026-03129",
			"item": "16700278",
			"eligible": True,
			"confidence": "EXACT",
		}
		aborted = {
			"aborted": True,
			"applied": [],
			"blocked": [{"error": "DEPENDENCY_REPAIR_REQUIRED: patient-zero is MAT-RECO-2026-02761"}],
			"sql_updates_executed": 0,
			"skip_reason": "DEPENDENCY_REPAIR_REQUIRED: patient-zero is MAT-RECO-2026-02761",
		}
		with mock.patch.object(rec, "repair_zero_rate_selected", return_value=aborted):
			with mock.patch.object(rec, "write_sle_transaction_rates", create=True) as write:
				out = rec.repair_wrong_rate_selected([row], dry_run=False)
		self.assertTrue(out["aborted"])
		self.assertEqual(out["sql_updates_executed"], 0)
		write.assert_not_called()

	def test_truncated_snapshot_refuses_write_rollback(self):
		from erpnext_extensions.iran_accounting.historical_stock.snapshot import restore_snapshot

		out = restore_snapshot({"truncated": True, "full_rollback_possible": False}, dry_run=True)
		self.assertFalse(out["full_rollback_possible"])
		self.assertIn("DATABASE BACKUP REQUIRED", out["warning"])

	def test_jsonable_accepts_attr_dict_rows(self):
		from erpnext_extensions.iran_accounting.historical_stock.snapshot import _jsonable

		class AttrDict(dict):
			def __getattr__(self, key):
				return self.get(key)

		rows = _jsonable([AttrDict({"name": "sed-1", "basic_rate": 1.5})])
		self.assertEqual(rows[0]["name"], "sed-1")
		self.assertEqual(rows[0]["basic_rate"], 1.5)


class TestPermissionsAndExport(unittest.TestCase):
	def test_unprivileged_user_gets_permission_message(self):
		from erpnext_extensions.iran_accounting.historical_stock import permissions as perm

		err = type("PermissionError", (Exception,), {})
		fake = mock.Mock()
		fake.session.user = "employee@x"
		fake.get_roles.return_value = ["Employee"]
		fake.PermissionError = err
		fake.throw.side_effect = lambda msg, exc=None: (_ for _ in ()).throw(err(msg))
		with mock.patch("erpnext_extensions.iran_accounting.historical_stock.permissions.frappe", fake):
			with self.assertRaises(err) as raised:
				perm.access_level()
		self.assertIn("Not permitted", str(raised.exception))

	def test_stock_manager_is_read_only(self):
		from erpnext_extensions.iran_accounting.historical_stock import permissions as perm

		err = type("PermissionError", (Exception,), {})
		fake = mock.Mock()
		fake.session.user = "stock@x"
		fake.get_roles.return_value = ["Stock Manager"]
		fake.PermissionError = err
		fake.throw.side_effect = lambda msg, exc=None: (_ for _ in ()).throw(err(msg))
		with mock.patch("erpnext_extensions.iran_accounting.historical_stock.permissions.frappe", fake):
			self.assertEqual(perm.access_level(), 1)
			with self.assertRaises(err):
				perm.require_repair()

	def test_system_manager_can_repair_not_admin(self):
		from erpnext_extensions.iran_accounting.historical_stock import permissions as perm

		err = type("PermissionError", (Exception,), {})
		fake = mock.Mock()
		fake.session.user = "sm@x"
		fake.get_roles.return_value = ["System Manager"]
		fake.PermissionError = err
		fake.throw.side_effect = lambda msg, exc=None: (_ for _ in ()).throw(err(msg))
		with mock.patch("erpnext_extensions.iran_accounting.historical_stock.permissions.frappe", fake):
			self.assertEqual(perm.access_level(), 2)
			self.assertEqual(perm.require_repair(), 2)
			with self.assertRaises(err):
				perm.require_admin()

	def test_administrator_is_level_three(self):
		from erpnext_extensions.iran_accounting.historical_stock import permissions as perm

		fake = mock.Mock()
		fake.session.user = "Administrator"
		fake.get_roles.return_value = ["System Manager", "Stock Manager"]
		with mock.patch("erpnext_extensions.iran_accounting.historical_stock.permissions.frappe", fake):
			self.assertEqual(perm.access_level(), 3)

	def test_xlsx_is_zip(self):
		from erpnext_extensions.iran_accounting.historical_stock.export import xlsx_bytes

		data = xlsx_bytes(["A", "B"], [["1", "2"]])
		self.assertTrue(data.startswith(b"PK"))
		self.assertGreater(len(data), 40)

	def test_repair_selected_is_operational_not_experimental(self):
		from erpnext_extensions.iran_accounting.historical_stock.permissions import FEATURE_MATURITY

		self.assertEqual(FEATURE_MATURITY["scan"], "A")
		self.assertEqual(FEATURE_MATURITY["repair_selected"], "B")
		self.assertEqual(FEATURE_MATURITY["resume"], "C")
		self.assertEqual(FEATURE_MATURITY["advanced_mode"], "C")
		self.assertEqual(FEATURE_MATURITY["rollback"], "C")
		self.assertEqual(FEATURE_MATURITY["benchmark"], "C")

	def test_system_manager_session_hides_experimental(self):
		from erpnext_extensions.iran_accounting.historical_stock import permissions as perm

		fake = mock.Mock()
		fake.session.user = "sm@x"
		fake.get_roles.return_value = ["System Manager"]
		with mock.patch("erpnext_extensions.iran_accounting.historical_stock.permissions.frappe", fake):
			info = perm.session_info()
		self.assertTrue(info["visible"]["repair"])
		self.assertFalse(info["visible"]["resume"])
		self.assertFalse(info["visible"]["advanced"])
		self.assertFalse(info["visible"]["rollback"])

	def test_repost_without_identity_is_not_global(self):
		from erpnext_extensions.iran_accounting.historical_stock.repost import _resolve_repost_identity

		item, warehouse, meta = _resolve_repost_identity(company="X", from_date="2026-01-01")
		self.assertIsNone(item)
		self.assertIsNone(warehouse)
		self.assertEqual(meta["identity_count"], 0)

	def test_graph_without_identity_does_not_throw(self):
		from erpnext_extensions.iran_accounting.historical_stock.graph import repair_graph

		out = repair_graph()
		self.assertEqual(out["nodes"], [])
		self.assertIn("requires", (out.get("warning") or "").lower())

	def test_impact_chain_uses_voucher_when_item_missing(self):
		from erpnext_extensions.iran_accounting.historical_stock.impact import _chain_for

		chain = _chain_for([{"voucher": "MAT-STE-2026-27531", "eligible": True, "confidence": "EXACT"}])
		self.assertIn("MAT-STE-2026-27531", chain)


class TestRepairPlanner(unittest.TestCase):
	"""Scan, Impact, and Apply must share one eligibility evaluator."""

	def _poison_pair(self):
		return {
			"inbound_document": "MAT-STE-2026-26135-1",
			"outbound_document": "MAT-STE-2026-26156",
			"item": "30300014",
			"warehouse": "انبار Quarantine محصول نیمه ساخته اسپاد",
			"confidence": CONFIDENCE_EXACT,
			"eligible": True,
			"status": "ELIGIBLE",
			"optimizer_status": "CROSS_TIME_REPAIRABLE",
			"current_inbound_time": "2026-09-01 10:00:00",
			"current_outbound_time": "2026-09-01 09:00:00",
			"proposed_outbound_time": "2026-09-01 10:00:01",
			"min_qty_before": -10,
			"min_qty_after": 0,
			"moves": [
				{
					"document": "MAT-STE-2026-26156",
					"old": "2026-09-01 09:00:00",
					"new": "2026-09-01 10:00:01",
					"seconds": 1,
				}
			],
		}

	def test_poison_scan_impact_apply_never_diverge(self):
		from erpnext_extensions.iran_accounting.historical_stock.impact import plan_repair_impact
		from erpnext_extensions.iran_accounting.historical_stock.planner import (
			PLAN_WAITING_RATE_REPAIR,
			assert_ready,
			attach_plan,
			evaluate_row,
			plan_selection,
		)

		row = self._poison_pair()
		hit = {
			"reason": "negative_incoming_rate",
			"voucher": "MAT-STE-2026-25469",
			"sle": "MAT-SLE-2026-166217",
			"incoming_rate": -9997892,
			"inversion_artifact": False,
		}

		def _load(voucher, cache=None, hint=None):
			return {
				"voucher": voucher,
				"item": row["item"],
				"warehouse": row["warehouse"],
				"status": "MANUAL_REVIEW",
				"confidence": "LIKELY",
				"topic": "WRONG_RATE",
			}

		with mock.patch(
			"erpnext_extensions.iran_accounting.stock_posting_order.replay.window_poison_hit",
			return_value=hit,
		), mock.patch(
			"erpnext_extensions.iran_accounting.stock_posting_order.repair._voucher_item_warehouses",
			return_value={(row["item"], row["warehouse"])},
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_stock.dependency.load_blocker_row",
			side_effect=_load,
		):
			decision = evaluate_row(row)
			stamped = attach_plan(row)
			impact = plan_selection([row])
			via_impact_api = plan_repair_impact([row])

		self.assertFalse(decision["eligible"])
		self.assertTrue(decision["blocked"])
		self.assertEqual(decision["planner_status"], PLAN_WAITING_RATE_REPAIR)
		self.assertEqual(decision["sql_updates"], 0)
		self.assertIn("negative_incoming_rate", decision["reason"])
		self.assertIn("MAT-STE-2026-25469", decision["reason"])
		self.assertIn("unrelated voucher", decision["reason"])
		self.assertFalse(stamped["eligible"])
		self.assertEqual(stamped["planner_status"], PLAN_WAITING_RATE_REPAIR)
		self.assertEqual(stamped["immediate_blocker"], "MAT-STE-2026-25469")
		self.assertNotEqual(stamped["required_action"], "BLOCKED")
		self.assertEqual(stamped["sql_updates"], 0)
		self.assertFalse(impact["executable"])
		self.assertEqual(impact["estimated_sql_updates"], 0)
		self.assertIn("MAT-STE-2026-25469", impact["skip_reason"] or "")
		self.assertEqual(impact["immediate_blocker"], "MAT-STE-2026-25469")
		self.assertEqual(via_impact_api["estimated_sql_updates"], 0)
		self.assertFalse(via_impact_api["executable"])

		def _throw(msg, *args, **kwargs):
			raise RuntimeError(msg)

		with mock.patch(
			"erpnext_extensions.iran_accounting.stock_posting_order.replay.window_poison_hit",
			return_value=hit,
		), mock.patch(
			"erpnext_extensions.iran_accounting.stock_posting_order.repair._voucher_item_warehouses",
			return_value={(row["item"], row["warehouse"])},
		), mock.patch("frappe.throw", side_effect=_throw):
			with self.assertRaises(RuntimeError) as ctx:
				assert_ready(row)
		self.assertIn("MAT-STE-2026-25469", str(ctx.exception))
		self.assertIn("negative_incoming_rate", str(ctx.exception))

	def test_wrong_rate_sle_waits_on_patient_zero(self):
		from erpnext_extensions.iran_accounting.historical_stock.planner import (
			PLAN_WAITING_PATIENT_ZERO,
			evaluate_row,
			plan_selection,
		)

		row = {
			"topic": "WRONG_RATE",
			"surface": "SLE",
			"voucher": "MAT-STE-2026-03129",
			"item": "16700278",
			"warehouse": "Stores",
			"eligible": True,
			"confidence": CONFIDENCE_EXACT,
			"status": "RECONSTRUCTABLE",
		}
		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_stock.planner._lookup_patient",
			return_value="MAT-RECO-2026-02761",
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_stock.planner._rate_poison_reason",
			return_value=None,
		):
			decision = evaluate_row(row)
			impact = plan_selection([row])
		self.assertEqual(decision["planner_status"], PLAN_WAITING_PATIENT_ZERO)
		self.assertFalse(decision["eligible"])
		self.assertEqual(decision["sql_updates"], 0)
		self.assertEqual(decision["patient_zero"], "MAT-RECO-2026-02761")
		self.assertEqual(decision["required_prerequisite"], "MAT-RECO-2026-02761")
		self.assertFalse(impact["executable"])
		self.assertEqual(impact["estimated_sql_updates"], 0)

	def test_gl_g1_is_ready_not_manual(self):
		from erpnext_extensions.iran_accounting.historical_stock.planner import PLAN_READY, evaluate_row

		row = {
			"topic": "GL",
			"gl_class": G1_ECONOMICALLY_WRONG,
			"voucher": "MAT-STE-1",
			"confidence": "LIKELY",
			"status": G1_ECONOMICALLY_WRONG,
		}
		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_stock.planner._gl_has_poison",
			return_value=False,
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_stock.planner._gl_row_count",
			return_value=4,
		):
			decision = evaluate_row(row)
		self.assertEqual(decision["planner_status"], PLAN_READY)
		self.assertTrue(decision["eligible"])
		self.assertEqual(decision["sql_updates"], 4)

	def test_ready_posting_reports_sql(self):
		from erpnext_extensions.iran_accounting.historical_stock.planner import PLAN_READY, evaluate_row

		row = self._poison_pair()
		counts = {"se": 2, "sle": 10, "sabb": 4, "sbe": 4, "bin": 1, "sql": 21}
		with mock.patch(
			"erpnext_extensions.iran_accounting.stock_posting_order.replay.window_poison_hit",
			return_value=None,
		), mock.patch(
			"erpnext_extensions.iran_accounting.stock_posting_order.repair._voucher_item_warehouses",
			return_value={(row["item"], row["warehouse"])},
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_stock.planner._posting_revalidate",
			return_value=None,
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_stock.planner._write_counts",
			return_value=counts,
		):
			decision = evaluate_row(row)
		self.assertEqual(decision["planner_status"], PLAN_READY)
		self.assertTrue(decision["eligible"])
		self.assertEqual(decision["sql_updates"], 21)
		self.assertEqual(decision["replay_count"], 1)


class TestDependencyResolution(unittest.TestCase):
	"""One walker. Scan / Impact / Planner / Repair must share the tree."""

	def _pair(self):
		return {
			"outbound_document": "MAT-STE-2026-26156",
			"inbound_document": "MAT-STE-2026-26135-1",
			"item": "30300014",
			"warehouse": "Q",
			"batch": "5861-30300014-SO262014T321",
			"work_order": "MFG-WO-2026-00578",
			"current_inbound_time": "2026-04-15 18:03:46",
			"current_outbound_time": "2026-04-15 18:01:10",
		}

	def _decision(self, status, **kw):
		from erpnext_extensions.iran_accounting.historical_stock.planner import empty_decision

		d = empty_decision(planner_status=status)
		d.update(kw)
		return d

	def _eval(self, mapping):
		from erpnext_extensions.iran_accounting.historical_stock.dependency import voucher_of

		def fake(row, cache=None):
			return dict(mapping[voucher_of(row)])

		return fake

	def _load(self, mapping):
		def fake(voucher, cache=None, hint=None):
			return dict(mapping[voucher])

		return fake

	def test_single_dependency(self):
		from erpnext_extensions.iran_accounting.historical_stock.dependency import resolve_dependencies
		from erpnext_extensions.iran_accounting.historical_stock.planner import PLAN_READY, PLAN_WAITING_RATE_REPAIR

		row = self._pair()
		evals = {
			"MAT-STE-2026-26156": self._decision(
				PLAN_WAITING_RATE_REPAIR,
				required_prerequisite="MAT-STE-2026-25469",
				dependency="negative_incoming_rate",
				reason="negative_incoming_rate on MAT-STE-2026-25469",
			),
			"MAT-STE-2026-25469": self._decision(PLAN_READY, eligible=True, blocked=False, sql_updates=4, reason="READY"),
		}
		loads = {"MAT-STE-2026-25469": {"voucher": "MAT-STE-2026-25469"}}
		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_stock.planner.evaluate_row",
			side_effect=self._eval(evals),
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_stock.dependency.load_blocker_row",
			side_effect=self._load(loads),
		):
			res = resolve_dependencies(row)
		self.assertEqual(res["immediate_blocker"], "MAT-STE-2026-25469")
		self.assertEqual(res["root_blocker"], "MAT-STE-2026-25469")
		self.assertEqual(res["root_status"], PLAN_READY)
		self.assertEqual(res["repair_order"], ["MAT-STE-2026-25469", "MAT-STE-2026-26156"])
		self.assertEqual(res["dependency_depth"], 1)
		self.assertFalse(res["no_repair_path"])
		self.assertEqual(res["required_action"], "Repair MAT-STE-2026-25469 first")

	def test_multi_level_dependency(self):
		from erpnext_extensions.iran_accounting.historical_stock.dependency import resolve_dependencies
		from erpnext_extensions.iran_accounting.historical_stock.planner import (
			PLAN_READY,
			PLAN_WAITING_PATIENT_ZERO,
			PLAN_WAITING_RATE_REPAIR,
		)

		row = self._pair()
		evals = {
			"MAT-STE-2026-26156": self._decision(
				PLAN_WAITING_RATE_REPAIR,
				required_prerequisite="MAT-STE-2026-25469",
				dependency="negative_incoming_rate",
				reason="negative_incoming_rate on MAT-STE-2026-25469",
			),
			"MAT-STE-2026-25469": self._decision(
				PLAN_WAITING_PATIENT_ZERO,
				required_prerequisite="MAT-STE-2026-25791",
				patient_zero="MAT-STE-2026-25791",
				dependency="qty_after_zero_nonzero_value",
			),
			"MAT-STE-2026-25791": self._decision(PLAN_READY, eligible=True, blocked=False, sql_updates=3),
		}
		loads = {
			"MAT-STE-2026-25469": {"voucher": "MAT-STE-2026-25469"},
			"MAT-STE-2026-25791": {"voucher": "MAT-STE-2026-25791"},
		}
		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_stock.planner.evaluate_row",
			side_effect=self._eval(evals),
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_stock.dependency.load_blocker_row",
			side_effect=self._load(loads),
		):
			res = resolve_dependencies(row)
		self.assertEqual(res["repair_order"], ["MAT-STE-2026-25791", "MAT-STE-2026-25469", "MAT-STE-2026-26156"])
		self.assertEqual(res["root_blocker"], "MAT-STE-2026-25791")
		self.assertEqual(res["dependency_depth"], 2)
		self.assertIn("waits for MAT-STE-2026-25469", res["tree_text"])
		self.assertIn("waits for MAT-STE-2026-25791", res["tree_text"])

	def test_patient_zero_detection(self):
		from erpnext_extensions.iran_accounting.historical_stock.dependency import resolve_dependencies
		from erpnext_extensions.iran_accounting.historical_stock.planner import PLAN_WAITING_PATIENT_ZERO, PLAN_READY

		row = {"voucher": "MAT-STE-2026-25469", "item": "30300014", "warehouse": "Q"}
		evals = {
			"MAT-STE-2026-25469": self._decision(
				PLAN_WAITING_PATIENT_ZERO,
				required_prerequisite="MAT-STE-2026-25791",
				patient_zero="MAT-STE-2026-25791",
			),
			"MAT-STE-2026-25791": self._decision(PLAN_READY, eligible=True, blocked=False, sql_updates=2),
		}
		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_stock.planner.evaluate_row",
			side_effect=self._eval(evals),
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_stock.dependency.load_blocker_row",
			side_effect=self._load({"MAT-STE-2026-25791": {"voucher": "MAT-STE-2026-25791"}}),
		):
			res = resolve_dependencies(row)
		self.assertEqual(res["root_blocker"], "MAT-STE-2026-25791")
		self.assertEqual(res["required_action"], "Repair MAT-STE-2026-25791 first")

	def test_circular_dependency_detection(self):
		from erpnext_extensions.iran_accounting.historical_stock.dependency import PLAN_NO_REPAIR_PATH, resolve_dependencies
		from erpnext_extensions.iran_accounting.historical_stock.planner import PLAN_WAITING_RATE_REPAIR

		row = {"voucher": "A"}
		evals = {
			"A": self._decision(PLAN_WAITING_RATE_REPAIR, required_prerequisite="B", reason="waits B"),
			"B": self._decision(PLAN_WAITING_RATE_REPAIR, required_prerequisite="A", reason="waits A"),
		}
		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_stock.planner.evaluate_row",
			side_effect=self._eval(evals),
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_stock.dependency.load_blocker_row",
			side_effect=self._load({"B": {"voucher": "B"}, "A": {"voucher": "A"}}),
		):
			res = resolve_dependencies(row)
		self.assertTrue(res["circular"])
		self.assertTrue(res["no_repair_path"])
		self.assertIn("circular", res["required_action"].lower())
		self.assertEqual(res["stop_reason"], "Circular dependency")

	def test_dependency_tree_rendering(self):
		from erpnext_extensions.iran_accounting.historical_stock.dependency import format_tree

		tree = {
			"voucher": "MAT-STE-2026-26156",
			"status": "WAITING_RATE_REPAIR",
			"children": [
				{
					"voucher": "MAT-STE-2026-25469",
					"status": "READY",
					"blocked_because": "negative_incoming_rate",
					"children": [],
				}
			],
		}
		text = format_tree(tree, 2)
		self.assertIn("MAT-STE-2026-26156", text)
		self.assertIn("waits for MAT-STE-2026-25469", text)
		self.assertIn("estimated chain:", text)
		self.assertIn("2 vouchers", text)

	def test_go_to_root_cause_fields(self):
		from erpnext_extensions.iran_accounting.historical_stock.planner import PLAN_READY, PLAN_WAITING_RATE_REPAIR, attach_plan

		row = self._pair()
		evals = {
			"MAT-STE-2026-26156": self._decision(
				PLAN_WAITING_RATE_REPAIR, required_prerequisite="MAT-STE-2026-25469", sql_updates=0
			),
			"MAT-STE-2026-25469": self._decision(PLAN_READY, eligible=True, blocked=False, sql_updates=4),
		}
		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_stock.planner.evaluate_row",
			side_effect=self._eval(evals),
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_stock.dependency.load_blocker_row",
			side_effect=self._load({"MAT-STE-2026-25469": {"voucher": "MAT-STE-2026-25469"}}),
		):
			stamped = attach_plan(row)
		self.assertEqual(stamped["root_blocker"], "MAT-STE-2026-25469")
		self.assertEqual(stamped["root_patient_zero"], "MAT-STE-2026-25469")
		self.assertTrue(stamped["repair_order_list"])
		self.assertEqual(stamped["repair_order_list"][0], "MAT-STE-2026-25469")

	def test_repair_dependency_chain_root_only(self):
		from erpnext_extensions.iran_accounting.historical_stock.dependency import apply_dependency_chain
		from erpnext_extensions.iran_accounting.historical_stock.planner import PLAN_READY, PLAN_WAITING_RATE_REPAIR

		row = self._pair()
		evals = {
			"MAT-STE-2026-26156": self._decision(
				PLAN_WAITING_RATE_REPAIR, required_prerequisite="MAT-STE-2026-25469", sql_updates=0
			),
			"MAT-STE-2026-25469": self._decision(PLAN_READY, eligible=True, blocked=False, sql_updates=4, reason="READY"),
		}
		root_row = {"voucher": "MAT-STE-2026-25469", "planner_status": PLAN_READY, "sql_updates": 4, "eligible": True}

		def fake_attach(r, cache=None):
			return {**r, "planner_status": PLAN_READY, "sql_updates": 4, "eligible": True, "reason": "READY"}

		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_stock.planner.evaluate_row",
			side_effect=self._eval(evals),
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_stock.dependency.load_blocker_row",
			side_effect=self._load({"MAT-STE-2026-25469": root_row}),
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_stock.planner.attach_plan",
			side_effect=fake_attach,
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_stock.dependency._dispatch_root_repair",
			return_value={"applied": [{"voucher": "MAT-STE-2026-25469"}], "sql_updates_executed": 4},
		) as dispatch:
			dry = apply_dependency_chain(row, dry_run=True)
			wrote = apply_dependency_chain(row, dry_run=False)
		self.assertTrue(dry["executable"])
		self.assertEqual(dry["repairing"], ["MAT-STE-2026-25469"])
		self.assertEqual(dispatch.call_count, 1)
		self.assertEqual(wrote["repaired_root"], "MAT-STE-2026-25469")
		self.assertIn("MAT-STE-2026-26156", wrote["skipped_descendants"])

	def test_batch_scoped_repair_candidate_is_not_proven(self):
		from erpnext_extensions.iran_accounting.historical_stock.dependency import (
			PLAN_READY_BATCH_SCOPED,
			analyze_batch_isolation,
		)

		row = self._pair()
		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_stock.dependency._window_batches",
			return_value=[row["batch"]],
		):
			out = analyze_batch_isolation(row)
		self.assertFalse(out["can_isolate_batch"])
		self.assertTrue(out["would_affect_unrelated_batches"])
		self.assertTrue(out["would_corrupt_gl_or_ma"])
		self.assertEqual(out["classification"], "WAREHOUSE_WIDE_POISON")
		self.assertNotEqual(out["classification"], PLAN_READY_BATCH_SCOPED)

	def test_warehouse_wide_poison(self):
		from erpnext_extensions.iran_accounting.historical_stock.dependency import analyze_batch_isolation
		from erpnext_extensions.iran_accounting.historical_stock.planner import PLAN_WAITING_RATE_REPAIR, attach_plan

		row = self._pair()
		hit = {
			"reason": "negative_incoming_rate",
			"voucher": "MAT-STE-2026-25469",
			"sle": "X",
			"incoming_rate": -1,
			"inversion_artifact": False,
		}
		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_stock.dependency._window_batches",
			return_value=[row["batch"], "9312-other"],
		):
			iso = analyze_batch_isolation(row)
		self.assertEqual(iso["classification"], "WAREHOUSE_WIDE_POISON")
		self.assertIn("unrelated", iso["detail"].lower())

		with mock.patch(
			"erpnext_extensions.iran_accounting.stock_posting_order.replay.window_poison_hit",
			return_value=hit,
		), mock.patch(
			"erpnext_extensions.iran_accounting.stock_posting_order.repair._voucher_item_warehouses",
			return_value={(row["item"], row["warehouse"])},
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_stock.dependency.load_blocker_row",
			return_value={"voucher": "MAT-STE-2026-25469", "confidence": "MANUAL", "status": "MANUAL_REVIEW"},
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_stock.dependency._window_batches",
			return_value=[row["batch"], "9312-other"],
		):
			stamped = attach_plan(
				{
					**row,
					"confidence": "EXACT",
					"eligible": True,
					"status": "ELIGIBLE",
					"optimizer_status": "CROSS_TIME_REPAIRABLE",
					"proposed_outbound_time": "2026-04-15 18:03:47",
					"min_qty_before": -10,
					"min_qty_after": 0,
					"moves": [{"document": "MAT-STE-2026-26156", "old": "a", "new": "b", "seconds": 1}],
				}
			)
		self.assertEqual(stamped["planner_status"], PLAN_WAITING_RATE_REPAIR)
		self.assertNotEqual(stamped["planner_status"], "READY_BATCH_SCOPED_REPAIR")
		self.assertEqual(stamped["batch_isolation"]["classification"], "WAREHOUSE_WIDE_POISON")

	def test_no_repair_path(self):
		from erpnext_extensions.iran_accounting.historical_stock.dependency import resolve_dependencies
		from erpnext_extensions.iran_accounting.historical_stock.planner import PLAN_AMBIGUOUS, PLAN_WAITING_RATE_REPAIR

		row = {"voucher": "MAT-STE-2026-26156"}
		evals = {
			"MAT-STE-2026-26156": self._decision(
				PLAN_WAITING_RATE_REPAIR, required_prerequisite="MAT-STE-2026-25469"
			),
			"MAT-STE-2026-25469": self._decision(PLAN_AMBIGUOUS, reason="AMBIGUOUS"),
		}
		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_stock.planner.evaluate_row",
			side_effect=self._eval(evals),
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_stock.dependency.load_blocker_row",
			side_effect=self._load({"MAT-STE-2026-25469": {"voucher": "MAT-STE-2026-25469"}}),
		):
			res = resolve_dependencies(row)
		self.assertTrue(res["no_repair_path"])
		self.assertEqual(res["stop_reason"], "Ambiguous")
		self.assertIn("NO REPAIR PATH", res["required_action"])
		self.assertNotEqual(res["required_action"], "BLOCKED")

	def test_scan_impact_share_tree(self):
		from erpnext_extensions.iran_accounting.historical_stock.impact import plan_repair_impact
		from erpnext_extensions.iran_accounting.historical_stock.planner import (
			PLAN_READY,
			PLAN_WAITING_RATE_REPAIR,
			attach_plan,
			plan_selection,
		)

		row = self._pair()
		evals = {
			"MAT-STE-2026-26156": self._decision(
				PLAN_WAITING_RATE_REPAIR, required_prerequisite="MAT-STE-2026-25469", sql_updates=0
			),
			"MAT-STE-2026-25469": self._decision(PLAN_READY, eligible=True, blocked=False, sql_updates=4),
		}
		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_stock.planner.evaluate_row",
			side_effect=self._eval(evals),
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_stock.dependency.load_blocker_row",
			side_effect=self._load({"MAT-STE-2026-25469": {"voucher": "MAT-STE-2026-25469"}}),
		):
			scan = attach_plan(row)
			impact = plan_selection([row])
			via = plan_repair_impact([row])
		self.assertEqual(scan["repair_order_list"], impact["repair_order"])
		self.assertEqual(impact["repair_order"], via["repair_order"])
		self.assertEqual(scan["tree_text"], impact["tree_text"])
		self.assertEqual(impact["tree_text"], via["tree_text"])

	def test_farvardin_regression_leftover_is_inversion_artifact(self):
		from erpnext_extensions.iran_accounting.stock_posting_order.replay import (
			INVERSION_ARTIFACT_POISONS,
			sle_poison_reason,
		)

		reason = sle_poison_reason(
			{
				"actual_qty": -10,
				"qty_after_transaction": 0,
				"stock_value": 5000,
				"stock_value_difference": -5000,
				"incoming_rate": 0,
				"valuation_rate": 0,
			}
		)
		self.assertEqual(reason, "qty_after_zero_nonzero_value")
		self.assertIn(reason, INVERSION_ARTIFACT_POISONS)
		self.assertNotIn("negative_incoming_rate", INVERSION_ARTIFACT_POISONS)


def _scope_sle(name, voucher, qty, dt, *, batch="", purpose="", incoming=0, outgoing=0, svd=0, after=0, value=0, rate=0, wo=""):
	from datetime import datetime

	if isinstance(dt, str):
		dt = datetime.fromisoformat(dt)
	return {
		"name": name,
		"voucher_no": voucher,
		"actual_qty": qty,
		"posting_datetime": dt,
		"creation": dt,
		"batch": batch,
		"purpose": purpose,
		"incoming_rate": incoming,
		"outgoing_rate": outgoing,
		"stock_value_difference": svd,
		"qty_after_transaction": after,
		"stock_value": value,
		"valuation_rate": rate,
		"work_order": wo,
	}


class TestMinimalScope(unittest.TestCase):
	INB = "MAT-STE-IN"
	OUT = "MAT-STE-OUT"
	BATCH = "B1"
	OTHER = "B2"
	WO = "WO-1"

	def _row(self, **kw):
		from datetime import datetime

		base = {
			"inbound_document": self.INB,
			"outbound_document": self.OUT,
			"item": "I",
			"warehouse": "W",
			"batch": self.BATCH,
			"work_order": self.WO,
			"current_outbound_time": datetime(2026, 4, 15, 18, 1, 10),
			"current_inbound_time": datetime(2026, 4, 15, 18, 3, 46),
			"proposed_outbound_time": datetime(2026, 4, 15, 18, 3, 47),
			"moves": [{"document": self.OUT, "new": datetime(2026, 4, 15, 18, 3, 47)}],
		}
		base.update(kw)
		return base

	def test_batch_local_inversion_unrelated_poison_does_not_block(self):
		from datetime import datetime
		from erpnext_extensions.iran_accounting.historical_stock.scope import (
			PLAN_READY_BATCH,
			POISON_UNRELATED,
			analyze_scope,
		)

		sles = [
			_scope_sle("1", self.OUT, -10, "2026-04-15T18:01:10", batch=self.BATCH, purpose="Material Transfer for Manufacture", outgoing=10, svd=-100, after=-10, value=-100),
			_scope_sle("2", self.INB, 10, "2026-04-15T18:03:46", batch=self.BATCH, purpose="Manufacture", incoming=10, svd=100, after=0, value=0),
			_scope_sle("3", "POISON", 5, "2026-09-01T10:00:00", batch=self.OTHER, purpose="Manufacture", incoming=-50, svd=-250, after=5, value=-250, wo="OTHER-WO"),
		]
		row = self._row()
		times = {self.OUT: datetime(2026, 4, 15, 18, 3, 47)}
		out = analyze_scope(row, sles, times, opening_qty=10, opening_value=100)
		self.assertTrue(out["batch_qty_ok"])
		unrelated = [p for p in out["unrelated_poison"] if p["voucher"] == "POISON"]
		self.assertEqual(unrelated[0]["class"], POISON_UNRELATED)
		self.assertEqual(unrelated[0]["effect"], "NONE")
		self.assertEqual(out["status"], PLAN_READY_BATCH)
		self.assertFalse(out["escalation_required"])

	def test_batch_local_repair_succeeds_without_touching_unrelated_lot(self):
		from datetime import datetime
		from erpnext_extensions.iran_accounting.historical_stock.scope import PLAN_READY_BATCH, analyze_scope

		sles = [
			_scope_sle("1", self.OUT, -10, "2026-04-15T18:01:10", batch=self.BATCH, purpose="Material Transfer for Manufacture", outgoing=10, svd=-100),
			_scope_sle("2", self.INB, 10, "2026-04-15T18:03:46", batch=self.BATCH, purpose="Manufacture", incoming=10, svd=100),
			_scope_sle("3", "LATER", -4, "2026-04-16T12:00:00", batch=self.OTHER, purpose="Material Transfer for Manufacture", outgoing=10, svd=-40),
		]
		out = analyze_scope(
			self._row(),
			sles,
			{self.OUT: datetime(2026, 4, 15, 18, 3, 47)},
			opening_qty=10,
			opening_value=100,
		)
		self.assertEqual(out["status"], PLAN_READY_BATCH)
		self.assertEqual(out["other_batch_rewrite_count"], 0)

	def test_batch_local_escalates_when_warehouse_ma_dependency_exists(self):
		from datetime import datetime
		from erpnext_extensions.iran_accounting.historical_stock.scope import (
			EDGE_WAREHOUSE_MA,
			PLAN_WAREHOUSE_ESCALATION,
			analyze_scope,
		)

		sles = [
			_scope_sle("1", self.OUT, -10, "2026-04-15T18:01:10", batch=self.BATCH, purpose="Material Transfer for Manufacture", outgoing=100, svd=-1000),
			_scope_sle("x", "OTHER-OUT", -5, "2026-04-15T18:02:00", batch=self.OTHER, purpose="Material Transfer for Manufacture", outgoing=50, svd=-250, wo=self.WO),
			_scope_sle("2", self.INB, 10, "2026-04-15T18:03:46", batch=self.BATCH, purpose="Manufacture", incoming=80, svd=800),
		]
		out = analyze_scope(self._row(), sles, {self.OUT: datetime(2026, 4, 15, 18, 3, 47)})
		self.assertEqual(out["status"], PLAN_WAREHOUSE_ESCALATION)
		self.assertTrue(out["escalation_required"])
		self.assertGreater(out["other_batch_rewrite_count"], 0)
		self.assertTrue(any(e["type"] == EDGE_WAREHOUSE_MA for e in out["edges"]))
		self.assertIn("WAREHOUSE_MA", out["escalation_reason"] or "")

	def test_unrelated_warehouse_poison_does_not_automatically_block(self):
		from datetime import datetime
		from erpnext_extensions.iran_accounting.historical_stock.scope import PLAN_READY_BATCH, analyze_scope

		sles = [
			_scope_sle("1", self.OUT, -8, "2026-04-15T18:01:10", batch=self.BATCH, purpose="Material Transfer for Manufacture", outgoing=8, svd=-64),
			_scope_sle("2", self.INB, 8, "2026-04-15T18:03:46", batch=self.BATCH, purpose="Manufacture", incoming=8, svd=64),
			_scope_sle("p", "25469", 2, "2026-09-06T10:00:00", batch="9312", purpose="Manufacture", incoming=-99, svd=-198, wo="WO-OTHER"),
		]
		out = analyze_scope(
			self._row(),
			sles,
			{self.OUT: datetime(2026, 4, 15, 18, 3, 47)},
			opening_qty=8,
			opening_value=64,
		)
		self.assertEqual(out["status"], PLAN_READY_BATCH)
		self.assertTrue(all(p["effect"] == "NONE" for p in out["unrelated_poison"]))

	def test_local_poison_does_block(self):
		from datetime import datetime
		from erpnext_extensions.iran_accounting.historical_stock.scope import POISON_LOCAL, analyze_scope

		sles = [
			_scope_sle("1", self.OUT, -8, "2026-04-15T18:01:10", batch=self.BATCH, purpose="Material Transfer for Manufacture", outgoing=8, svd=-64),
			_scope_sle("2", self.INB, 8, "2026-04-15T18:03:46", batch=self.BATCH, purpose="Manufacture", incoming=-12, svd=-96),
		]
		out = analyze_scope(self._row(), sles, {self.OUT: datetime(2026, 4, 15, 18, 3, 47)})
		self.assertTrue(out["local_poisons"])
		self.assertEqual(out["local_poisons"][0]["class"], POISON_LOCAL)
		self.assertEqual(out["status"], "WAITING_RATE_REPAIR")

	def test_dependency_edge_types(self):
		from datetime import datetime
		from erpnext_extensions.iran_accounting.historical_stock.scope import EDGE_BATCH, EDGE_DOCUMENT, analyze_scope

		row = self._row(downstream_vouchers=["MAT-STE-DOWN"])
		sles = [
			_scope_sle("1", self.OUT, -3, "2026-04-15T18:01:10", batch=self.BATCH, purpose="Material Transfer for Manufacture", outgoing=3, svd=-9),
			_scope_sle("2", self.INB, 3, "2026-04-15T18:03:46", batch=self.BATCH, purpose="Manufacture", incoming=3, svd=9),
		]
		out = analyze_scope(row, sles, {self.OUT: datetime(2026, 4, 15, 18, 3, 47)})
		types = {e["type"] for e in out["edges"]}
		self.assertIn(EDGE_BATCH, types)
		self.assertIn(EDGE_DOCUMENT, types)

	def test_two_node_cycle(self):
		from datetime import datetime
		from erpnext_extensions.iran_accounting.historical_stock.scope import PLAN_INVALID_GRAPH, analyze_scope, _cyclic

		self.assertTrue(_cyclic([{"from": "A", "to": "B", "type": "X"}, {"from": "B", "to": "A", "type": "Y"}]))
		self.assertFalse(_cyclic([{"from": "A", "to": "B", "type": "X"}, {"from": "B", "to": "C", "type": "Y"}]))
		row = self._row(downstream_vouchers=[self.INB])
		sles = [
			_scope_sle("1", self.OUT, -2, "2026-04-15T18:01:10", batch=self.BATCH, purpose="Material Transfer for Manufacture", outgoing=2, svd=-4),
			_scope_sle("2", self.INB, 2, "2026-04-15T18:03:46", batch=self.BATCH, purpose="Manufacture", incoming=2, svd=4),
		]
		out = analyze_scope(row, sles, {self.OUT: datetime(2026, 4, 15, 18, 3, 47)})
		self.assertTrue(out["cyclic"])
		self.assertEqual(out["status"], PLAN_INVALID_GRAPH)

	def test_multi_node_cycle(self):
		from datetime import datetime
		from erpnext_extensions.iran_accounting.historical_stock.scope import PLAN_INVALID_GRAPH, analyze_scope, _cyclic

		self.assertTrue(
			_cyclic(
				[
					{"from": "A", "to": "B", "type": "X"},
					{"from": "B", "to": "C", "type": "X"},
					{"from": "C", "to": "A", "type": "X"},
				]
			)
		)
		row = self._row(downstream_vouchers=[self.OUT, self.INB])
		sles = [
			_scope_sle("1", self.OUT, -2, "2026-04-15T18:01:10", batch=self.BATCH, purpose="Material Transfer for Manufacture", outgoing=2, svd=-4),
			_scope_sle("2", self.INB, 2, "2026-04-15T18:03:46", batch=self.BATCH, purpose="Manufacture", incoming=2, svd=4),
		]
		out = analyze_scope(row, sles, {self.OUT: datetime(2026, 4, 15, 18, 3, 47)})
		self.assertTrue(out["cyclic"])
		self.assertEqual(out["status"], PLAN_INVALID_GRAPH)

	def test_patient_zero_resolution_is_not_warehouse_overreach(self):
		from datetime import datetime
		from erpnext_extensions.iran_accounting.historical_stock.scope import analyze_scope

		sles = [
			_scope_sle("1", self.OUT, -6, "2026-04-15T18:01:10", batch=self.BATCH, purpose="Material Transfer for Manufacture", outgoing=6, svd=-36),
			_scope_sle("2", self.INB, 6, "2026-04-15T18:03:46", batch=self.BATCH, purpose="Manufacture", incoming=6, svd=36),
			_scope_sle("pz", "25791", -1, "2026-04-05T18:00:15", batch="OTHERPZ", purpose="Material Transfer for Manufacture", outgoing=6, svd=-6, after=0, value=50),
		]
		# 25791 is before the window start in real data; if present later as leftover it must not be a BATCH edge
		out = analyze_scope(self._row(), sles, {self.OUT: datetime(2026, 4, 15, 18, 3, 47)})
		self.assertFalse(any(e["to"] == "25791" and e["type"] == "PATIENT_ZERO_DEPENDENCY" for e in out["edges"]))

	def test_minimal_scope_selection_prefers_batch(self):
		from datetime import datetime
		from erpnext_extensions.iran_accounting.historical_stock.scope import SCOPE_BATCH, analyze_scope

		sles = [
			_scope_sle("1", self.OUT, -1, "2026-04-15T18:01:10", batch=self.BATCH, purpose="Material Transfer for Manufacture", outgoing=1, svd=-1),
			_scope_sle("2", self.INB, 1, "2026-04-15T18:03:46", batch=self.BATCH, purpose="Manufacture", incoming=1, svd=1),
		]
		out = analyze_scope(self._row(), sles, {self.OUT: datetime(2026, 4, 15, 18, 3, 47)})
		self.assertEqual(out["smallest_safe_scope"], SCOPE_BATCH)

	def test_rescan_after_root_repair_chain_is_root_only(self):
		from erpnext_extensions.iran_accounting.historical_stock.dependency import apply_dependency_chain
		from erpnext_extensions.iran_accounting.historical_stock.planner import PLAN_READY

		row = self._row()
		evals = {
			self.OUT: {"planner_status": "WAITING_RATE_REPAIR", "required_prerequisite": self.INB, "sql_updates": 0, "eligible": False, "blocked": True, "reason": "wait"},
			self.INB: {"planner_status": PLAN_READY, "eligible": True, "blocked": False, "sql_updates": 3, "reason": "READY"},
		}

		def fake_eval(r, cache=None):
			from erpnext_extensions.iran_accounting.historical_stock.dependency import voucher_of
			from erpnext_extensions.iran_accounting.historical_stock.planner import empty_decision

			d = empty_decision()
			d.update(evals[voucher_of(r)])
			return d

		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_stock.planner.evaluate_row",
			side_effect=fake_eval,
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_stock.dependency.load_blocker_row",
			return_value={"voucher": self.INB},
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_stock.planner.attach_plan",
			side_effect=lambda r, cache=None: {**r, "planner_status": PLAN_READY, "sql_updates": 3, "eligible": True},
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_stock.dependency._dispatch_root_repair",
			return_value={"applied": [{"voucher": self.INB}], "sql_updates_executed": 3},
		) as dispatch:
			wrote = apply_dependency_chain({"voucher": self.OUT, "inbound_document": self.INB, "outbound_document": self.OUT}, dry_run=False)
		self.assertEqual(dispatch.call_count, 1)
		self.assertEqual(wrote.get("repaired_root"), self.INB)

	def test_unrelated_poison_is_not_walked_on_loaded_scope(self):
		from erpnext_extensions.iran_accounting.historical_stock.dependency import resolve_dependencies
		from erpnext_extensions.iran_accounting.historical_stock.planner import empty_decision
		from erpnext_extensions.iran_accounting.historical_stock.scope import PLAN_WAREHOUSE_ESCALATION, SCOPE_WAREHOUSE

		row = self._row()
		scope = {
			"window_loaded": True,
			"status": PLAN_WAREHOUSE_ESCALATION,
			"smallest_safe_scope": SCOPE_WAREHOUSE,
			"escalation_reason": "WAREHOUSE_MA_DEPENDENCY: OTHER-OUT (batch B2)",
			"reason": "WAREHOUSE_MA_DEPENDENCY: OTHER-OUT (batch B2)",
			"cyclic": False,
			"repair_order": [self.INB, self.OUT, "MAT-STE-DOWN"],
			"edges": [
				{"from": self.INB, "to": self.OUT, "type": "BATCH_DEPENDENCY", "why": "same batch"},
				{"from": self.OUT, "to": "OTHER-OUT", "type": "WAREHOUSE_MA_DEPENDENCY", "why": "interleaved"},
			],
			"unrelated_poison": [
				{"voucher": "MAT-STE-2026-25469", "batch": "9312", "effect": "NONE", "class": "UNRELATED_WAREHOUSE_POISON"}
			],
			"local_poisons": [],
			"other_batch_rewrite_count": 1,
			"pair_end_value_equal": False,
			"dependency_type": "BATCH_DEPENDENCY",
		}
		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_stock.scope.evaluate_minimal_scope",
			return_value=scope,
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_stock.planner.evaluate_row",
			return_value=empty_decision(planner_status=PLAN_WAREHOUSE_ESCALATION, reason=scope["reason"]),
		):
			res = resolve_dependencies(row)
		self.assertEqual(res["status"], PLAN_WAREHOUSE_ESCALATION)
		self.assertNotIn("MAT-STE-2026-25469", res["repair_order"])
		self.assertFalse(res["circular"])
		self.assertEqual(res["unrelated_poison"][0]["effect"], "NONE")
		self.assertIn("WAREHOUSE_MA", res["escalation_reason"])


if __name__ == "__main__":
	unittest.main()

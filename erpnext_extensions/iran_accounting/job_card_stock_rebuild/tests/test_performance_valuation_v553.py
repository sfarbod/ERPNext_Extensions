# Copyright (c) 2026, ERPNext Extensions contributors
"""PF01–PF04: bounded sync valuation + Dry Run timing capture (v5.5.3)."""

from __future__ import annotations

import time

import frappe
from frappe.tests.utils import FrappeTestCase

from erpnext_extensions.iran_accounting.job_card_stock_rebuild.api import (
	scan_manufacture_reconciliation,
)
from erpnext_extensions.iran_accounting.job_card_stock_rebuild.atomic_repair import (
	dry_run_manufacture_repair,
)
from erpnext_extensions.iran_accounting.job_card_stock_rebuild import sync_valuation as sv_mod


JC = "PO-JOB08760"
MFG = "MAT-STE-2026-31724-1"


def _plan_from_scan():
	scan = scan_manufacture_reconciliation(JC)
	plan = scan["plan"]
	dispositions = [
		{
			"item_code": r["item_code"],
			"batch_no": r.get("batch_no") or "",
			"proposed_consumed": r.get("proposed_consumed") or 0,
			"proposed_scrap": r.get("proposed_scrap") or 0,
			"proposed_return": r.get("proposed_return") or 0,
			"proposed_still_in_wip": r.get("proposed_still_in_wip") or 0,
			"disposition": r.get("suggested_action"),
		}
		for r in scan["scan"]["rows"]
	]
	return {
		"dispositions": dispositions,
		"merge_documents": plan.get("merge_documents") or [],
		"stamp_mode": plan.get("stamp_mode") or "HISTORICAL",
		"fingerprint": plan.get("fingerprint"),
	}


class TestPerformanceValuationV553(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		if not frappe.db.exists("Job Card", JC):
			self.skipTest(f"missing {JC}")
		if not frappe.db.exists("Stock Entry", MFG):
			self.skipTest(f"missing {MFG}")

	def tearDown(self):
		frappe.db.rollback()

	def _run_scoped_valuation_with_stub(self):
		calls = []

		class _Fake:
			reposted_dependant_item_wh = {}

		import erpnext.stock.stock_ledger as sl

		orig = sl.update_entries_after

		def _fake_update(args, allow_negative_stock=False):
			calls.append((args.get("item_code"), args.get("warehouse")))
			return _Fake()

		# Preserve Core method attrs used by _scope_dependant_repost.
		_fake_update.include_dependant_sle_in_reposting = getattr(
			orig, "include_dependant_sle_in_reposting", lambda *a, **k: None
		)
		sl.update_entries_after = _fake_update
		try:
			val = sv_mod.sync_valuation_for_vouchers([MFG])
		finally:
			sl.update_entries_after = orig
		return val, calls

	def test_pf01_bounded_valuation_roots(self):
		"""PF01: valuation roots are limited to repair voucher Item×Warehouse pairs."""
		val, calls = self._run_scoped_valuation_with_stub()
		self.assertTrue(val.get("ok"), val.get("error"))
		self.assertLessEqual(len(calls), 80)
		voucher_pairs = {
			(r.item_code, r.warehouse)
			for r in frappe.db.sql(
				"""
				select distinct item_code, warehouse from `tabStock Ledger Entry`
				where voucher_no=%s and ifnull(is_cancelled,0)=0
				  and item_code is not null and warehouse is not null
				""",
				MFG,
				as_dict=1,
			)
		}
		self.assertEqual(val.get("dependant_scope_pairs"), len(voucher_pairs))
		for pair in calls:
			self.assertIn(pair, voucher_pairs)

	def test_pf02_no_duplicate_valuation_root_processing(self):
		"""PF02: same Item×Warehouse root is not processed twice."""
		val, calls = self._run_scoped_valuation_with_stub()
		self.assertTrue(val.get("ok"), val.get("error"))
		self.assertEqual(len(calls), len(set(calls)))

	def test_pf03_po_job08760_dry_run_timing_captured(self):
		"""PF03: capture Dry Run timing for operator decision (not a hard SLA assert)."""
		plan_in = _plan_from_scan()
		t0 = time.perf_counter()
		result = dry_run_manufacture_repair(JC, plan=plan_in)
		elapsed = round(time.perf_counter() - t0, 2)
		self.assertEqual(result.get("status"), "DRY_RUN_PASS")
		phases = (result.get("phase_timings") or {}).get("phases") or []
		t17 = next((p for p in phases if p.get("phase") == "T17"), {})
		frappe.logger().info(
			f"PF03 Dry Run total={elapsed}s T17={t17.get('elapsed')} "
			f"pairs={t17.get('pairs')} future={t17.get('future_sle_total')}"
		)
		self.assertGreater(elapsed, 0)
		self.assertIn("elapsed", t17)

	def test_pf04_dry_run_zero_persistent_mutation(self):
		"""PF04: Dry Run leaves zero persistent business mutation."""
		before = {
			"se1": frappe.db.count("Stock Entry", {"docstatus": 1}),
			"sle": frappe.db.sql("select count(*) from `tabStock Ledger Entry`")[0][0],
			"gl": frappe.db.sql("select count(*) from `tabGL Entry`")[0][0],
			"a": frappe.db.get_value("Stock Entry", "MAT-STE-2026-31725", "docstatus"),
			"b": frappe.db.get_value("Stock Entry", "MAT-STE-2026-31726", "docstatus"),
		}
		result = dry_run_manufacture_repair(JC, plan=_plan_from_scan())
		self.assertEqual(result.get("status"), "DRY_RUN_PASS")
		self.assertFalse(result.get("mutated"))
		self.assertFalse(result.get("committed"))
		after = {
			"se1": frappe.db.count("Stock Entry", {"docstatus": 1}),
			"sle": frappe.db.sql("select count(*) from `tabStock Ledger Entry`")[0][0],
			"gl": frappe.db.sql("select count(*) from `tabGL Entry`")[0][0],
			"a": frappe.db.get_value("Stock Entry", "MAT-STE-2026-31725", "docstatus"),
			"b": frappe.db.get_value("Stock Entry", "MAT-STE-2026-31726", "docstatus"),
		}
		self.assertEqual(before, after)

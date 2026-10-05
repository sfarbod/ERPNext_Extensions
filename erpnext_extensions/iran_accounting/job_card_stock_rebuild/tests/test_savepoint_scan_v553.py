# Copyright (c) 2026, ERPNext Extensions contributors
"""SP01–SP10: shared-logistics Scan cancel-probe transaction safety (v5.5.3)."""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import cint

from erpnext_extensions.iran_accounting.job_card_stock_rebuild.api import (
	scan_manufacture_reconciliation,
)
from erpnext_extensions.iran_accounting.job_card_stock_rebuild.shared_logistics import (
	simulate_cancel_stock_ok,
)


JC = "PO-JOB08760"
ITEM = "13200544"
SE_A = "MAT-STE-2026-31725"  # historically needs bridge
SE_B = "MAT-STE-2026-31726"  # alone recreatable


def _counts():
	return {
		"se_submitted": cint(
			frappe.db.count("Stock Entry", {"docstatus": 1})
		),
		"sle": cint(frappe.db.sql("select count(*) from `tabStock Ledger Entry`")[0][0]),
		"gl": cint(frappe.db.sql("select count(*) from `tabGL Entry`")[0][0]),
		"bin": cint(frappe.db.sql("select count(*) from `tabBin`")[0][0]),
		"batch": cint(frappe.db.sql("select count(*) from `tabBatch`")[0][0]),
		"se_a": cint(frappe.db.get_value("Stock Entry", SE_A, "docstatus")),
		"se_b": cint(frappe.db.get_value("Stock Entry", SE_B, "docstatus")),
	}


class TestSavepointScanV553(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		frappe.flags.jc_shared_cancel_probe_cache = {}
		if not frappe.db.exists("Job Card", JC):
			self.skipTest(f"missing fixture {JC}")
		if cint(frappe.db.get_value("Stock Entry", SE_A, "docstatus")) != 1:
			self.skipTest(f"{SE_A} not submitted — need pre-repair fixture")

	def tearDown(self):
		frappe.db.rollback()
		frappe.flags.jc_shared_cancel_probe_cache = {}

	def test_sp01_cancel_simulation_keeps_transaction_boundary(self):
		"""SP01: nested full rollback must not destroy the probe savepoint."""
		from erpnext.stock.doctype.stock_entry.stock_entry import StockEntry

		orig = StockEntry.cancel

		def _cancel_with_nested_full_rollback(self):
			try:
				return orig(self)
			except Exception:
				frappe.db.rollback()  # would destroy SAVEPOINT without ownership guard
				raise

		StockEntry.cancel = _cancel_with_nested_full_rollback
		try:
			ok, reason = simulate_cancel_stock_ok([SE_A])
			self.assertFalse(ok)
			self.assertNotIn("1305", reason)
			self.assertNotIn("does not exist", reason.lower())
		finally:
			StockEntry.cancel = orig

	def test_sp02_successful_probe_zero_mutation(self):
		"""SP02: successful cancel probe leaves zero persistent mutation."""
		before = _counts()
		ok, reason = simulate_cancel_stock_ok([SE_B])
		self.assertTrue(ok, reason)
		self.assertEqual(_counts(), before)

	def test_sp03_failed_probe_zero_mutation(self):
		"""SP03: failed cancel probe leaves zero persistent mutation."""
		before = _counts()
		ok, reason = simulate_cancel_stock_ok([SE_A])
		self.assertFalse(ok, reason)
		self.assertEqual(_counts(), before)

	def test_sp04_exception_inside_cancel_zero_mutation(self):
		"""SP04: exception inside cancellation leaves zero persistent mutation."""
		from erpnext.stock.doctype.stock_entry.stock_entry import StockEntry

		before = _counts()
		orig = StockEntry.cancel

		def _boom(self):
			raise RuntimeError("forced probe exception")

		StockEntry.cancel = _boom
		try:
			ok, reason = simulate_cancel_stock_ok([SE_B])
			self.assertFalse(ok)
			self.assertIn("forced probe exception", reason)
		finally:
			StockEntry.cancel = orig
		self.assertEqual(_counts(), before)

	def test_sp05_nested_rollback_behavior_safe(self):
		"""SP05: nested full rollback during probe is demoted safely."""
		before = _counts()
		from erpnext.stock.doctype.stock_entry.stock_entry import StockEntry

		orig = StockEntry.cancel

		def _cancel_rb(self):
			try:
				return orig(self)
			except Exception:
				frappe.db.rollback()
				raise

		StockEntry.cancel = _cancel_rb
		try:
			ok, reason = simulate_cancel_stock_ok([SE_A])
			self.assertFalse(ok)
			self.assertNotIn("SAVEPOINT", reason)
		finally:
			StockEntry.cancel = orig
		self.assertEqual(_counts(), before)

	def test_sp06_no_operational_error_1305(self):
		"""SP06: probe must not raise OperationalError 1305."""
		for names in ([SE_A], [SE_B], [SE_B, SE_A]):
			frappe.flags.jc_shared_cancel_probe_cache = {}
			try:
				simulate_cancel_stock_ok(names)
			except Exception as exc:
				self.fail(f"probe raised for {names}: {exc}")

	def test_sp07_scan_po_job08760_completes(self):
		"""SP07: Scan PO-JOB08760 completes."""
		result = scan_manufacture_reconciliation(JC)
		self.assertIn("scan", result)
		row = next(r for r in result["scan"]["rows"] if r["item_code"] == ITEM)
		self.assertEqual(float(row["remaining_wip"]), 1148.0)
		self.assertEqual(row["suggested_action"], "CONSUMED")
		self.assertEqual(float(row["suggested_qty"]), 1148.0)

	def test_sp08_repeated_scan_deterministic(self):
		"""SP08: repeated Scan is deterministic."""
		a = scan_manufacture_reconciliation(JC)
		b = scan_manufacture_reconciliation(JC)
		ra = next(r for r in a["scan"]["rows"] if r["item_code"] == ITEM)
		rb = next(r for r in b["scan"]["rows"] if r["item_code"] == ITEM)
		for k in (
			"issued",
			"returned",
			"consumed",
			"remaining_wip",
			"suggested_action",
			"suggested_qty",
			"proposed_consumed",
			"confidence",
		):
			self.assertEqual(ra.get(k), rb.get(k), k)

	def test_sp09_submitted_stock_entry_count_unchanged(self):
		"""SP09: submitted Stock Entry count unchanged after Scan."""
		before = cint(frappe.db.count("Stock Entry", {"docstatus": 1}))
		scan_manufacture_reconciliation(JC)
		after = cint(frappe.db.count("Stock Entry", {"docstatus": 1}))
		self.assertEqual(before, after)

	def test_sp10_sle_gl_bin_batch_unchanged(self):
		"""SP10: SLE/GL/Bin/Batch unchanged after Scan."""
		before = _counts()
		scan_manufacture_reconciliation(JC)
		self.assertEqual(_counts(), before)

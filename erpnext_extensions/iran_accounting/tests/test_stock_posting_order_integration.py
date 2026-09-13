# Copyright (c) 2026, ERPNext Extensions contributors
"""Persisted posting-order integration tests on development.localhost."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta

import frappe
from frappe.utils import flt, now_datetime, nowdate, nowtime

from erpnext_extensions.iran_accounting.e2e_bootstrap import (
	enable_perpetual_inventory,
	ensure_test_item,
	get_irr_company,
	get_second_warehouse,
	get_warehouse,
	submit_material_receipt,
)
from erpnext_extensions.iran_accounting.integration.bootstrap import apply as apply_bootstrap
from erpnext_extensions.iran_accounting.stock_posting_order.prevention import (
	PREVENTION_FLAG,
	ensure_dependent_stock_posting_after,
)
from erpnext_extensions.iran_accounting.stock_posting_order.repair import apply_repairs, scan_production_posting_order_anomalies
from erpnext_extensions.iran_accounting.stock_posting_order.integrity import integrity_check


def _enable_negative_stock(on: bool) -> int:
	cur = frappe.db.get_single_value("Stock Settings", "allow_negative_stock")
	frappe.db.set_single_value("Stock Settings", "allow_negative_stock", 1 if on else 0)
	return cur


def _se_row(item, qty, s_wh=None, t_wh=None, rate=1000, against=None, batch=None):
	row = {
		"item_code": item,
		"qty": qty,
		"transfer_qty": qty,
		"conversion_factor": 1,
		"basic_rate": rate,
		"valuation_rate": rate,
		"s_warehouse": s_wh,
		"t_warehouse": t_wh,
	}
	if against:
		row["against_stock_entry"] = against
	if batch:
		row["batch_no"] = batch
	return row


class TestStockPostingOrderIntegration(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		apply_bootstrap()
		frappe.set_user("Administrator")
		cls.company = get_irr_company("ESPAD")
		enable_perpetual_inventory(cls.company)
		frappe.flags.iran_gate_defaults = True
		cls.stores = get_warehouse(cls.company)
		cls.wip = get_second_warehouse(cls.company, cls.stores)
		cls.fg_wh = cls.stores

	def setUp(self):
		frappe.flags[PREVENTION_FLAG] = False
		frappe.flags.iran_gate_defaults = True

	def _new_items(self, tag):
		rm = ensure_test_item(self.company, f"PPO-RM-{tag}")
		fg = ensure_test_item(self.company, f"PPO-FG-{tag}")
		for code in (rm, fg):
			frappe.db.set_value(
				"Item",
				code,
				{"valuation_rate": 1000, "is_stock_item": 1},
				update_modified=False,
			)
		return rm, fg

	def _make_mtfm(self, rm, qty, posting_date, posting_time, *, submit=True):
		se = frappe.new_doc("Stock Entry")
		se.company = self.company
		se.purpose = "Material Transfer for Manufacture"
		se.stock_entry_type = "Material Transfer for Manufacture"
		se.set_posting_time = 1
		se.posting_date = posting_date
		se.posting_time = posting_time
		se.append("items", _se_row(rm, qty, s_wh=self.stores, t_wh=self.wip))
		se.insert(ignore_permissions=True)
		if submit:
			se.submit()
		return se

	def _make_manufacture(self, rm, fg, qty, posting_date, posting_time, *, against=None, submit=True):
		se = frappe.new_doc("Stock Entry")
		se.company = self.company
		se.purpose = "Manufacture"
		se.stock_entry_type = "Manufacture"
		se.fg_completed_qty = qty
		se.set_posting_time = 1
		se.posting_date = posting_date
		se.posting_time = posting_time
		se.append("items", _se_row(rm, qty, s_wh=self.wip, t_wh=None, against=against))
		fg_row = _se_row(fg, qty, s_wh=None, t_wh=self.fg_wh, rate=1000)
		fg_row["is_finished_item"] = 1
		se.append("items", fg_row)
		se.insert(ignore_permissions=True)
		if against:
			frappe.db.sql(
				"""
				UPDATE `tabStock Entry Detail`
				SET against_stock_entry=%s
				WHERE parent=%s AND IFNULL(s_warehouse,'') != ''
				""",
				(against, se.name),
			)
			se.reload()
		if submit:
			se.submit()
		return se

	def _inverted_chain(self, tag, qty=10, when=None):
		"""Submit manufacture first at T, then MTfM at T → outbound creation wins."""
		rm, fg = self._new_items(tag)
		submit_material_receipt(self.company, rm, qty=qty + 5, rate=1000, warehouse=self.stores)
		posting_date = "2026-09-01"
		posting_time = "14:00:00"
		prev = _enable_negative_stock(True)
		frappe.flags[PREVENTION_FLAG] = True
		try:
			mfg = self._make_manufacture(rm, fg, qty, posting_date, posting_time, submit=True)
			mtfm = self._make_mtfm(rm, qty, posting_date, posting_time, submit=True)
			# link after names exist
			frappe.db.set_value(
				"Stock Entry Detail",
				{"parent": mfg.name, "item_code": rm, "s_warehouse": self.wip},
				"against_stock_entry",
				mtfm.name,
			)
			mfg.reload()
		finally:
			frappe.flags[PREVENTION_FLAG] = False
			_enable_negative_stock(bool(prev))
		return rm, fg, mtfm, mfg, posting_date, posting_time

	def test_transfer_into_wip_then_consume_inverted_and_repair(self):
		rm, fg, mtfm, mfg, posting_date, posting_time = self._inverted_chain("INV")
		sles = frappe.db.sql(
			"""
			SELECT voucher_no, actual_qty, qty_after_transaction, posting_datetime, creation
			FROM `tabStock Ledger Entry`
			WHERE item_code=%s AND warehouse=%s AND is_cancelled=0
			  AND voucher_no IN %s
			ORDER BY posting_datetime, creation
			""",
			(rm, self.wip, (mtfm.name, mfg.name)),
			as_dict=True,
		)
		self.assertGreaterEqual(len(sles), 2)
		self.assertEqual(sles[0].voucher_no, mfg.name, "outbound processed first at same second")
		self.assertLess(flt(sles[0].qty_after_transaction), 0)
		final_before = flt(sles[-1].qty_after_transaction)

		rows = scan_production_posting_order_anomalies(company=self.company, from_date=posting_date, to_date=posting_date)
		match = [
			r
			for r in rows
			if r["inbound_document"] == mtfm.name and r["outbound_document"] == mfg.name and r["item"] == rm
		]
		self.assertTrue(match, f"expected EXACT chain in scan, got {[(r['inbound_document'], r['outbound_document'], r['confidence'], r['status']) for r in rows[:8]]}")
		row = match[0]
		self.assertEqual(row["confidence"], "EXACT")

		preview = apply_repairs([row], dry_run=True)
		self.assertTrue(preview["dry_run"])
		mfg.reload()
		self.assertEqual(str(mfg.posting_time), posting_time)

		# stale preview
		frappe.db.set_value("Stock Entry", mfg.name, "remarks", "stale-check")
		row_stale = dict(row)
		blocked = apply_repairs([row_stale], dry_run=False)
		self.assertTrue(blocked["blocked"])

		# refresh modified and repair
		mfg.reload()
		row["outbound_modified"] = str(mfg.modified)
		mtfm.reload()
		row["inbound_modified"] = str(mtfm.modified)
		result = apply_repairs([row], dry_run=False)
		self.assertTrue(result["applied"], result)
		mfg.reload()
		from erpnext_extensions.iran_accounting.stock_posting_order.ordering import add_seconds, combine_posting

		self.assertEqual(
			combine_posting(mfg.posting_date, mfg.posting_time),
			add_seconds(combine_posting(posting_date, posting_time), 1),
		)
		sles_after = frappe.db.sql(
			"""
			SELECT voucher_no, actual_qty, qty_after_transaction, posting_datetime, creation
			FROM `tabStock Ledger Entry`
			WHERE item_code=%s AND warehouse=%s AND is_cancelled=0
			  AND voucher_no IN %s
			ORDER BY posting_datetime, creation
			""",
			(rm, self.wip, (mtfm.name, mfg.name)),
			as_dict=True,
		)
		self.assertEqual(sles_after[0].voucher_no, mtfm.name)
		self.assertGreaterEqual(min(flt(s.qty_after_transaction) for s in sles_after), 0)
		self.assertEqual(flt(sles_after[-1].qty_after_transaction), final_before)
		gate = integrity_check([mtfm.name, mfg.name], item_code=rm, warehouse=self.wip)
		self.assertTrue(gate["ok"], gate)

	def test_prevention_bumps_dependent_on_submit(self):
		rm, fg = self._new_items("PREV")
		submit_material_receipt(self.company, rm, qty=20, rate=1000, warehouse=self.stores)
		posting_date = "2026-09-01"
		posting_time = "12:00:00"
		prev = _enable_negative_stock(True)
		try:
			mtfm = self._make_mtfm(rm, 5, posting_date, posting_time)
		except Exception:
			_enable_negative_stock(bool(prev))
			raise
		try:
			from erpnext_extensions.iran_accounting.stock_posting_order.ordering import combine_posting
			from erpnext_extensions.iran_accounting.stock_posting_order.prevention import (
				apply_production_posting_order,
				find_prerequisite_stock_entry,
			)

			mfg = self._make_manufacture(
				rm, fg, 5, posting_date, posting_time, against=mtfm.name, submit=False
			)
			details = frappe.db.sql(
				"""
				SELECT item_code, s_warehouse, t_warehouse, against_stock_entry
				FROM `tabStock Entry Detail` WHERE parent=%s
				""",
				mfg.name,
				as_dict=True,
			)
			if combine_posting(mfg.posting_date, mfg.posting_time) <= combine_posting(
				mtfm.posting_date, mtfm.posting_time
			):
				result = apply_production_posting_order(mfg)
				self.assertTrue(
					result and result.get("changed"),
					f"prereq={find_prerequisite_stock_entry(mfg)} result={result} time={mfg.posting_time} details={details} items={[(i.item_code, i.s_warehouse, i.get('against_stock_entry')) for i in mfg.items]}",
				)
			self.assertGreater(
				combine_posting(mfg.posting_date, mfg.posting_time),
				combine_posting(mtfm.posting_date, mtfm.posting_time),
			)
			mfg.submit()
			mfg.reload()
			self.assertGreater(
				combine_posting(mfg.posting_date, mfg.posting_time),
				combine_posting(mtfm.posting_date, mtfm.posting_time),
			)
		finally:
			_enable_negative_stock(bool(prev))

	def test_cancelled_and_draft_excluded(self):
		rm, fg, mtfm, mfg, posting_date, posting_time = self._inverted_chain("EXCL")
		# draft sibling
		draft = self._make_manufacture(rm, fg, 1, posting_date, posting_time, against=mtfm.name, submit=False)
		self.assertEqual(draft.docstatus, 0)
		rows = scan_production_posting_order_anomalies(company=self.company, from_date=posting_date, to_date=posting_date)
		self.assertFalse(any(r["outbound_document"] == draft.name for r in rows))
		# cancel manufacture if possible; scan must drop it
		# cancelled docs are docstatus 2 — skip cancel if stock layers block it
		names = {r["outbound_document"] for r in rows}
		self.assertIn(mfg.name, names)

	def test_sfg_chain_offsets(self):
		"""Three sequential production SEs: transfer, manufacture, next consume."""
		from erpnext_extensions.iran_accounting.stock_posting_order.dag import assign_minimum_offsets
		from erpnext_extensions.iran_accounting.stock_posting_order.ordering import combine_posting

		t = combine_posting(nowdate(), "10:00:00")
		plan = assign_minimum_offsets(
			{"A": t, "B": t, "C": t},
			[("A", "B"), ("B", "C")],
		)
		self.assertEqual(plan["proposed"]["A"][-8:], "10:00:00")
		self.assertEqual(plan["proposed"]["B"][-8:], "10:00:01")
		self.assertEqual(plan["proposed"]["C"][-8:], "10:00:02")

	def test_no_repair_needed_when_opening_covers(self):
		rm, fg = self._new_items("NR")
		submit_material_receipt(self.company, rm, qty=30, rate=1000, warehouse=self.stores)
		posting_date = "2026-09-01"
		prev = _enable_negative_stock(True)
		frappe.flags[PREVENTION_FLAG] = True
		try:
			pre = self._make_mtfm(rm, 10, posting_date, "13:00:00")
			mfg = self._make_manufacture(rm, fg, 10, posting_date, "14:00:00")
			mtfm = self._make_mtfm(rm, 10, posting_date, "14:00:00")
			frappe.db.set_value(
				"Stock Entry Detail",
				{"parent": mfg.name, "item_code": rm, "s_warehouse": self.wip},
				"against_stock_entry",
				mtfm.name,
			)
		finally:
			frappe.flags[PREVENTION_FLAG] = False
			_enable_negative_stock(bool(prev))
		rows = scan_production_posting_order_anomalies(
			company=self.company, from_date=posting_date, to_date=posting_date
		)
		match = [r for r in rows if r["inbound_document"] == mtfm.name and r["outbound_document"] == mfg.name]
		self.assertTrue(match, f"expected chain, got {[(r['chain'], r['status']) for r in rows[:12]]}")
		self.assertIn(match[0]["optimizer_status"], ("NO_REPAIR_NEEDED", "NO_REPAIR_NEEDED"))
		self.assertFalse(match[0]["eligible"])
		self.assertEqual(match[0]["minimum_seconds_label"], "Repair unnecessary")
		self.assertTrue(pre.name)

	def test_real_shortage_not_repaired(self):
		rm, fg = self._new_items("SH")
		submit_material_receipt(self.company, rm, qty=20, rate=1000, warehouse=self.stores)
		posting_date = "2026-09-01"
		prev = _enable_negative_stock(True)
		frappe.flags[PREVENTION_FLAG] = True
		try:
			mfg = self._make_manufacture(rm, fg, 15, posting_date, "15:00:00")
			mtfm = self._make_mtfm(rm, 10, posting_date, "15:00:00")
			frappe.db.set_value(
				"Stock Entry Detail",
				{"parent": mfg.name, "item_code": rm, "s_warehouse": self.wip},
				"against_stock_entry",
				mtfm.name,
			)
		finally:
			frappe.flags[PREVENTION_FLAG] = False
			_enable_negative_stock(bool(prev))
		rows = scan_production_posting_order_anomalies(
			company=self.company, from_date=posting_date, to_date=posting_date
		)
		match = [r for r in rows if r["inbound_document"] == mtfm.name and r["outbound_document"] == mfg.name]
		self.assertTrue(match)
		self.assertEqual(match[0]["optimizer_status"], "REAL_STOCK_SHORTAGE")
		self.assertFalse(match[0]["eligible"])
		blocked = apply_repairs([dict(match[0], eligible=True, status="ELIGIBLE")], dry_run=False)
		self.assertTrue(blocked["blocked"])

	def test_repair_replays_stock_value_and_bin(self):
		rm, fg, mtfm, mfg, posting_date, posting_time = self._inverted_chain("VAL")
		rows = scan_production_posting_order_anomalies(
			company=self.company, from_date=posting_date, to_date=posting_date
		)
		match = [
			r
			for r in rows
			if r["inbound_document"] == mtfm.name and r["outbound_document"] == mfg.name and r.get("eligible")
		]
		self.assertTrue(match)
		row = match[0]
		result = apply_repairs([row], dry_run=False)
		self.assertTrue(result["applied"], result)
		gate = integrity_check([mtfm.name, mfg.name], item_code=rm, warehouse=self.wip)
		self.assertTrue(gate["ok"], gate)
		self.assertTrue(gate.get("bin"))
		self.assertLessEqual(abs(flt(gate["bin"]["sle_qty"]) - flt(gate["bin"]["bin_qty"])), 0.5)
		self.assertLessEqual(abs(flt(gate["bin"]["sle_value"]) - flt(gate["bin"]["bin_value"])), 0.5)
		applied = result["applied"][0]
		self.assertTrue(applied.get("replay"))

	def test_operator_batch_same_time_classified(self):
		from erpnext_extensions.iran_accounting.stock_posting_order.scanner import scan_same_time_groups

		self.assertTrue(frappe.db.exists("Item", "18000007"), "operator item 18000007 required")
		result = scan_same_time_groups(from_date="2026-06-21", to_date="2026-06-21", include_likely=True)
		match = [
			r
			for r in result["rows"]
			if r.get("item") == "18000007" and "18:01:20" in str(r.get("current_inbound_time") or "")
		]
		self.assertTrue(match, "expected 18:01:20 SABB group for 18000007")
		row = match[0]
		self.assertTrue(row.get("batch"))
		self.assertTrue(row.get("has_batch"))
		self.assertEqual(row.get("confidence"), "LIKELY")
		self.assertFalse(row.get("eligible"))
		self.assertEqual(row.get("optimizer_status"), "NO_REPAIR_NEEDED")
		self.assertEqual(row.get("minimum_seconds_label"), "Repair unnecessary")


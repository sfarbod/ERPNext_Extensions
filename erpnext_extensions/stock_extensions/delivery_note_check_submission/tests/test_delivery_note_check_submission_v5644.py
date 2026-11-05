# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.6.44 — future-ledger / batch NegativeStockError parity for Check Submission."""

from __future__ import annotations

import unittest

import frappe
from frappe.utils import add_days, flt, getdate, nowdate, random_string

from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry

from erpnext_extensions.iran_accounting.e2e_bootstrap import (
	apply_stock_entry_site_defaults,
	get_irr_company,
	get_warehouse,
)
from erpnext_extensions.stock_extensions.delivery_note_check_submission.api import (
	validate_delivery_note_before_submit,
)
from erpnext_extensions.stock_extensions.delivery_note_check_submission.stock_availability import (
	_future_batch_negative_after_outbound,
	collect_stock_availability_errors,
)
from erpnext_extensions.stock_extensions.delivery_note_check_submission.tests.test_delivery_note_check_submission_v5637 import (
	_customer,
	_disable_negative_stock,
	_fingerprint,
	_make_batch,
	_make_item,
	create_delivery_note,
)
from erpnext_extensions.stock_extensions.delivery_note_check_submission.write_guard import enforce_read_only


def _submit_issue(*, item, qty, warehouse, company, batch_no, posting_date):
	se = make_stock_entry(
		item_code=item,
		qty=qty,
		rate=40,
		source=warehouse,
		company=company,
		purpose="Material Issue",
		do_not_save=True,
		do_not_submit=True,
	)
	se.set_posting_time = 1
	se.posting_date = posting_date
	se.posting_time = "16:00:00"
	for row in se.items:
		row.use_serial_batch_fields = 1
		row.batch_no = batch_no
		row.allow_zero_valuation_rate = 1
	frappe.flags.iran_gate_defaults = True
	apply_stock_entry_site_defaults(se)
	se.insert()
	se.submit()
	return se.name


class TestDeliveryNoteCheckSubmissionV5644(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		_disable_negative_stock()
		cls.company = get_irr_company("اسپاد فارمد دارو")
		cls.warehouse = get_warehouse(cls.company)
		cls.customer = _customer()
		if not cls.company or not cls.customer or not cls.warehouse:
			raise unittest.SkipTest("Missing company/customer/warehouse fixtures")

	def setUp(self):
		frappe.set_user("Administrator")
		_disable_negative_stock()

	def _assert_immutable(self, before, dn_name, item, warehouse):
		after = _fingerprint(dn_name, item, warehouse)
		self.assertEqual(frappe.as_json(before), frappe.as_json(after))

	def test_future_batch_consumption_blocks_backdated_dn(self):
		"""Historical qty OK at DN date, but later issue makes Submit fail → Check must BLOCK."""
		item = _make_item(
			self.company,
			{
				"has_batch_no": 1,
				"create_new_batch": 0,
				"batch_number_series": f"DNCS44-B-{random_string(3)}-.#####",
			},
		)
		receive_date = add_days(nowdate(), -60)
		dn_date = add_days(nowdate(), -40)
		future_issue_date = add_days(nowdate(), -10)
		# Receive 100 before DN date; batch expires far in the future.
		# _make_batch posts receipt at expiry-60 — choose expiry so receipt lands on receive_date.
		batch_no = _make_batch(
			item, add_days(receive_date, 60), self.company, self.warehouse, qty=100
		)
		# Ensure batch remains valid relative to DN / today for this scenario.
		frappe.db.set_value("Batch", batch_no, "expiry_date", add_days(nowdate(), 180))
		frappe.clear_cache(doctype="Batch")
		# Later consumption leaves only 20 of this batch in the warehouse.
		_submit_issue(
			item=item,
			qty=80,
			warehouse=self.warehouse,
			company=self.company,
			batch_no=batch_no,
			posting_date=future_issue_date,
		)

		# Backdated DN asks for 50 — historical at dn_date is 100, but future cumulative → -30.
		dn = create_delivery_note(
			company=self.company,
			customer=self.customer,
			warehouse=self.warehouse,
			item_code=item,
			qty=50,
			posting_date=dn_date,
			use_serial_batch_fields=1,
			batch_no=batch_no,
			do_not_submit=True,
		)
		before = _fingerprint(dn.name, item, self.warehouse)

		# Direct helper must detect the deficit.
		from erpnext.stock.utils import get_combine_datetime

		hit = _future_batch_negative_after_outbound(
			item,
			self.warehouse,
			batch_no,
			get_combine_datetime(dn_date, "12:00:00"),
			50,
		)
		self.assertIsNotNone(hit)
		self.assertAlmostEqual(flt(hit["needed"]), 30.0, places=2)

		result = validate_delivery_note_before_submit(dn.name)
		self.assertFalse(result["ok"])
		self.assertFalse(result["ready_for_submission"])
		self.assertEqual(result["status"], "blocked")
		batch_errs = [
			e
			for e in result["errors"]
			if e.get("batch_no") == batch_no and e.get("check") == "future_batch_ledger"
		]
		self.assertTrue(batch_errs, result["errors"])
		self.assertAlmostEqual(flt(batch_errs[0].get("needed")), 30.0, places=2)
		self._assert_immutable(before, dn.name, item, self.warehouse)

	def test_reference_mat_dn_2026_00281_not_ready(self):
		"""Live regression: MAT-DN-2026-00281 must not report READY while batch stock fails Submit."""
		name = "MAT-DN-2026-00281"
		if not frappe.db.exists("Delivery Note", name):
			self.skipTest(f"{name} not on this site")
		meta = frappe.db.get_value(
			"Delivery Note", name, ["docstatus", "posting_date", "posting_time"], as_dict=True
		)
		if not meta or meta.docstatus != 0:
			self.skipTest(f"{name} is not a Draft Delivery Note")

		item = "20100138"
		batch = "3123-20100138-ss241607p22"
		wh = frappe.db.get_value(
			"Delivery Note Item",
			{"parent": name, "item_code": item, "batch_no": batch},
			"warehouse",
		)
		if not wh:
			self.skipTest(f"{name} no longer contains {item}/{batch}")

		before = _fingerprint(name, item, wh)
		result = validate_delivery_note_before_submit(name)
		self.assertNotEqual(result["status"], "ready")
		self.assertFalse(result["ready_for_submission"])
		self.assertFalse(result["ok"])

		matched = [
			e
			for e in result["errors"]
			if e.get("batch_no") == batch and e.get("exc_type") == "NegativeStockError"
		]
		self.assertTrue(matched, result["errors"])
		self.assertAlmostEqual(flt(matched[0].get("needed")), 46.0, places=2)
		self._assert_immutable(before, name, item, wh)

	def test_write_guard_around_future_batch_check(self):
		item = _make_item(self.company)
		dn = create_delivery_note(
			company=self.company,
			customer=self.customer,
			warehouse=self.warehouse,
			item_code=item,
			qty=1,
			do_not_submit=True,
		)
		before = _fingerprint(dn.name, item, self.warehouse)
		with enforce_read_only():
			doc = frappe.get_doc("Delivery Note", dn.name)
			collect_stock_availability_errors(doc)
		self._assert_immutable(before, dn.name, item, self.warehouse)

	def test_status_values_are_ready_blocked_or_incomplete(self):
		item = _make_item(self.company)
		dn = create_delivery_note(
			company=self.company,
			customer=self.customer,
			warehouse=self.warehouse,
			item_code=item,
			qty=1,
			do_not_submit=True,
		)
		# Force insufficient stock so status is blocked.
		dn.items[0].qty = 999999
		dn.items[0].stock_qty = 999999
		frappe.db.set_value("Delivery Note Item", dn.items[0].name, "qty", 999999)
		frappe.db.set_value("Delivery Note Item", dn.items[0].name, "stock_qty", 999999)
		result = validate_delivery_note_before_submit(dn.name)
		self.assertIn(result["status"], ("ready", "blocked", "incomplete"))
		self.assertEqual(result["status"], "blocked")

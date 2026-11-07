# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

"""v5.7.5 — Request Purchase must not fail Required By vs Transaction Date.

Regression for AUD-AR-2026-00049: AR txn 2026-10-27 / required 2026-10-28
(Jalali 1405-08-05 / 1405-08-06) failed when MR used transaction_date=today().
"""

from __future__ import annotations

import unittest

import frappe
from frappe.utils import add_days, getdate, today

from erpnext_extensions.asset_usage_depreciation.services.fulfillment_service import (
	material_request_dates_from_asset_request,
)
from erpnext_extensions.asset_usage_depreciation.tests import test_helpers as h

ASSET = "AUD-AR-2026-00049"


class TestMaterialRequestDatesHelperV575(unittest.TestCase):
	def test_required_after_transaction(self):
		doc = frappe._dict(transaction_date="2026-10-27", required_date="2026-10-28")
		txn, sched = material_request_dates_from_asset_request(doc)
		self.assertEqual(txn, getdate("2026-10-27"))
		self.assertEqual(sched, getdate("2026-10-28"))
		self.assertFalse(sched < txn)

	def test_required_equals_transaction(self):
		doc = frappe._dict(transaction_date="2026-10-27", required_date="2026-10-27")
		txn, sched = material_request_dates_from_asset_request(doc)
		self.assertEqual(txn, sched)

	def test_required_several_days_after(self):
		doc = frappe._dict(transaction_date="2026-10-27", required_date="2026-11-05")
		txn, sched = material_request_dates_from_asset_request(doc)
		self.assertEqual(sched, getdate("2026-11-05"))
		self.assertGreater(sched, txn)

	def test_item_level_required_date_preferred(self):
		doc = frappe._dict(transaction_date="2026-10-27", required_date="2026-10-28")
		item = frappe._dict(required_date="2026-11-01")
		txn, sched = material_request_dates_from_asset_request(doc, item)
		self.assertEqual(txn, getdate("2026-10-27"))
		self.assertEqual(sched, getdate("2026-11-01"))

	def test_required_before_transaction_clamped(self):
		"""Does not invent invalid pairs; clamp schedule up to transaction_date."""
		doc = frappe._dict(transaction_date="2026-10-28", required_date="2026-10-27")
		txn, sched = material_request_dates_from_asset_request(doc)
		self.assertEqual(txn, getdate("2026-10-28"))
		self.assertEqual(sched, getdate("2026-10-28"))

	def test_missing_required_falls_back_to_transaction(self):
		doc = frappe._dict(transaction_date="2026-10-27", required_date=None)
		txn, sched = material_request_dates_from_asset_request(doc)
		self.assertEqual(txn, sched)

	def test_past_required_vs_today_still_uses_ar_transaction(self):
		"""Core bug: today() as MR txn made past AR required_date fail."""
		past_txn = add_days(today(), -20)
		past_req = add_days(today(), -19)
		doc = frappe._dict(transaction_date=past_txn, required_date=past_req)
		txn, sched = material_request_dates_from_asset_request(doc)
		self.assertEqual(txn, getdate(past_txn))
		self.assertEqual(sched, getdate(past_req))
		self.assertLess(sched, getdate(today()))
		self.assertFalse(sched < txn)


class TestRequestPurchaseDatesIntegrationV575(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		cls.skip = h.skip_if_unready()
		if cls.skip:
			return
		h.ensure_settings(prevent_duplicate_active_requests=0, require_named_manager_approver=0)
		h.ensure_employee_asset_request_perms()
		cls.company = h.company()
		cls.employee = h.make_employee(company_name=cls.company)

	def _ready(self):
		if self.skip:
			self.skipTest(self.skip)

	def test_request_purchase_with_past_ar_dates_succeeds(self):
		"""Synthetic AR with past txn/required dates must create a valid MR."""
		self._ready()
		item = h.make_fixed_asset_item()
		txn = add_days(today(), -14)
		req = add_days(today(), -13)
		doc = h.make_request(
			company_name=self.company,
			employee=self.employee,
			item_code=item,
			transaction_date=str(txn),
			required_date=str(req),
		)
		# Force dates in case helper overwrites
		doc.db_set("transaction_date", txn, update_modified=False)
		doc.db_set("required_date", req, update_modified=False)
		for row in doc.items:
			frappe.db.set_value(
				"Asset Request Item", row.name, "required_date", req, update_modified=False
			)
		doc.reload()
		h.submit_and_approve(doc)
		doc.reload()
		h.request_purchase(doc)
		doc.reload()
		self.assertTrue(doc.material_request)
		mr = frappe.get_doc("Material Request", doc.material_request)
		self.assertEqual(getdate(mr.transaction_date), getdate(txn))
		self.assertEqual(getdate(mr.schedule_date), getdate(req))
		for row in mr.items:
			self.assertEqual(getdate(row.schedule_date), getdate(req))
			self.assertFalse(getdate(row.schedule_date) < getdate(mr.transaction_date))
		# Submit must also pass ERPNext schedule validation
		if mr.docstatus == 0:
			mr.submit()
		self.assertEqual(mr.docstatus, 1)

	def test_aud_ar_2026_00049_jalali_display_case(self):
		"""Reproduce production case: Jalali 1405-08-05 / 1405-08-06."""
		if not frappe.db.exists("Asset Request", ASSET):
			self.skipTest(f"{ASSET} not on site")
		ar = frappe.get_doc("Asset Request", ASSET)
		self.assertEqual(getdate(ar.transaction_date), getdate("2026-10-27"))
		self.assertEqual(getdate(ar.required_date), getdate("2026-10-28"))
		self.assertEqual(ar.status, "Approved")

		from erpnext_extensions.asset_usage_depreciation.doctype.asset_request.asset_request import (
			request_purchase as request_purchase_rpc,
		)

		# If a prior successful MR exists, assert its dates; otherwise create via RPC
		# (RPC persists submitted Asset Request header + allocations).
		if ar.material_request and frappe.db.exists("Material Request", ar.material_request):
			mr_name = ar.material_request
		else:
			result = request_purchase_rpc(ASSET)
			mr_name = result["material_request"]
			ar.reload()
			self.assertEqual(ar.material_request, mr_name)

		mr = frappe.get_doc("Material Request", mr_name)
		self.assertEqual(getdate(mr.transaction_date), getdate("2026-10-27"))
		self.assertEqual(getdate(mr.schedule_date), getdate("2026-10-28"))
		for row in mr.items:
			self.assertEqual(getdate(row.schedule_date), getdate("2026-10-28"))
			self.assertFalse(getdate(row.schedule_date) < getdate(mr.transaction_date))
		if mr.docstatus == 0:
			mr.submit()
		self.assertEqual(mr.docstatus, 1)

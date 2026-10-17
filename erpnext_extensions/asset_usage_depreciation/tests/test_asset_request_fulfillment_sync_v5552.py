# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

"""v5.5.52 — Asset Request fulfillment sync from Asset Movement delivery."""

from __future__ import annotations

import unittest

import frappe
from frappe.utils import cint, now_datetime, random_string

from erpnext_extensions.asset_usage_depreciation.constants import (
	ALLOC_ISSUED,
	ALLOC_MOVEMENT_DRAFT,
	FULFILLMENT_FULFILLED,
	FULFILLMENT_PARTIALLY_FULFILLED,
	FULFILLMENT_WAITING,
	WF_STATE_APPROVED,
)
from erpnext_extensions.asset_usage_depreciation.services.delivery_sync import (
	demote_ineligible_transfer_allocations,
	evaluate_delivery_eligibility,
	is_delivery_movement,
	on_asset_movement_submit,
	reconcile_asset_request_fulfillment,
	sync_request_from_movement,
)
from erpnext_extensions.asset_usage_depreciation.services.request_service import (
	refresh_header_fulfillment,
)
from erpnext_extensions.asset_usage_depreciation.tests import test_helpers as h


class TestAssetRequestFulfillmentSyncV5552(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		cls.skip = h.skip_if_unready()
		if cls.skip:
			return
		h.ensure_settings(
			auto_create_material_request=0,
			auto_create_asset_movement=0,
			require_named_manager_approver=0,
			prevent_duplicate_active_requests=0,
		)
		cls.company = h.company()
		cls.employee = h.make_employee(company_name=cls.company)

	def _ready(self):
		if getattr(self, "skip", None):
			self.skipTest(self.skip)

	def _approved(self, item_code: str, qty: int = 1, **kwargs):
		doc = h.make_request(
			company_name=self.company,
			employee=self.employee,
			item_code=item_code,
			qty=qty,
			**kwargs,
		)
		h.submit_and_approve(doc)
		doc.reload()
		return doc

	def _submit_am(self, am_name: str):
		am = frappe.get_doc("Asset Movement", am_name)
		if cint(am.docstatus) == 0:
			am.submit()
		return am

	# 1 — Approved without delivery
	def test_01_approved_without_delivery(self):
		self._ready()
		item = h.make_fixed_asset_item()
		doc = self._approved(item, qty=2)
		self.assertEqual(doc.workflow_state, WF_STATE_APPROVED)
		self.assertEqual(doc.status, "Approved")
		self.assertEqual(doc.fulfillment_status, FULFILLMENT_WAITING)
		self.assertEqual(cint(doc.delivered_qty), 0)
		self.assertEqual(cint(doc.items[0].delivered_qty), 0)
		self.assertEqual(cint(doc.items[0].remaining_qty), 2)

	# 2 — Single-item full delivery (Issue from Pool → submit AM)
	def test_02_single_item_full_delivery(self):
		self._ready()
		tag = random_string(6)
		cat = h.make_isolated_category(tag)
		item = h.make_fixed_asset_item(code=f"AUD-AR-F52-S-{tag}", category=cat)
		asset = h.make_pool_asset(item_code=item, company_name=self.company)
		doc = self._approved(item, qty=1)
		h.issue_from_pool(doc, selections=[{"item_row": doc.items[0].name, "asset": asset}])
		doc.reload()
		self.assertEqual(doc.allocations[0].fulfillment_status, ALLOC_MOVEMENT_DRAFT)
		am_name = doc.allocations[0].asset_movement
		self._submit_am(am_name)
		doc.reload()
		self.assertEqual(doc.allocations[0].fulfillment_status, ALLOC_ISSUED)
		self.assertEqual(doc.fulfillment_status, FULFILLMENT_FULFILLED)
		self.assertEqual(cint(doc.delivered_qty), 1)
		self.assertEqual(cint(doc.items[0].remaining_qty), 0)
		self.assertEqual(doc.workflow_state, WF_STATE_APPROVED)
		self.assertEqual(doc.status, "Approved")

	# 3 — Partial delivery
	def test_03_partial_delivery(self):
		self._ready()
		tag = random_string(6)
		cat = h.make_isolated_category(tag)
		item = h.make_fixed_asset_item(code=f"AUD-AR-F52-P-{tag}", category=cat)
		a1 = h.make_pool_asset(item_code=item, company_name=self.company)
		h.make_pool_asset(item_code=item, company_name=self.company)
		doc = self._approved(item, qty=2)
		h.issue_from_pool(doc, selections=[{"item_row": doc.items[0].name, "asset": a1}])
		doc.reload()
		self._submit_am(doc.allocations[0].asset_movement)
		doc.reload()
		self.assertEqual(doc.fulfillment_status, FULFILLMENT_PARTIALLY_FULFILLED)
		self.assertEqual(cint(doc.delivered_qty), 1)
		self.assertEqual(cint(doc.items[0].remaining_qty), 1)

	# 4 / 5 — Multi-item partial then full
	def test_04_05_multi_item_partial_then_full(self):
		self._ready()
		tag = random_string(6)
		cat = h.make_isolated_category(tag)
		i1 = h.make_fixed_asset_item(code=f"AUD-AR-F52-M1-{tag}", category=cat)
		i2 = h.make_fixed_asset_item(code=f"AUD-AR-F52-M2-{tag}", category=cat)
		a1 = h.make_pool_asset(item_code=i1, company_name=self.company)
		a2 = h.make_pool_asset(item_code=i2, company_name=self.company)
		doc = h.make_request(company_name=self.company, employee=self.employee, item_code=i1, qty=1)
		doc.append("items", {"requested_item_code": i2, "qty": 1})
		doc.save(ignore_permissions=True)
		h.submit_and_approve(doc)
		doc.reload()
		h.issue_from_pool(doc, selections=[{"item_row": doc.items[0].name, "asset": a1}])
		doc.reload()
		self._submit_am(doc.allocations[0].asset_movement)
		doc.reload()
		self.assertEqual(doc.fulfillment_status, FULFILLMENT_PARTIALLY_FULFILLED)
		h.issue_from_pool(doc, selections=[{"item_row": doc.items[1].name, "asset": a2}])
		doc.reload()
		# Second AM may be a new draft
		ams = {a.asset_movement for a in doc.allocations if a.asset_movement}
		for am in ams:
			self._submit_am(am)
		doc.reload()
		self.assertEqual(doc.fulfillment_status, FULFILLMENT_FULFILLED)
		self.assertEqual(cint(doc.delivered_qty), 2)

	# 6 — Draft AM does not count
	def test_06_draft_movement_not_delivered(self):
		self._ready()
		tag = random_string(6)
		cat = h.make_isolated_category(tag)
		item = h.make_fixed_asset_item(code=f"AUD-AR-F52-D-{tag}", category=cat)
		asset = h.make_pool_asset(item_code=item, company_name=self.company)
		doc = self._approved(item, qty=1)
		h.issue_from_pool(doc, selections=[{"item_row": doc.items[0].name, "asset": asset}])
		doc.reload()
		self.assertEqual(doc.allocations[0].fulfillment_status, ALLOC_MOVEMENT_DRAFT)
		self.assertEqual(cint(doc.delivered_qty), 0)
		self.assertNotEqual(doc.fulfillment_status, FULFILLMENT_FULFILLED)

	# 7 covered by test_02; 8 — cancel reverses
	def test_08_cancel_movement_reverts_delivery(self):
		self._ready()
		tag = random_string(6)
		cat = h.make_isolated_category(tag)
		item = h.make_fixed_asset_item(code=f"AUD-AR-F52-C-{tag}", category=cat)
		asset = h.make_pool_asset(item_code=item, company_name=self.company)
		doc = self._approved(item, qty=1)
		h.issue_from_pool(doc, selections=[{"item_row": doc.items[0].name, "asset": asset}])
		doc.reload()
		am = self._submit_am(doc.allocations[0].asset_movement)
		doc.reload()
		self.assertEqual(doc.fulfillment_status, FULFILLMENT_FULFILLED)
		am.cancel()
		doc.reload()
		self.assertEqual(cint(doc.delivered_qty), 0)
		self.assertNotEqual(doc.fulfillment_status, FULFILLMENT_FULFILLED)

	# 9 — Duplicate movement references do not double-count
	def test_09_duplicate_asset_not_double_counted(self):
		self._ready()
		tag = random_string(6)
		cat = h.make_isolated_category(tag)
		item = h.make_fixed_asset_item(code=f"AUD-AR-F52-X-{tag}", category=cat)
		asset = h.make_pool_asset(item_code=item, company_name=self.company)
		doc = self._approved(item, qty=2)
		doc.append(
			"allocations",
			{
				"asset_request_item": doc.items[0].name,
				"requested_item_code": item,
				"fulfilled_item_code": item,
				"method": "Issue Existing",
				"fulfillment_status": ALLOC_ISSUED,
				"allocated_asset": asset,
				"asset_movement": "AM-FAKE-1",
			},
		)
		doc.append(
			"allocations",
			{
				"asset_request_item": doc.items[0].name,
				"requested_item_code": item,
				"fulfilled_item_code": item,
				"method": "Issue Existing",
				"fulfillment_status": ALLOC_ISSUED,
				"allocated_asset": asset,
				"asset_movement": "AM-FAKE-2",
			},
		)
		refresh_header_fulfillment(doc)
		self.assertEqual(cint(doc.items[0].delivered_qty), 1)

	# 10 — Unrelated movement ignored
	def test_10_unrelated_movement_ignored(self):
		self._ready()
		item = h.make_fixed_asset_item()
		doc = self._approved(item, qty=1)
		asset = h.make_pool_asset(item_code=item, company_name=self.company)
		src = frappe.db.get_value("Asset", asset, "location")
		am = frappe.get_doc(
			{
				"doctype": "Asset Movement",
				"company": self.company,
				"purpose": "Issue",
				"transaction_date": now_datetime(),
				"assets": [
					{
						"asset": asset,
						"source_location": src,
						"to_employee": self.employee,
						"company": self.company,
					}
				],
			}
		)
		am.insert(ignore_permissions=True)
		am.submit()
		on_asset_movement_submit(am)
		doc.reload()
		self.assertEqual(doc.fulfillment_status, FULFILLMENT_WAITING)
		self.assertFalse(doc.allocations)

	# 11 — Transfer to requesting employee (proven handover)
	def test_11_manual_linked_transfer_syncs(self):
		self._ready()
		tag = random_string(6)
		cat = h.make_isolated_category(tag)
		item = h.make_fixed_asset_item(code=f"AUD-AR-F52-T-{tag}", category=cat)
		assets = [h.make_pool_asset(item_code=item, company_name=self.company) for _ in range(3)]
		doc = self._approved(item, qty=3)
		target = h.ensure_location(f"AR Target {tag}")
		rows = []
		for a in assets:
			src = frappe.db.get_value("Asset", a, "location")
			rows.append(
				{
					"asset": a,
					"source_location": src,
					"target_location": target,
					"to_employee": self.employee,
					"company": self.company,
				}
			)
		am = frappe.get_doc(
			{
				"doctype": "Asset Movement",
				"company": self.company,
				"purpose": "Transfer",
				"transaction_date": now_datetime(),
				"reference_doctype": "Asset Request",
				"reference_name": doc.name,
				"assets": rows,
			}
		)
		am.insert(ignore_permissions=True)
		am.submit()
		doc.reload()
		self.assertEqual(doc.fulfillment_status, FULFILLMENT_FULFILLED)
		self.assertEqual(cint(doc.delivered_qty), 3)
		self.assertEqual(len([a for a in doc.allocations if a.fulfillment_status == ALLOC_ISSUED]), 3)
		self.assertTrue(is_delivery_movement(am, ar_employee=self.employee))

	# 12 — Repeated synchronization idempotent
	def test_12_repeated_sync_idempotent(self):
		self._ready()
		tag = random_string(6)
		cat = h.make_isolated_category(tag)
		item = h.make_fixed_asset_item(code=f"AUD-AR-F52-I-{tag}", category=cat)
		asset = h.make_pool_asset(item_code=item, company_name=self.company)
		doc = self._approved(item, qty=1)
		h.issue_from_pool(doc, selections=[{"item_row": doc.items[0].name, "asset": asset}])
		doc.reload()
		am = self._submit_am(doc.allocations[0].asset_movement)
		r1 = sync_request_from_movement(doc.name, am)
		r2 = sync_request_from_movement(doc.name, am)
		doc.reload()
		self.assertEqual(cint(doc.delivered_qty), 1)
		self.assertEqual(len(doc.allocations), 1)
		self.assertFalse(r2.get("created_allocations"))

	# 13 — Historical reconciliation (Transfer + employee handover)
	def test_13_historical_reconciliation(self):
		self._ready()
		tag = random_string(6)
		cat = h.make_isolated_category(tag)
		item = h.make_fixed_asset_item(code=f"AUD-AR-F52-H-{tag}", category=cat)
		assets = [h.make_pool_asset(item_code=item, company_name=self.company) for _ in range(2)]
		doc = self._approved(item, qty=2)
		target = h.ensure_location(f"AR Hist {tag}")
		am = frappe.get_doc(
			{
				"doctype": "Asset Movement",
				"company": self.company,
				"purpose": "Transfer",
				"transaction_date": now_datetime(),
				"reference_doctype": "Asset Request",
				"reference_name": doc.name,
				"assets": [
					{
						"asset": a,
						"source_location": frappe.db.get_value("Asset", a, "location"),
						"target_location": target,
						"to_employee": self.employee,
						"company": self.company,
					}
					for a in assets
				],
			}
		)
		am.insert(ignore_permissions=True)
		# Submit without hook path — set docstatus via submit then re-run reconcile
		# (submit will fire hook; for isolation call reconcile on a second stale request pattern)
		am.submit()
		# Force stale: clear allocations & waiting
		frappe.db.sql("delete from `tabAsset Request Allocation` where parent=%s", doc.name)
		frappe.db.set_value(
			"Asset Request",
			doc.name,
			{"fulfillment_status": FULFILLMENT_WAITING, "delivered_qty": 0, "issued_qty": 0},
			update_modified=False,
		)
		frappe.db.commit()
		summary = reconcile_asset_request_fulfillment(asset_request=doc.name, dry_run=False)
		doc.reload()
		self.assertEqual(doc.fulfillment_status, FULFILLMENT_FULFILLED)
		self.assertEqual(cint(doc.delivered_qty), 2)
		self.assertTrue(any(u.get("asset_request") == doc.name for u in summary.get("updated") or []))

	# 14 — AUD-AR-2026-00008 scenario (site fixture if present)
	def test_14_aud_ar_2026_00008_scenario(self):
		self._ready()
		name = "AUD-AR-2026-00008"
		if not frappe.db.exists("Asset Request", name):
			self.skipTest("AUD-AR-2026-00008 not on site")
		summary = reconcile_asset_request_fulfillment(asset_request=name, dry_run=False)
		doc = frappe.get_doc("Asset Request", name)
		self.assertEqual(doc.workflow_state, "Approved")
		self.assertEqual(doc.status, "Approved")
		# 10 chairs via ACC-ASM-2026-11731 Transfer with to_employee handover
		self.assertGreaterEqual(cint(doc.delivered_qty), 10)
		self.assertEqual(doc.fulfillment_status, FULFILLMENT_FULFILLED)
		self.assertEqual(cint(doc.items[0].remaining_qty), 0)
		_ = summary

	# 15 — Location-only Transfer with AR reference is NOT delivery
	def test_15_location_only_transfer_not_delivery(self):
		self._ready()
		tag = random_string(6)
		cat = h.make_isolated_category(tag)
		item = h.make_fixed_asset_item(code=f"AUD-AR-F52-L-{tag}", category=cat)
		asset = h.make_pool_asset(item_code=item, company_name=self.company)
		doc = self._approved(item, qty=1)
		target = h.ensure_location(f"AR LocOnly {tag}")
		am = frappe.get_doc(
			{
				"doctype": "Asset Movement",
				"company": self.company,
				"purpose": "Transfer",
				"transaction_date": now_datetime(),
				"reference_doctype": "Asset Request",
				"reference_name": doc.name,
				"assets": [
					{
						"asset": asset,
						"source_location": frappe.db.get_value("Asset", asset, "location"),
						"target_location": target,
						"company": self.company,
					}
				],
			}
		)
		am.insert(ignore_permissions=True)
		am.submit()
		doc.reload()
		elig = evaluate_delivery_eligibility(am, ar_employee=self.employee)
		self.assertFalse(elig["eligible"])
		self.assertEqual(elig["reason"], "transfer_without_employee_handover")
		self.assertEqual(doc.fulfillment_status, FULFILLMENT_WAITING)
		self.assertEqual(cint(doc.delivered_qty), 0)
		self.assertFalse([a for a in (doc.allocations or []) if a.fulfillment_status == ALLOC_ISSUED])

	# 16 — Transfer to a different employee is NOT delivery
	def test_16_transfer_to_other_employee_not_delivery(self):
		self._ready()
		tag = random_string(6)
		cat = h.make_isolated_category(tag)
		item = h.make_fixed_asset_item(code=f"AUD-AR-F52-OE-{tag}", category=cat)
		asset = h.make_pool_asset(item_code=item, company_name=self.company)
		doc = self._approved(item, qty=1)
		other_email = f"ar.other.{tag.lower()}@example.com"
		h.make_user(email=other_email, roles=["Employee", "Desk User"])
		other = h.make_employee(company_name=self.company, user_id=other_email)
		self.assertNotEqual(other, self.employee)
		target = h.ensure_location(f"AR OtherEmp {tag}")
		am = frappe.get_doc(
			{
				"doctype": "Asset Movement",
				"company": self.company,
				"purpose": "Transfer",
				"transaction_date": now_datetime(),
				"reference_doctype": "Asset Request",
				"reference_name": doc.name,
				"assets": [
					{
						"asset": asset,
						"source_location": frappe.db.get_value("Asset", asset, "location"),
						"target_location": target,
						"to_employee": other,
						"company": self.company,
					}
				],
			}
		)
		am.insert(ignore_permissions=True)
		am.submit()
		doc.reload()
		self.assertFalse(is_delivery_movement(am, ar_employee=self.employee))
		self.assertEqual(doc.fulfillment_status, FULFILLMENT_WAITING)
		self.assertEqual(cint(doc.delivered_qty), 0)

	# 17 — Demote previously Issued location-only Transfer allocations
	def test_17_demote_ineligible_transfer_allocations(self):
		self._ready()
		tag = random_string(6)
		cat = h.make_isolated_category(tag)
		item = h.make_fixed_asset_item(code=f"AUD-AR-F52-DM-{tag}", category=cat)
		asset = h.make_pool_asset(item_code=item, company_name=self.company)
		doc = self._approved(item, qty=1)
		target = h.ensure_location(f"AR Demote {tag}")
		am = frappe.get_doc(
			{
				"doctype": "Asset Movement",
				"company": self.company,
				"purpose": "Transfer",
				"transaction_date": now_datetime(),
				"reference_doctype": "Asset Request",
				"reference_name": doc.name,
				"assets": [
					{
						"asset": asset,
						"source_location": frappe.db.get_value("Asset", asset, "location"),
						"target_location": target,
						"company": self.company,
					}
				],
			}
		)
		am.insert(ignore_permissions=True)
		am.submit()
		# Simulate pre-hardening Issued allocation from reference-only Transfer
		doc.reload()
		doc.append(
			"allocations",
			{
				"asset_request_item": doc.items[0].name,
				"requested_item_code": item,
				"fulfilled_item_code": item,
				"method": "Issue Existing",
				"fulfillment_status": ALLOC_ISSUED,
				"allocated_asset": asset,
				"asset_movement": am.name,
			},
		)
		doc.flags.ignore_validate_update_after_submit = True
		doc.save(ignore_permissions=True)
		refresh_header_fulfillment(doc)
		doc.flags.ignore_validate_update_after_submit = True
		doc.save(ignore_permissions=True)
		doc.reload()
		self.assertEqual(doc.fulfillment_status, FULFILLMENT_FULFILLED)
		demote = demote_ineligible_transfer_allocations(asset_request=doc.name, dry_run=False)
		doc.reload()
		self.assertTrue(demote.get("demoted"))
		self.assertEqual(cint(doc.delivered_qty), 0)
		self.assertNotEqual(doc.fulfillment_status, FULFILLMENT_FULFILLED)

	# 18 — Qty fields: requested / remaining after approve
	def test_18_requested_remaining_qty_fields(self):
		self._ready()
		item = h.make_fixed_asset_item()
		doc = self._approved(item, qty=5)
		self.assertEqual(cint(doc.requested_qty), 5)
		self.assertEqual(cint(doc.delivered_qty), 0)
		self.assertEqual(cint(doc.remaining_qty), 5)
		self.assertEqual(doc.fulfillment_status, FULFILLMENT_WAITING)

	# 19 — Transfer and Issue still counts (purpose delivery)
	def test_19_transfer_and_issue_counts(self):
		self._ready()
		tag = random_string(6)
		cat = h.make_isolated_category(tag)
		item = h.make_fixed_asset_item(code=f"AUD-AR-F52-TI-{tag}", category=cat)
		asset = h.make_pool_asset(item_code=item, company_name=self.company)
		# ERPNext requires from_employee matching current custodian for Transfer and Issue
		cust_email = f"ar.cust.{tag.lower()}@example.com"
		h.make_user(email=cust_email, roles=["Employee", "Desk User"])
		custodian = h.make_employee(company_name=self.company, user_id=cust_email)
		frappe.db.set_value("Asset", asset, "custodian", custodian)
		doc = self._approved(item, qty=1)
		src = frappe.db.get_value("Asset", asset, "location")
		target = h.ensure_location(f"AR TI {tag}")
		am = frappe.get_doc(
			{
				"doctype": "Asset Movement",
				"company": self.company,
				"purpose": "Transfer and Issue",
				"transaction_date": now_datetime(),
				"reference_doctype": "Asset Request",
				"reference_name": doc.name,
				"assets": [
					{
						"asset": asset,
						"source_location": src,
						"target_location": target,
						"from_employee": custodian,
						"to_employee": self.employee,
						"company": self.company,
					}
				],
			}
		)
		am.insert(ignore_permissions=True)
		am.submit()
		doc.reload()
		self.assertTrue(is_delivery_movement(am, ar_employee=self.employee))
		self.assertEqual(doc.fulfillment_status, FULFILLMENT_FULFILLED)
		self.assertEqual(cint(doc.delivered_qty), 1)

	# 20 — Reconciliation idempotent under hardened rules
	def test_20_reconcile_idempotent(self):
		self._ready()
		name = "AUD-AR-2026-00008"
		if not frappe.db.exists("Asset Request", name):
			self.skipTest("AUD-AR-2026-00008 not on site")
		r1 = reconcile_asset_request_fulfillment(asset_request=name, dry_run=False)
		before = frappe.db.get_value(
			"Asset Request",
			name,
			["fulfillment_status", "delivered_qty", "remaining_qty"],
			as_dict=True,
		)
		r2 = reconcile_asset_request_fulfillment(asset_request=name, dry_run=False)
		after = frappe.db.get_value(
			"Asset Request",
			name,
			["fulfillment_status", "delivered_qty", "remaining_qty"],
			as_dict=True,
		)
		self.assertEqual(before, after)
		self.assertFalse(r2.get("demote", {}).get("demoted"))
		_ = r1

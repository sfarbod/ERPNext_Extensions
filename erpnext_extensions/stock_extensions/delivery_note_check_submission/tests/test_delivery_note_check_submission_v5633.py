# Copyright (c) 2026, ERPNext Extensions contributors
"""Delivery Note Check Submission (v5.6.33) — non-destructive pre-submit validation.

Uses ``unittest.TestCase`` + iran_accounting e2e_bootstrap (avoids ERPNext
``BootStrapTestData`` UOM seeding and fills site-mandatory Stock Entry fields).
"""

from __future__ import annotations

import unittest
from unittest import mock

import frappe
from frappe.utils import cint, flt, nowdate, nowtime, random_string

from erpnext.stock.stock_ledger import NegativeStockError

from erpnext_extensions.iran_accounting.e2e_bootstrap import (
	ensure_test_item,
	get_irr_company,
	get_warehouse,
	submit_material_receipt,
)
from erpnext_extensions.stock_extensions.delivery_note_check_submission.api import (
	validate_delivery_note_before_submit,
)
from erpnext_extensions.stock_extensions.delivery_note_check_submission.validator import (
	validate_delivery_note_submission,
)

SUBMITTER_USER = "dn_check_submission_submitter@example.com"
NO_PERM_USER = "dn_check_submission_noperm@example.com"


def _customer() -> str:
	name = frappe.db.get_value("Customer", {"disabled": 0}, "name", order_by="creation asc")
	if not name:
		raise unittest.SkipTest("No Customer available")
	return name


def _expense_account(company: str) -> str:
	acc = frappe.db.get_value(
		"Account",
		{"company": company, "account_type": "Cost of Goods Sold", "is_group": 0},
		"name",
	) or frappe.db.get_value(
		"Account", {"company": company, "account_type": "Expense Account", "is_group": 0}, "name"
	)
	if not acc:
		raise unittest.SkipTest(f"No expense account for {company}")
	return acc


def _cost_center(company: str) -> str:
	cc = frappe.db.get_value(
		"Cost Center", {"company": company, "is_group": 0}, "name", order_by="creation asc"
	)
	if not cc:
		raise unittest.SkipTest(f"No cost center for {company}")
	return cc


def _department(company: str) -> str | None:
	return frappe.db.get_value(
		"Department", {"company": company, "is_group": 0}, "name", order_by="creation asc"
	)


def _disable_negative_stock() -> None:
	frappe.db.set_single_value("Stock Settings", "allow_negative_stock", 0)
	frappe.clear_cache(doctype="Stock Settings")
	# Drop in-memory single-doc cache used by is_negative_stock_allowed.
	frappe.cache.delete_value("singles_dict:Stock Settings")
	if hasattr(frappe.local, "document_cache"):
		frappe.local.document_cache.pop(("Stock Settings", "Stock Settings"), None)


def _make_item(company: str, properties: dict | None = None) -> str:
	item = ensure_test_item(company, prefix="DNCS")
	doc = frappe.get_doc("Item", item)
	# Delivery Note requires sales items.
	doc.is_sales_item = 1
	doc.is_stock_item = 1
	if properties:
		doc.update(properties)
	doc.save(ignore_permissions=True)
	return doc.name


def create_delivery_note(**args):
	"""Minimal draft/submitted DN fixture for the active IRR company."""
	args = frappe._dict(args)
	company = args.company
	dn = frappe.new_doc("Delivery Note")
	dn.posting_date = args.posting_date or nowdate()
	dn.posting_time = args.posting_time or nowtime()
	dn.set_posting_time = 1
	dn.company = company
	dn.customer = args.customer
	dn.currency = args.currency or frappe.get_cached_value("Company", company, "default_currency")
	row = {
		"item_code": args.item_code,
		"warehouse": args.warehouse,
		"qty": args.get("qty", 1),
		"rate": args.get("rate", 100),
		"conversion_factor": 1.0,
		"allow_zero_valuation_rate": args.get("allow_zero_valuation_rate", 1),
		"expense_account": args.expense_account or _expense_account(company),
		"cost_center": args.cost_center or _cost_center(company),
		"use_serial_batch_fields": args.get("use_serial_batch_fields"),
		"serial_no": args.get("serial_no"),
		"batch_no": args.get("batch_no"),
	}
	# Site ESPAD requires Department on P&L GL lines created at DN submit.
	dept = args.department or _department(company)
	if dept and frappe.get_meta("Delivery Note Item").has_field("department"):
		row["department"] = dept
	dn.append("items", row)
	if not args.do_not_save:
		dn.insert()
		if not args.do_not_submit:
			dn.submit()
		dn.load_from_db()
	return dn


def _ensure_role(role: str) -> None:
	if not frappe.db.exists("Role", role):
		frappe.get_doc({"doctype": "Role", "role_name": role, "desk_access": 1}).insert(
			ignore_permissions=True
		)


def _ensure_user(email: str, roles: list[str]) -> str:
	if frappe.db.exists("User", email):
		user = frappe.get_doc("User", email)
	else:
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": email.split("@")[0],
				"send_welcome_email": 0,
				"user_type": "System User",
			}
		)
		user.insert(ignore_permissions=True)
	for role in roles:
		_ensure_role(role)
		if not frappe.db.exists("Has Role", {"parent": email, "role": role}):
			user.add_roles(role)
	return email


def _bin_actual(item_code: str, warehouse: str) -> float:
	name = frappe.db.get_value("Bin", {"item_code": item_code, "warehouse": warehouse})
	if not name:
		return 0.0
	return flt(frappe.db.get_value("Bin", name, "actual_qty"))


def _sle_count(dn_name: str) -> int:
	return frappe.db.count(
		"Stock Ledger Entry", {"voucher_type": "Delivery Note", "voucher_no": dn_name}
	)


def _gle_count(dn_name: str) -> int:
	return frappe.db.count("GL Entry", {"voucher_type": "Delivery Note", "voucher_no": dn_name})


def _dn_snapshot(dn_name: str, item_code: str, warehouse: str) -> dict:
	return {
		"docstatus": cint(frappe.db.get_value("Delivery Note", dn_name, "docstatus")),
		"modified": frappe.db.get_value("Delivery Note", dn_name, "modified"),
		"sle": _sle_count(dn_name),
		"gle": _gle_count(dn_name),
		"bin": _bin_actual(item_code, warehouse),
	}


class TestDeliveryNoteCheckSubmissionV5633(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		_disable_negative_stock()
		cls.company = get_irr_company("اسپاد فارمد دارو")
		cls.warehouse = get_warehouse(cls.company)
		cls.customer = _customer()
		cls.department = _department(cls.company)

		_ensure_user(
			SUBMITTER_USER,
			["Stock User", "Stock Manager", "Sales User", "Sales Manager", "Accounts User"],
		)
		_ensure_user(NO_PERM_USER, ["Desk User"])

		cls.item = _make_item(cls.company)
		submit_material_receipt(cls.company, cls.item, qty=500, rate=100, warehouse=cls.warehouse)

	def setUp(self):
		frappe.set_user("Administrator")
		_disable_negative_stock()

	def tearDown(self):
		frappe.set_user("Administrator")

	def _draft_dn(self, **kwargs):
		kwargs.setdefault("item_code", self.item)
		kwargs.setdefault("warehouse", self.warehouse)
		kwargs.setdefault("company", self.company)
		kwargs.setdefault("customer", self.customer)
		kwargs.setdefault("do_not_submit", True)
		return create_delivery_note(**kwargs)

	def _assert_no_side_effects(self, before: dict, dn_name: str, item_code: str, warehouse: str):
		after = _dn_snapshot(dn_name, item_code, warehouse)
		self.assertEqual(after["docstatus"], 0)
		self.assertEqual(after["docstatus"], before["docstatus"])
		self.assertEqual(after["sle"], before["sle"])
		self.assertEqual(after["gle"], before["gle"])
		self.assertEqual(after["bin"], before["bin"])
		self.assertEqual(after["modified"], before["modified"])

	def test_write_without_submit_can_validate(self):
		dn = self._draft_dn(qty=1, rate=100)
		before = _dn_snapshot(dn.name, self.item, self.warehouse)
		real_has_permission = frappe.has_permission

		def _write_no_submit(doctype, ptype="read", doc=None, user=None, **kwargs):
			# Ignore raise_exception / verbose kwargs — signatures vary by Frappe version.
			kwargs.pop("raise_exception", None)
			if doctype == "Delivery Note":
				if ptype == "submit":
					return False
				if ptype in ("read", "write"):
					return True
			return real_has_permission(doctype, ptype=ptype, doc=doc, user=user, **kwargs)

		with mock.patch("frappe.has_permission", side_effect=_write_no_submit):
			with mock.patch(
				"erpnext_extensions.stock_extensions.delivery_note_check_submission.validator.frappe.has_permission",
				side_effect=_write_no_submit,
			):
				self.assertFalse(frappe.has_permission("Delivery Note", "submit", doc=dn))
				self.assertTrue(frappe.has_permission("Delivery Note", "write", doc=dn))
				result = validate_delivery_note_before_submit(name=dn.name)

		self.assertTrue(result["ok"], result)
		self._assert_no_side_effects(before, dn.name, self.item, self.warehouse)

	def test_submit_permission_user_can_validate(self):
		dn = self._draft_dn(qty=1, rate=100)
		frappe.set_user(SUBMITTER_USER)
		before = _dn_snapshot(dn.name, self.item, self.warehouse)
		result = validate_delivery_note_before_submit(name=dn.name)
		self.assertTrue(result["ok"], result)
		self._assert_no_side_effects(before, dn.name, self.item, self.warehouse)

	def test_insufficient_permission_rejected(self):
		dn = self._draft_dn(qty=1, rate=100)
		frappe.set_user(NO_PERM_USER)
		with self.assertRaises(frappe.PermissionError):
			validate_delivery_note_before_submit(name=dn.name)

	def test_valid_delivery_note_returns_success(self):
		dn = self._draft_dn(qty=1, rate=100)
		before = _dn_snapshot(dn.name, self.item, self.warehouse)
		result = validate_delivery_note_submission(dn.name)
		self.assertTrue(result["ok"], result)
		self.assertIn("ready for submission", result["message"])
		self.assertTrue(result["caveat"])
		self.assertEqual(result["errors"], [])
		self._assert_no_side_effects(before, dn.name, self.item, self.warehouse)

	def test_insufficient_stock_returns_erpnext_error(self):
		item = _make_item(self.company)
		frappe.db.set_value("Item", item, "allow_negative_stock", 0)
		frappe.clear_cache(doctype="Item")
		submit_material_receipt(self.company, item, qty=1, rate=50, warehouse=self.warehouse)
		_disable_negative_stock()
		dn = self._draft_dn(item_code=item, qty=50, rate=100)
		before = _dn_snapshot(dn.name, item, self.warehouse)
		self.assertLess(before["bin"], 50)
		result = validate_delivery_note_submission(dn.name)
		self.assertFalse(result["ok"])
		self.assertTrue(result["errors"])
		joined = " ".join(e["message"] for e in result["errors"]).lower()
		# Classic ERPNext Insufficient Stock, or site Iran valuation integrity (I4)
		# which also blocks the same oversell before persistence.
		self.assertTrue(
			any(
				token in joined
				for token in (
					"insufficient",
					"needed",
					"negative",
					"valuation integrity",
					"stock valuation",
				)
			),
			result["errors"],
		)
		self._assert_no_side_effects(before, dn.name, item, self.warehouse)

		# Control: real Submit fails with the same class of stock/valuation error.
		# On this site the failure is raised in on_submit (after docstatus is written
		# in the open test transaction), so we only assert the exception type here.
		dn_reload = frappe.get_doc("Delivery Note", dn.name)
		with self.assertRaises((NegativeStockError, frappe.ValidationError)):
			dn_reload.submit()

	def test_invalid_warehouse_returns_error(self):
		dn = self._draft_dn(qty=1, rate=100)
		frappe.db.set_value("Delivery Note Item", dn.items[0].name, "warehouse", "")
		dn.reload()
		before = _dn_snapshot(dn.name, self.item, self.warehouse)
		result = validate_delivery_note_submission(dn.name)
		self.assertFalse(result["ok"])
		joined = " ".join(e["message"] for e in result["errors"]).lower()
		self.assertTrue("warehouse" in joined, result["errors"])
		self._assert_no_side_effects(before, dn.name, self.item, self.warehouse)

	def test_missing_batch_number_returns_error(self):
		# Do not receive stock first — site Iran SE prevention blocks batch receipts
		# without a batch; DN validate itself must still reject missing batch.
		item = _make_item(
			self.company,
			{
				"has_batch_no": 1,
				"create_new_batch": 1,
				"batch_number_series": f"DNCS-BAT-{random_string(4)}-.#####",
			},
		)
		dn = self._draft_dn(item_code=item, qty=1, rate=100)
		before = _dn_snapshot(dn.name, item, self.warehouse)
		result = validate_delivery_note_submission(dn.name)
		self.assertFalse(result["ok"])
		joined = " ".join(e["message"] for e in result["errors"]).lower()
		self.assertTrue("batch" in joined or "serial" in joined, result["errors"])
		self._assert_no_side_effects(before, dn.name, item, self.warehouse)

	def test_invalid_serial_number_returns_error(self):
		item = _make_item(
			self.company,
			{
				"has_serial_no": 1,
				"serial_no_series": f"DNCS-SN-{random_string(4)}-.#####",
			},
		)
		se = submit_material_receipt(self.company, item, qty=1, rate=40, warehouse=self.warehouse)
		dn = create_delivery_note(
			item_code=item,
			warehouse=self.warehouse,
			company=self.company,
			customer=self.customer,
			qty=1,
			rate=100,
			use_serial_batch_fields=1,
			serial_no="DNCS-INVALID-SERIAL-DOES-NOT-EXIST",
			do_not_submit=True,
		)
		before = _dn_snapshot(dn.name, item, self.warehouse)
		result = validate_delivery_note_submission(dn.name)
		self.assertFalse(result["ok"], f"expected failure; se={getattr(se, 'name', se)}")
		joined = " ".join(e["message"] for e in result["errors"]).lower()
		self.assertTrue(
			"serial" in joined or "not available" in joined or "exist" in joined,
			result["errors"],
		)
		self._assert_no_side_effects(before, dn.name, item, self.warehouse)

	def test_validation_does_not_change_status_or_ledgers(self):
		dn = self._draft_dn(qty=1, rate=100)
		before = _dn_snapshot(dn.name, self.item, self.warehouse)
		result = validate_delivery_note_submission(dn.name)
		self.assertTrue(result["ok"], result)
		self.assertTrue(result["side_effects_clean"])
		self._assert_no_side_effects(before, dn.name, self.item, self.warehouse)
		dn_after = frappe.get_doc("Delivery Note", dn.name)
		self.assertEqual(dn_after.docstatus, 0)
		self.assertEqual(flt(dn_after.items[0].qty), 1)

	def test_rejects_non_draft(self):
		dn = self._draft_dn(qty=1, rate=100)
		dn.submit()
		self.assertEqual(dn.docstatus, 1)
		with self.assertRaises(frappe.ValidationError):
			validate_delivery_note_before_submit(name=dn.name)

	def test_rejects_missing_name(self):
		with self.assertRaises(frappe.ValidationError):
			validate_delivery_note_before_submit(name="")

	def test_doctype_js_hook_registered(self):
		hooks = frappe.get_hooks("doctype_js") or {}
		dn_js = hooks.get("Delivery Note") or []
		joined = dn_js if isinstance(dn_js, str) else " ".join(dn_js)
		self.assertIn("delivery_note_check_submission.js", joined)

	def test_success_message_includes_caveat_and_skipped_checks(self):
		dn = self._draft_dn(qty=1, rate=100)
		result = validate_delivery_note_submission(dn.name)
		self.assertTrue(result["ok"], result)
		self.assertIn("repost_future_sle_and_gle", result["skipped_checks"])
		self.assertIn("remain unchanged", result["caveat"])

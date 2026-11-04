# Copyright (c) 2026, ERPNext Extensions contributors
"""Delivery Note Check Submission (v5.6.34) — strict read-only architecture."""

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
from erpnext_extensions.stock_extensions.delivery_note_check_submission.write_guard import (
	ReadOnlyViolation,
	enforce_read_only,
	is_mutating_sql,
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
	frappe.cache.delete_value("singles_dict:Stock Settings")
	if hasattr(frappe.local, "document_cache"):
		frappe.local.document_cache.pop(("Stock Settings", "Stock Settings"), None)


def _make_item(company: str, properties: dict | None = None) -> str:
	item = ensure_test_item(company, prefix="DNCS34")
	doc = frappe.get_doc("Item", item)
	doc.is_sales_item = 1
	doc.is_stock_item = 1
	if properties:
		doc.update(properties)
	doc.save(ignore_permissions=True)
	return doc.name


def create_delivery_note(**args):
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


def _bin_row(item_code: str, warehouse: str) -> dict:
	row = frappe.db.get_value(
		"Bin",
		{"item_code": item_code, "warehouse": warehouse},
		["name", "actual_qty", "reserved_qty", "reserved_stock", "modified"],
		as_dict=True,
	)
	return row or {"name": None, "actual_qty": 0, "reserved_qty": 0, "reserved_stock": 0, "modified": None}


def _full_fingerprint(dn_name: str, item_code: str, warehouse: str) -> dict:
	dn = frappe.db.get_value(
		"Delivery Note", dn_name, ["name", "docstatus", "modified", "status"], as_dict=True
	)
	items = frappe.get_all(
		"Delivery Note Item",
		filters={"parent": dn_name},
		fields=["name", "qty", "warehouse", "item_code", "modified"],
	)
	return {
		"dn": dn,
		"items": items,
		"bin": _bin_row(item_code, warehouse),
		"sle": frappe.db.count(
			"Stock Ledger Entry", {"voucher_type": "Delivery Note", "voucher_no": dn_name}
		),
		"gle": frappe.db.count("GL Entry", {"voucher_type": "Delivery Note", "voucher_no": dn_name}),
		"sabb": frappe.db.count(
			"Serial and Batch Bundle", {"voucher_type": "Delivery Note", "voucher_no": dn_name}
		),
	}


class TestWriteGuard(unittest.TestCase):
	def test_mutating_sql_detection(self):
		self.assertTrue(is_mutating_sql("INSERT INTO tabX (a) VALUES (1)"))
		self.assertTrue(is_mutating_sql("UPDATE tabX SET a=1"))
		self.assertTrue(is_mutating_sql("DELETE FROM tabX"))
		self.assertTrue(is_mutating_sql("SAVEPOINT sp1"))
		self.assertTrue(is_mutating_sql("COMMIT"))
		self.assertFalse(is_mutating_sql("SELECT name FROM tabItem"))
		self.assertFalse(is_mutating_sql("  select 1"))

	def test_enforce_read_only_blocks_writes(self):
		with enforce_read_only():
			with self.assertRaises(ReadOnlyViolation):
				frappe.db.sql("UPDATE `tabDocType` SET modified=modified WHERE name=%s LIMIT 0", "Item")
			with self.assertRaises(ReadOnlyViolation):
				frappe.db.set_value("User", "Administrator", "first_name", "Administrator")
			with self.assertRaises(ReadOnlyViolation):
				frappe.enqueue(lambda: None)
			# Reads still work.
			self.assertTrue(frappe.db.get_value("User", "Administrator", "name"))


class TestDeliveryNoteCheckSubmissionV5634(unittest.TestCase):
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

	def _assert_immutable(self, before: dict, dn_name: str, item_code: str, warehouse: str):
		after = _full_fingerprint(dn_name, item_code, warehouse)
		self.assertEqual(frappe.as_json(after), frappe.as_json(before))

	def test_architecture_is_strict_read_only(self):
		dn = self._draft_dn(qty=1, rate=100)
		result = validate_delivery_note_submission(dn.name)
		self.assertEqual(result["architecture"], "strict_read_only")
		self.assertFalse(result["mutated"])
		self.assertNotIn("savepoint", (result.get("architecture") or "").lower())

	def test_write_without_submit_can_validate(self):
		dn = self._draft_dn(qty=1, rate=100)
		before = _full_fingerprint(dn.name, self.item, self.warehouse)
		real_has_permission = frappe.has_permission

		def _write_no_submit(doctype, ptype="read", doc=None, user=None, **kwargs):
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
				result = validate_delivery_note_before_submit(name=dn.name)

		self.assertTrue(result["ok"], result)
		self._assert_immutable(before, dn.name, self.item, self.warehouse)

	def test_insufficient_permission_rejected(self):
		dn = self._draft_dn(qty=1, rate=100)
		frappe.set_user(NO_PERM_USER)
		with self.assertRaises(frappe.PermissionError):
			validate_delivery_note_before_submit(name=dn.name)

	def test_valid_delivery_note_success(self):
		dn = self._draft_dn(qty=1, rate=100)
		before = _full_fingerprint(dn.name, self.item, self.warehouse)
		result = validate_delivery_note_submission(dn.name)
		self.assertTrue(result["ok"], result)
		self.assertTrue(result["ready_for_submission"])
		self.assertEqual(result["errors"], [])
		self.assertTrue(result["caveat"])
		self.assertTrue(any(l.get("code") == "sle_gle_simulation" for l in result["limitations"]))
		self._assert_immutable(before, dn.name, self.item, self.warehouse)

	def test_insufficient_stock(self):
		item = _make_item(self.company)
		frappe.db.set_value("Item", item, "allow_negative_stock", 0)
		submit_material_receipt(self.company, item, qty=1, rate=50, warehouse=self.warehouse)
		_disable_negative_stock()
		dn = self._draft_dn(item_code=item, qty=50, rate=100)
		before = _full_fingerprint(dn.name, item, self.warehouse)
		result = validate_delivery_note_submission(dn.name)
		self.assertFalse(result["ok"])
		joined = " ".join(e["message"] for e in result["errors"]).lower()
		self.assertTrue(
			any(t in joined for t in ("insufficient", "needed", "available", "stock")),
			result["errors"],
		)
		self.assertTrue(any(e["exc_type"] == "NegativeStockError" for e in result["errors"]), result)
		self._assert_immutable(before, dn.name, item, self.warehouse)

	def test_invalid_warehouse(self):
		dn = self._draft_dn(qty=1, rate=100)
		frappe.db.set_value("Delivery Note Item", dn.items[0].name, "warehouse", "")
		dn.reload()
		before = _full_fingerprint(dn.name, self.item, self.warehouse)
		result = validate_delivery_note_submission(dn.name)
		self.assertFalse(result["ok"])
		joined = " ".join(e["message"] for e in result["errors"]).lower()
		self.assertIn("warehouse", joined)
		self._assert_immutable(before, dn.name, self.item, self.warehouse)

	def test_missing_batch(self):
		item = _make_item(
			self.company,
			{
				"has_batch_no": 1,
				"create_new_batch": 1,
				"batch_number_series": f"DNCS34-BAT-{random_string(4)}-.#####",
			},
		)
		dn = self._draft_dn(item_code=item, qty=1, rate=100)
		before = _full_fingerprint(dn.name, item, self.warehouse)
		result = validate_delivery_note_submission(dn.name)
		self.assertFalse(result["ok"])
		joined = " ".join(e["message"] for e in result["errors"]).lower()
		# Either hard error or blocking auto-pick limitation
		if result["errors"]:
			self.assertTrue("batch" in joined or "serial" in joined, result["errors"])
		else:
			self.assertTrue(
				any(l.get("severity") == "blocking" for l in result["limitations"]),
				result["limitations"],
			)
		self._assert_immutable(before, dn.name, item, self.warehouse)

	def test_invalid_serial(self):
		item = _make_item(
			self.company,
			{
				"has_serial_no": 1,
				"serial_no_series": f"DNCS34-SN-{random_string(4)}-.#####",
			},
		)
		submit_material_receipt(self.company, item, qty=1, rate=40, warehouse=self.warehouse)
		dn = create_delivery_note(
			item_code=item,
			warehouse=self.warehouse,
			company=self.company,
			customer=self.customer,
			qty=1,
			rate=100,
			use_serial_batch_fields=1,
			serial_no="DNCS34-INVALID-SERIAL-DOES-NOT-EXIST",
			do_not_submit=True,
		)
		before = _full_fingerprint(dn.name, item, self.warehouse)
		result = validate_delivery_note_submission(dn.name)
		self.assertFalse(result["ok"])
		joined = " ".join(e["message"] for e in result["errors"]).lower()
		self.assertTrue(
			"serial" in joined or "not available" in joined or "exist" in joined,
			result["errors"],
		)
		self._assert_immutable(before, dn.name, item, self.warehouse)

	def test_accounting_missing_expense_account(self):
		dn = self._draft_dn(qty=1, rate=100)
		frappe.db.set_value("Delivery Note Item", dn.items[0].name, "expense_account", "")
		dn.reload()
		before = _full_fingerprint(dn.name, self.item, self.warehouse)
		result = validate_delivery_note_submission(dn.name)
		# May fail earlier on mandatory field or accounting readiness
		self.assertFalse(result["ok"])
		self._assert_immutable(before, dn.name, self.item, self.warehouse)

	def test_no_mutation_across_related_doctypes(self):
		dn = self._draft_dn(qty=1, rate=100)
		before = _full_fingerprint(dn.name, self.item, self.warehouse)
		# Also capture global SLE/GLE counts for this item to detect unrelated writes.
		sle_before = frappe.db.count("Stock Ledger Entry", {"item_code": self.item})
		result = validate_delivery_note_submission(dn.name)
		self.assertTrue(result["ok"], result)
		self._assert_immutable(before, dn.name, self.item, self.warehouse)
		self.assertEqual(frappe.db.count("Stock Ledger Entry", {"item_code": self.item}), sle_before)

	def test_rejects_non_draft(self):
		dn = self._draft_dn(qty=1, rate=100)
		dn.submit()
		with self.assertRaises(frappe.ValidationError):
			validate_delivery_note_before_submit(name=dn.name)

	def test_doctype_js_hook_registered(self):
		hooks = frappe.get_hooks("doctype_js") or {}
		dn_js = hooks.get("Delivery Note") or []
		joined = dn_js if isinstance(dn_js, str) else " ".join(dn_js)
		self.assertIn("delivery_note_check_submission.js", joined)

	def test_write_guard_trips_on_injected_mutation(self):
		"""If a called validator tried to write, Check Submission must hard-fail."""
		dn = self._draft_dn(qty=1, rate=100)

		def _evil(_doc):
			frappe.db.sql(
				"UPDATE `tabDelivery Note` SET title=title WHERE name=%s",
				dn.name,
			)

		with mock.patch(
			"erpnext_extensions.stock_extensions.delivery_note_check_submission.validator._run_category_a_erpnext",
			side_effect=_evil,
		):
			with self.assertRaises(ReadOnlyViolation):
				validate_delivery_note_submission(dn.name)

		# Document must remain draft / unmodified by the blocked write.
		self.assertEqual(cint(frappe.db.get_value("Delivery Note", dn.name, "docstatus")), 0)

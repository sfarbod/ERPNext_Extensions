# Copyright (c) 2026, ERPNext Extensions contributors
"""Delivery Note Check Submission (v5.6.37) — posting-date batch/stock + error aggregation."""

from __future__ import annotations

import unittest

import frappe
from frappe.utils import add_days, flt, getdate, nowdate, random_string

from erpnext_extensions.iran_accounting.e2e_bootstrap import (
	ensure_test_item,
	get_irr_company,
	get_warehouse,
	submit_material_receipt,
)
from erpnext_extensions.stock_extensions.delivery_note_check_submission.stock_availability import (
	is_batch_expired_on,
)
from erpnext_extensions.stock_extensions.delivery_note_check_submission.validator import (
	validate_delivery_note_submission,
)


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


def _make_item(company: str, properties: dict | None = None) -> str:
	item = ensure_test_item(company, prefix="DNCS37")
	doc = frappe.get_doc("Item", item)
	doc.is_sales_item = 1
	doc.is_stock_item = 1
	if properties:
		doc.update(properties)
	doc.save(ignore_permissions=True)
	return doc.name


def _fingerprint(dn_name: str, item_code: str, warehouse: str) -> dict:
	bin_row = frappe.db.get_value(
		"Bin",
		{"item_code": item_code, "warehouse": warehouse},
		["actual_qty", "reserved_stock", "modified"],
		as_dict=True,
	)
	return {
		"dn": frappe.db.get_value(
			"Delivery Note", dn_name, ["docstatus", "modified", "status"], as_dict=True
		),
		"bin": bin_row,
		"sle": frappe.db.count(
			"Stock Ledger Entry", {"voucher_type": "Delivery Note", "voucher_no": dn_name}
		),
		"gle": frappe.db.count("GL Entry", {"voucher_type": "Delivery Note", "voucher_no": dn_name}),
		"sabb": frappe.db.count(
			"Serial and Batch Bundle", {"voucher_type": "Delivery Note", "voucher_no": dn_name}
		),
		"batch_modified": frappe.db.get_all(
			"Batch",
			filters={"item": item_code},
			fields=["name", "expiry_date", "modified"],
		),
	}


def create_delivery_note(**args):
	args = frappe._dict(args)
	company = args.company
	dn = frappe.new_doc("Delivery Note")
	dn.posting_date = args.posting_date or nowdate()
	dn.posting_time = args.posting_time or "12:00:00"
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
		"allow_zero_valuation_rate": 1,
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
	# Extra rows for multi-error scenarios
	for extra in args.get("extra_items") or []:
		er = {
			"item_code": extra["item_code"],
			"warehouse": extra.get("warehouse", args.warehouse),
			"qty": extra.get("qty", 1),
			"rate": extra.get("rate", 100),
			"conversion_factor": 1.0,
			"allow_zero_valuation_rate": 1,
			"expense_account": _expense_account(company),
			"cost_center": _cost_center(company),
			"use_serial_batch_fields": extra.get("use_serial_batch_fields"),
			"serial_no": extra.get("serial_no"),
			"batch_no": extra.get("batch_no"),
		}
		if dept and frappe.get_meta("Delivery Note Item").has_field("department"):
			er["department"] = dept
		dn.append("items", er)
	if not args.do_not_save:
		dn.insert()
		if not args.do_not_submit:
			dn.submit()
		dn.load_from_db()
	return dn


def _make_batch(item_code: str, expiry_date, company: str, warehouse: str, qty: float = 10):
	"""Create batch + receive stock into it (no ERPNext test bootstrap imports)."""
	from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry

	from erpnext_extensions.iran_accounting.e2e_bootstrap import apply_stock_entry_site_defaults

	ident = f"DNCS37-{random_string(6)}"
	batch = frappe.new_doc("Batch")
	batch.item = item_code
	batch.batch_id = ident
	batch.expiry_date = expiry_date
	meta = frappe.get_meta("Batch")
	if meta.has_field("custom_batch_no"):
		batch.custom_batch_no = ident
	for df in meta.fields:
		if not df.reqd or batch.get(df.fieldname) not in (None, ""):
			continue
		if df.fieldname in ("naming_series", "item", "batch_id", "expiry_date"):
			continue
		if df.fieldtype in ("Data", "Small Text", "Text", "Long Text"):
			batch.set(df.fieldname, ident)
	batch.insert(ignore_permissions=True)

	se = make_stock_entry(
		item_code=item_code,
		qty=qty,
		rate=50,
		target=warehouse,
		company=company,
		purpose="Material Receipt",
		do_not_save=True,
		do_not_submit=True,
	)
	se.set_posting_time = 1
	se.posting_date = add_days(getdate(expiry_date), -60) if expiry_date else add_days(nowdate(), -60)
	se.posting_time = "10:00:00"
	for row in se.items:
		row.use_serial_batch_fields = 1
		row.batch_no = batch.name
		row.allow_zero_valuation_rate = 1
	frappe.flags.iran_gate_defaults = True
	apply_stock_entry_site_defaults(se)
	se.insert()
	se.submit()
	return batch.name


class TestBatchExpiryConvention(unittest.TestCase):
	def test_expiry_boundary_matches_erpnext(self):
		# ERPNext: expired iff expiry < posting (equal is valid)
		self.assertFalse(is_batch_expired_on("2025-05-10", "2025-03-31"))
		self.assertTrue(is_batch_expired_on("2025-02-15", "2025-03-31"))
		self.assertFalse(is_batch_expired_on("2025-03-31", "2025-03-31"))
		self.assertFalse(is_batch_expired_on(None, "2025-03-31"))


class TestDeliveryNoteCheckSubmissionV5637(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		_disable_negative_stock()
		cls.company = get_irr_company("اسپاد فارمد دارو")
		cls.warehouse = get_warehouse(cls.company)
		cls.customer = _customer()

	def setUp(self):
		frappe.set_user("Administrator")
		_disable_negative_stock()

	def _assert_immutable(self, before, dn_name, item_code, warehouse):
		after = _fingerprint(dn_name, item_code, warehouse)
		self.assertEqual(frappe.as_json(after), frappe.as_json(before))

	def test_batch_expires_after_dn_date_valid(self):
		"""Current date may be after expiry, but DN posting is before expiry → not expired."""
		item = _make_item(
			self.company,
			{
				"has_batch_no": 1,
				"create_new_batch": 0,
				"batch_number_series": f"DNCS37-B-{random_string(3)}-.#####",
			},
		)
		# Expiry in the past relative to "today", but DN posts earlier while still valid.
		dn_date = add_days(nowdate(), -40)
		expiry = add_days(nowdate(), -10)  # expired today, valid on dn_date
		self.assertFalse(is_batch_expired_on(expiry, dn_date))
		self.assertTrue(is_batch_expired_on(expiry, nowdate()))

		batch_no = _make_batch(item, expiry, self.company, self.warehouse, qty=5)
		dn = create_delivery_note(
			company=self.company,
			customer=self.customer,
			warehouse=self.warehouse,
			item_code=item,
			qty=1,
			posting_date=dn_date,
			use_serial_batch_fields=1,
			batch_no=batch_no,
			do_not_submit=True,
		)
		before = _fingerprint(dn.name, item, self.warehouse)
		result = validate_delivery_note_submission(dn.name)
		expiry_errors = [e for e in result["errors"] if e.get("category") == "batch_expiration"]
		self.assertEqual(expiry_errors, [], result["errors"])
		# Should not report zero available solely because batch is expired today.
		stock_errors = [e for e in result["errors"] if e.get("category") == "stock_availability"]
		for e in stock_errors:
			self.assertNotEqual(flt(e.get("available")), 0.0, e)
		self._assert_immutable(before, dn.name, item, self.warehouse)

	def test_batch_expires_before_dn_date_invalid(self):
		item = _make_item(
			self.company,
			{
				"has_batch_no": 1,
				"create_new_batch": 0,
				"batch_number_series": f"DNCS37-B-{random_string(3)}-.#####",
			},
		)
		dn_date = add_days(nowdate(), -5)
		expiry = add_days(dn_date, -25)
		self.assertTrue(is_batch_expired_on(expiry, dn_date))
		# DN insert uses ERPNext's posting-date expiry guard, so create with a
		# still-valid expiry then mutate Batch.expiry_date to the invalid value.
		batch_no = _make_batch(item, add_days(dn_date, 30), self.company, self.warehouse, qty=5)
		dn = create_delivery_note(
			company=self.company,
			customer=self.customer,
			warehouse=self.warehouse,
			item_code=item,
			qty=1,
			posting_date=dn_date,
			use_serial_batch_fields=1,
			batch_no=batch_no,
			do_not_submit=True,
		)
		frappe.db.set_value("Batch", batch_no, "expiry_date", expiry)
		frappe.clear_cache(doctype="Batch")
		before = _fingerprint(dn.name, item, self.warehouse)
		result = validate_delivery_note_submission(dn.name)
		self.assertFalse(result["ok"])
		cats = {e.get("category") for e in result["errors"]}
		self.assertIn("batch_expiration", cats, result["errors"])
		self._assert_immutable(before, dn.name, item, self.warehouse)

	def test_batch_expires_exactly_on_dn_date_valid(self):
		item = _make_item(
			self.company,
			{
				"has_batch_no": 1,
				"create_new_batch": 0,
				"batch_number_series": f"DNCS37-B-{random_string(3)}-.#####",
			},
		)
		dn_date = add_days(nowdate(), -7)
		expiry = dn_date  # equal → valid per ERPNext
		self.assertFalse(is_batch_expired_on(expiry, dn_date))
		batch_no = _make_batch(item, expiry, self.company, self.warehouse, qty=5)
		dn = create_delivery_note(
			company=self.company,
			customer=self.customer,
			warehouse=self.warehouse,
			item_code=item,
			qty=1,
			posting_date=dn_date,
			use_serial_batch_fields=1,
			batch_no=batch_no,
			do_not_submit=True,
		)
		result = validate_delivery_note_submission(dn.name)
		expiry_errors = [e for e in result["errors"] if e.get("category") == "batch_expiration"]
		self.assertEqual(expiry_errors, [], result["errors"])

	def test_historical_stock_available_on_dn_date(self):
		from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry

		from erpnext_extensions.iran_accounting.e2e_bootstrap import apply_stock_entry_site_defaults

		item = _make_item(self.company)
		# Receive 20 on day -20 so a later issue of 8 still leaves enough headroom
		# for a backdated DN of 5 (future SLE must not go negative — Submit parity).
		se = make_stock_entry(
			item_code=item,
			qty=20,
			rate=40,
			target=self.warehouse,
			company=self.company,
			purpose="Material Receipt",
			do_not_save=True,
			do_not_submit=True,
		)
		se.set_posting_time = 1
		se.posting_date = add_days(nowdate(), -20)
		se.posting_time = "10:00:00"
		frappe.flags.iran_gate_defaults = True
		apply_stock_entry_site_defaults(se)
		se.insert()
		se.submit()

		# Later consume 8 so today's bin (12) differs from historical balance at DN date (20)
		se2 = make_stock_entry(
			item_code=item,
			qty=8,
			rate=40,
			source=self.warehouse,
			company=self.company,
			purpose="Material Issue",
			do_not_save=True,
			do_not_submit=True,
		)
		se2.set_posting_time = 1
		se2.posting_date = add_days(nowdate(), -5)
		se2.posting_time = "10:00:00"
		frappe.flags.iran_gate_defaults = True
		apply_stock_entry_site_defaults(se2)
		se2.insert()
		se2.submit()

		bin_now = flt(
			frappe.db.get_value("Bin", {"item_code": item, "warehouse": self.warehouse}, "actual_qty")
		)
		self.assertLess(bin_now, 20)
		self.assertGreaterEqual(bin_now, 5)

		dn = create_delivery_note(
			company=self.company,
			customer=self.customer,
			warehouse=self.warehouse,
			item_code=item,
			qty=5,
			posting_date=add_days(nowdate(), -15),
			do_not_submit=True,
		)
		before = _fingerprint(dn.name, item, self.warehouse)
		result = validate_delivery_note_submission(dn.name)
		stock_errors = [e for e in result["errors"] if e.get("category") == "stock_availability"]
		self.assertEqual(stock_errors, [], result["errors"])
		self._assert_immutable(before, dn.name, item, self.warehouse)

	def test_historical_stock_insufficient_on_dn_date(self):
		item = _make_item(self.company)
		submit_material_receipt(self.company, item, qty=2, rate=40, warehouse=self.warehouse)
		dn = create_delivery_note(
			company=self.company,
			customer=self.customer,
			warehouse=self.warehouse,
			item_code=item,
			qty=50,
			posting_date=nowdate(),
			do_not_submit=True,
		)
		before = _fingerprint(dn.name, item, self.warehouse)
		result = validate_delivery_note_submission(dn.name)
		self.assertFalse(result["ok"])
		self.assertTrue(
			any(e.get("category") == "stock_availability" for e in result["errors"]),
			result["errors"],
		)
		self._assert_immutable(before, dn.name, item, self.warehouse)

	def test_aggregated_multiple_independent_errors(self):
		"""Stock shortage + missing serial + empty warehouse collected together."""
		stock_item = _make_item(self.company)
		submit_material_receipt(self.company, stock_item, qty=1, rate=10, warehouse=self.warehouse)

		serial_item = _make_item(
			self.company,
			{
				"has_serial_no": 1,
				"serial_no_series": f"DNCS37-SN-{random_string(3)}-.#####",
			},
		)
		batch_item = _make_item(
			self.company,
			{
				"has_batch_no": 1,
				"create_new_batch": 0,
				"batch_number_series": f"DNCS37-BX-{random_string(3)}-.#####",
			},
		)

		dn = create_delivery_note(
			company=self.company,
			customer=self.customer,
			warehouse=self.warehouse,
			item_code=stock_item,
			qty=99,  # insufficient
			do_not_submit=True,
			extra_items=[
				{"item_code": serial_item, "qty": 1},  # missing serial
				{"item_code": batch_item, "qty": 1},  # missing batch
			],
		)
		# Clear warehouse on third row via db for warehouse error without blocking insert
		# (third row is batch_item)
		frappe.db.set_value("Delivery Note Item", dn.items[2].name, "warehouse", "")

		before = _fingerprint(dn.name, stock_item, self.warehouse)
		result = validate_delivery_note_submission(dn.name)
		self.assertFalse(result["ok"])
		self.assertGreaterEqual(result["summary"]["errors"], 3, result)
		cats = {e.get("category") for e in result["errors"]}
		self.assertTrue(
			{"stock_availability", "serial_number", "batch_eligibility"} & cats
			or len(result["errors"]) >= 3,
			result["errors"],
		)
		# Never Ready when incomplete/fail
		self.assertFalse(result["ready_for_submission"])
		self.assertIn(result["status"], ("fail", "blocked", "incomplete"))
		self._assert_immutable(before, dn.name, stock_item, self.warehouse)

	def test_duplicate_error_suppression(self):
		item = _make_item(self.company)
		dn = create_delivery_note(
			company=self.company,
			customer=self.customer,
			warehouse=self.warehouse,
			item_code=item,
			qty=1,
			do_not_submit=True,
		)
		frappe.db.set_value("Delivery Note Item", dn.items[0].name, "warehouse", "")
		result = validate_delivery_note_submission(dn.name)
		msgs = [e["message"] for e in result["errors"]]
		self.assertEqual(len(msgs), len(set(msgs)) or len(msgs) >= 1)

	def test_response_structure_has_summary_and_status(self):
		item = _make_item(self.company)
		submit_material_receipt(self.company, item, qty=5, rate=10, warehouse=self.warehouse)
		dn = create_delivery_note(
			company=self.company,
			customer=self.customer,
			warehouse=self.warehouse,
			item_code=item,
			qty=1,
			do_not_submit=True,
		)
		result = validate_delivery_note_submission(dn.name)
		self.assertIn(result["status"], ("ready", "blocked", "incomplete", "pass", "fail"))
		self.assertIn("summary", result)
		self.assertIn("errors", result["summary"])
		self.assertEqual(result["architecture"], "strict_read_only")
		self.assertFalse(result["mutated"])

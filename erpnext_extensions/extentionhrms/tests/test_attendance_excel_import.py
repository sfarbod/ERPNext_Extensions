# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Tests for Attendance Details Excel bulk import helpers."""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, getdate, nowdate

import os

from erpnext_extensions.extentionhrms.attendance_excel_import import (
	_normalize_personnel_code,
	_reapply_excel_authoritative_fields,
	approved_overtime_hours_from_components,
	find_existing_attendance,
	get_personnel_code_index,
	import_attendance_details,
	preload_attendance_import_context,
)
from erpnext_extensions.extentionhrms.attendance_excel_import_setup import (
	ensure_attendance_excel_import_setup,
)


class TestAttendanceExcelImport(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		ensure_attendance_excel_import_setup()
		frappe.db.commit()

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		self.employees = []
		self.users = []
		self.attendance = []

	def tearDown(self):
		frappe.set_user("Administrator")
		for name in self.attendance:
			if frappe.db.exists("Attendance", name):
				doc = frappe.get_doc("Attendance", name)
				if int(doc.docstatus) == 1:
					doc.cancel()
				frappe.delete_doc("Attendance", name, force=1, ignore_permissions=True)
		for name in self.employees:
			if frappe.db.exists("Employee", name):
				frappe.db.sql("delete from `tabAttendance` where employee=%s", name)
				frappe.delete_doc("Employee", name, force=1, ignore_permissions=True)
		for name in self.users:
			if name != "Administrator" and frappe.db.exists("User", name):
				frappe.delete_doc("User", name, force=1, ignore_permissions=True)
		frappe.db.commit()
		super().tearDown()

	def _employee(self, personal_code="77001"):
		template = frappe.db.get_value(
			"Employee",
			{"status": "Active", "company": ["is", "set"]},
			["company", "department", "payroll_cost_center"],
			as_dict=True,
		)
		doc = frappe.get_doc(
			{
				"doctype": "Employee",
				"naming_series": "HR-EMP-",
				"first_name": "AttExcel",
				"gender": "Male",
				"date_of_birth": "1990-01-01",
				"date_of_joining": "2020-01-01",
				"status": "Active",
				"company": template.company,
				"department": template.department,
				"payroll_cost_center": template.payroll_cost_center,
				"custom_personal_code": personal_code,
			}
		)
		doc.insert()
		self.employees.append(doc.name)
		return doc.name

	def _user(self, roles):
		email = f"att-excel-{frappe.generate_hash(length=6)}@example.com"
		doc = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": "AttExcel",
				"send_welcome_email": 0,
				"roles": [{"role": role} for role in roles],
			}
		)
		doc.insert(ignore_permissions=True)
		self.users.append(doc.name)
		return doc.name

	def _attendance(self, employee, day, status="Present", submit=True):
		doc = frappe.get_doc(
			{
				"doctype": "Attendance",
				"employee": employee,
				"attendance_date": day,
				"status": status,
				"company": frappe.db.get_value("Employee", employee, "company"),
			}
		)
		doc.insert()
		if submit:
			doc.submit()
		self.attendance.append(doc.name)
		return doc

	def test_find_existing_attendance_reports_draft_and_submitted(self):
		employee = self._employee("77011")
		day = add_days(getdate(nowdate()), -10)
		draft = self._attendance(employee, day, submit=False)
		submitted = self._attendance(employee, add_days(day, 1), submit=True)
		before = frappe.db.count("Attendance")
		result = find_existing_attendance(
			[
				{"employee": employee, "attendance_date": str(day)},
				{"employee": employee, "attendance_date": str(add_days(day, 1))},
				{"employee": employee, "attendance_date": str(add_days(day, 2))},
			]
		)
		names = {row["name"] for row in result["rows"]}
		self.assertIn(draft.name, names)
		self.assertIn(submitted.name, names)
		self.assertEqual(frappe.db.count("Attendance"), before)

	def test_find_existing_ignores_cancelled(self):
		employee = self._employee("77012")
		day = add_days(getdate(nowdate()), -11)
		doc = self._attendance(employee, day, submit=True)
		doc.cancel()
		result = find_existing_attendance(
			[{"employee": employee, "attendance_date": str(day)}]
		)
		self.assertEqual(result["rows"], [])

	def test_unauthorized_user_cannot_lookup_or_import(self):
		employee = self._employee("77013")
		user = self._user(["Employee"])
		frappe.set_user(user)
		with self.assertRaises(frappe.PermissionError):
			find_existing_attendance(
				[{"employee": employee, "attendance_date": str(nowdate())}]
			)
		with self.assertRaises(frappe.PermissionError):
			import_attendance_details([])
		frappe.set_user("Administrator")

	def test_import_create_submits_attendance(self):
		employee = self._employee("77014")
		day = add_days(getdate(nowdate()), -12)
		company = frappe.db.get_value("Employee", employee, "company")
		before_checkin = frappe.db.count("Employee Checkin")
		result = import_attendance_details(
			[
				{
					"employee": employee,
					"attendance_date": str(day),
					"company": company,
					"mode": "create",
					"values": {
						"status": "Present",
						"working_hours": 8,
						"standard_working_hours": 8,
					},
					"row": 1,
				}
			]
		)
		self.assertEqual(len(result["created"]), 1)
		self.assertEqual(result["failed"], [])
		name = result["created"][0]["name"]
		self.attendance.append(name)
		doc = frappe.get_doc("Attendance", name)
		self.assertEqual(int(doc.docstatus), 1)
		self.assertEqual(doc.status, "Present")
		self.assertEqual(frappe.db.count("Employee Checkin"), before_checkin)

	def test_import_is_idempotent_for_existing_day(self):
		employee = self._employee("77015")
		day = add_days(getdate(nowdate()), -13)
		company = frappe.db.get_value("Employee", employee, "company")
		first = import_attendance_details(
			[
				{
					"employee": employee,
					"attendance_date": str(day),
					"company": company,
					"mode": "create",
					"values": {"status": "Present", "standard_working_hours": 8, "working_hours": 8},
					"row": 1,
				}
			]
		)
		self.attendance.append(first["created"][0]["name"])
		second = import_attendance_details(
			[
				{
					"employee": employee,
					"attendance_date": str(day),
					"company": company,
					"mode": "update_submitted",
					"name": first["created"][0]["name"],
					"values": {
						"status": "Present",
						"standard_working_hours": 8,
						"working_hours": 7.5,
						"custom_approved_overtime_hours": 0.5,
					},
					"row": 1,
				}
			]
		)
		self.assertEqual(len(second["updated"]), 1)
		self.assertEqual(frappe.db.count("Attendance", {"employee": employee, "attendance_date": day, "docstatus": 1}), 1)

	def test_preload_context_returns_employee_company(self):
		employee = self._employee("77016")
		ctx = preload_attendance_import_context([employee])
		self.assertIn(employee, ctx["employees"])
		self.assertTrue(ctx["employees"][employee]["company"])

	def test_client_script_sync_is_idempotent(self):
		first = ensure_attendance_excel_import_setup()
		second = ensure_attendance_excel_import_setup()
		self.assertIn(first["client_script"], ("created", "updated", "unchanged"))
		self.assertEqual(second["client_script"], "unchanged")
		script = frappe.db.get_value("Client Script", "Employee Attendance Excel Import", "script")
		self.assertIn("V2.0.3", script)
		self.assertIn("import_attendance_details", script)
		self.assertIn("get_personnel_code_index", script)
		self.assertIn("/assets/erpnext_extensions/js/lib/xlsx.full.min.js", script)
		self.assertNotIn("cdn.jsdelivr.net", script)
		# Refresh Preview must not use btn-primary (collides with dialog primary on Frappe 16).
		self.assertIn("btn-default btn-sm espad-refresh", script)
		self.assertNotIn("btn-primary btn-sm espad-refresh", script)
		# Progress stays inside the main dialog — no nested frappe.show_progress.
		self.assertIn("data-espad-att-progress", script)
		self.assertNotIn("frappe.show_progress", script)
		self.assertNotIn("Attendance Import Result", script)
		# Approved OT must map explicitly to custom_approved_overtime_hours.
		self.assertIn('setHours(doc, "Approved Overtime Hours", r.approvedOT', script)
		self.assertIn("nightOTApproved", script)
		self.assertIn("CONFIG.approvedOT", script)

	def test_sheetjs_asset_is_served_locally(self):
		path = os.path.join(
			frappe.get_app_path("erpnext_extensions"),
			"public",
			"js",
			"lib",
			"xlsx.full.min.js",
		)
		self.assertTrue(os.path.isfile(path), "vendored SheetJS missing: " + path)
		self.assertGreater(os.path.getsize(path), 100_000)

	def test_normalize_personnel_code_strips_zeros_and_persian_digits(self):
		self.assertEqual(_normalize_personnel_code("000025"), "25")
		self.assertEqual(_normalize_personnel_code("        ۲۵"), "25")
		self.assertEqual(_normalize_personnel_code("۰۶۷"), "67")

	def test_personnel_code_index_includes_readable_employees(self):
		employee = self._employee("000771")
		result = get_personnel_code_index()
		self.assertIn("771", result["index"])
		self.assertIn(employee, result["index"]["771"])

	def test_unauthorized_user_cannot_get_personnel_index(self):
		user = self._user(["Employee"])
		frappe.set_user(user)
		with self.assertRaises(frappe.PermissionError):
			get_personnel_code_index()
		frappe.set_user("Administrator")

	def test_approved_overtime_hours_formula_matches_legacy_script(self):
		# 00:30 + 01:00 + 00:45 + 02:15 = 4.5 hours
		self.assertEqual(
			approved_overtime_hours_from_components(30, 60, 45, 135),
			4.5,
		)
		# single Normal Approved 01:30
		self.assertEqual(approved_overtime_hours_from_components(0, 0, 0, 90), 1.5)
		# zero
		self.assertEqual(approved_overtime_hours_from_components(0, 0, 0, 0), 0.0)
		# multi-component from mehr.xls row 115: 5 + 859 minutes
		self.assertEqual(approved_overtime_hours_from_components(5, 0, 0, 859), 14.4)
		# Night-approved minutes are not interchangeable with Normal-approved positionally,
		# but the sum is commutative — both paths equal 1.5 hours.
		self.assertEqual(approved_overtime_hours_from_components(90, 0, 0, 0), 1.5)
		self.assertEqual(approved_overtime_hours_from_components(0, 0, 0, 90), 1.5)

	def test_create_persists_approved_overtime_hours(self):
		employee = self._employee("77040")
		day = add_days(getdate(nowdate()), -20)
		company = frappe.db.get_value("Employee", employee, "company")
		expected = approved_overtime_hours_from_components(30, 60, 45, 135)
		result = import_attendance_details(
			[
				{
					"employee": employee,
					"attendance_date": str(day),
					"company": company,
					"mode": "create",
					"values": {
						"status": "Present",
						"working_hours": 8,
						"standard_working_hours": 8,
						"custom_approved_overtime_hours": expected,
						"custom_regular_work_hours": 1.25,
						"custom_calculation_details": (
							'{"source":"Attendance Excel Import",'
							'"approved_overtime_minutes":270}'
						),
					},
					"row": 1,
				}
			]
		)
		self.assertEqual(result["failed"], [])
		self.assertEqual(len(result["created"]), 1)
		name = result["created"][0]["name"]
		self.attendance.append(name)
		self.assertAlmostEqual(
			float(frappe.db.get_value("Attendance", name, "custom_approved_overtime_hours")),
			expected,
			places=5,
		)
		self.assertAlmostEqual(
			float(frappe.db.get_value("Attendance", name, "custom_regular_work_hours") or 0),
			1.25,
			places=5,
		)

	def test_submitted_update_persists_approved_overtime_hours(self):
		employee = self._employee("77041")
		day = add_days(getdate(nowdate()), -21)
		company = frappe.db.get_value("Employee", employee, "company")
		first = import_attendance_details(
			[
				{
					"employee": employee,
					"attendance_date": str(day),
					"company": company,
					"mode": "create",
					"values": {
						"status": "Present",
						"standard_working_hours": 8,
						"working_hours": 8,
						"custom_approved_overtime_hours": 0,
					},
					"row": 1,
				}
			]
		)
		name = first["created"][0]["name"]
		self.attendance.append(name)
		expected = approved_overtime_hours_from_components(0, 0, 0, 90)
		second = import_attendance_details(
			[
				{
					"employee": employee,
					"attendance_date": str(day),
					"mode": "update_submitted",
					"name": name,
					"values": {
						"status": "Present",
						"standard_working_hours": 8,
						"working_hours": 8,
						"custom_approved_overtime_hours": expected,
					},
					"row": 1,
				}
			]
		)
		self.assertEqual(len(second["updated"]), 1)
		self.assertAlmostEqual(
			float(frappe.db.get_value("Attendance", name, "custom_approved_overtime_hours")),
			expected,
			places=5,
		)

	def test_reapply_restores_approved_ot_after_post_submit_wipe(self):
		employee = self._employee("77042")
		day = add_days(getdate(nowdate()), -22)
		company = frappe.db.get_value("Employee", employee, "company")
		expected = approved_overtime_hours_from_components(5, 0, 0, 859)
		result = import_attendance_details(
			[
				{
					"employee": employee,
					"attendance_date": str(day),
					"company": company,
					"mode": "create",
					"values": {
						"status": "Present",
						"standard_working_hours": 8,
						"working_hours": 8,
						"custom_approved_overtime_hours": expected,
						"custom_calculation_details": (
							'{"source":"Attendance Excel Import",'
							'"approved_overtime_minutes":864}'
						),
					},
					"row": 115,
				}
			]
		)
		name = result["created"][0]["name"]
		self.attendance.append(name)
		# Simulate ESPAD engine wiping Approved OT after submit.
		frappe.db.set_value("Attendance", name, "custom_approved_overtime_hours", 0)
		self.assertEqual(float(frappe.db.get_value("Attendance", name, "custom_approved_overtime_hours")), 0.0)
		_reapply_excel_authoritative_fields(
			name,
			{
				"custom_approved_overtime_hours": expected,
				"custom_calculation_details": (
					'{"source":"Attendance Excel Import","approved_overtime_minutes":864}'
				),
			},
		)
		self.assertAlmostEqual(
			float(frappe.db.get_value("Attendance", name, "custom_approved_overtime_hours")),
			expected,
			places=5,
		)

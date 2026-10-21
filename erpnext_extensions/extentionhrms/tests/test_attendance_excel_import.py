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
	excel_status_from_absence,
	expected_gregorian_from_jalali_ymd,
	find_existing_attendance,
	get_personnel_code_index,
	import_attendance_details,
	minutes_to_hours,
	mission_absence_conflict,
	preload_attendance_import_context,
	unauthorized_normal_ot_hours,
	work_shortage_hours_from_components,
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
		self.assertIn("V2.0.8", script)
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
		# Excel-authoritative dual mapping: عادی غیرمجاز → both fields.
		self.assertIn("unauthorizedNormalOT", script)
		self.assertIn("custom_regular_work_hours", script)
		self.assertIn("custom_extra_presence_hours", script)
		self.assertIn("workShortage", script)
		self.assertIn('setHours(doc, "Approved Overtime Hours", r.approvedOT', script)
		self.assertIn("holidayOTApproved + d.normalOTApproved", script)
		self.assertIn("excelImportStatus", script)
		self.assertIn("rejectOutlierWorkbookYears", script)
		self.assertNotIn('return "On Duty"', script)
		self.assertIn("Mission and absence conflict", script)
		# Mission days must not be blocked by incomplete IN/OUT.
		self.assertIn("missionPresent", script)
		self.assertIn("&& !missionPresent", script)
		self.assertIn("CONFIG.approvedOT", script)
		self.assertIn("Excel غیبت", script)
		self.assertIn(first.get("status_options"), ("removed", "unchanged"))

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

	def test_approved_overtime_hours_formula_normal_plus_holiday_only(self):
		# Normal 02:30 + Holiday 01:15 = 3.75; Night 03:00 + Mission 02:00 excluded
		self.assertEqual(
			approved_overtime_hours_from_components(180, 120, 75, 150),
			3.75,
		)
		# Normal only 01:30
		self.assertEqual(approved_overtime_hours_from_components(0, 0, 0, 90), 1.5)
		# Holiday only 00:45
		self.assertEqual(approved_overtime_hours_from_components(0, 0, 45, 0), 0.75)
		# Night-only must NOT count
		self.assertEqual(approved_overtime_hours_from_components(90, 0, 0, 0), 0.0)
		# Mission-only must NOT count
		self.assertEqual(approved_overtime_hours_from_components(0, 90, 0, 0), 0.0)
		# zero / blank
		self.assertEqual(approved_overtime_hours_from_components(0, 0, 0, 0), 0.0)
		# Night+Normal: only Normal
		self.assertEqual(approved_overtime_hours_from_components(5, 0, 0, 859), minutes_to_hours(859))

	def test_create_persists_approved_overtime_hours(self):
		employee = self._employee("77040")
		day = add_days(getdate(nowdate()), -20)
		company = frappe.db.get_value("Employee", employee, "company")
		expected = approved_overtime_hours_from_components(30, 60, 45, 135)  # = 3.0
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
							'"approved_overtime_minutes":180}'
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
		self.assertEqual(expected, 3.0)
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
		# Night 5 excluded; only Normal 859 minutes → hours
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
							'"approved_overtime_minutes":859}'
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
					'{"source":"Attendance Excel Import","approved_overtime_minutes":859}'
				),
			},
		)
		self.assertAlmostEqual(
			float(frappe.db.get_value("Attendance", name, "custom_approved_overtime_hours")),
			expected,
			places=5,
		)

	def test_minutes_to_hours_and_shortage_formula(self):
		self.assertEqual(minutes_to_hours(90), 1.5)
		self.assertEqual(minutes_to_hours(0), 0.0)
		self.assertEqual(work_shortage_hours_from_components(30, 20, 40), 1.5)
		self.assertEqual(work_shortage_hours_from_components(0, 0, 0), 0.0)
		# Absence is never part of shortage.
		self.assertEqual(work_shortage_hours_from_components(30, 0, 0), 0.5)

	def test_unauthorized_normal_ot_dual_field_formula(self):
		# 01:45 → 1.75 on BOTH targets
		self.assertEqual(unauthorized_normal_ot_hours(105), 1.75)
		self.assertEqual(unauthorized_normal_ot_hours(0), 0.0)

	def test_excel_absence_forces_absent_status(self):
		self.assertEqual(excel_status_from_absence(30, "Present"), "Absent")
		self.assertEqual(excel_status_from_absence(480, "Present"), "Absent")
		self.assertEqual(excel_status_from_absence(0, "Present"), "Present")
		self.assertEqual(excel_status_from_absence(None, "Present"), "Present")
		# Blank/zero absence with positive presence → Present
		self.assertEqual(
			excel_status_from_absence(0, "Absent", presence_minutes=661),
			"Present",
		)
		# No punches, blank absence, positive Excel حضور → Present
		self.assertEqual(
			excel_status_from_absence(0, "Absent", presence_minutes=120, has_in_out=False),
			"Present",
		)
		# No punches, zero presence, blank absence → do NOT force Absent
		self.assertEqual(
			excel_status_from_absence(0, "Present", presence_minutes=0, has_in_out=False),
			"Present",
		)
		# Mission without punches → Present
		self.assertEqual(
			excel_status_from_absence(0, "Present", mission_minutes=540),
			"Present",
		)
		# Mission with presence still Present
		self.assertEqual(
			excel_status_from_absence(0, "Present", presence_minutes=120, mission_minutes=480),
			"Present",
		)
		self.assertTrue(mission_absence_conflict(540, 30))
		self.assertFalse(mission_absence_conflict(540, 0))

	def test_create_persists_excel_authoritative_mapping(self):
		employee = self._employee("77050")
		day = add_days(getdate(nowdate()), -30)
		company = frappe.db.get_value("Employee", employee, "company")
		shortage = work_shortage_hours_from_components(30, 20, 40)
		unauth = unauthorized_normal_ot_hours(105)
		approved = approved_overtime_hours_from_components(30, 0, 45, 60)  # holiday+normal=1.75
		result = import_attendance_details(
			[
				{
					"employee": employee,
					"attendance_date": str(day),
					"company": company,
					"mode": "create",
					"values": {
						"status": "Absent",
						"working_hours": 7.5,
						"standard_working_hours": 8,
						"custom_paid_leave_hours": 1.0,
						"custom_unpaid_leave_hours": 0.5,
						"custom_night_work_hours": 2.25,
						"custom_mission_work_hours": 1.0,
						"custom_work_shortage_hours": shortage,
						"custom_regular_work_hours": unauth,
						"custom_extra_presence_hours": unauth,
						"custom_approved_overtime_hours": approved,
						"custom_holiday_work_hours": 0.75,
						"custom_calculation_details": (
							'{"source":"Attendance Excel Import","mapping_version":"V2.0.5"}'
						),
					},
					"row": 1,
				}
			]
		)
		self.assertEqual(result["failed"], [])
		name = result["created"][0]["name"]
		self.attendance.append(name)
		row = frappe.db.get_value(
			"Attendance",
			name,
			[
				"status",
				"working_hours",
				"custom_paid_leave_hours",
				"custom_unpaid_leave_hours",
				"custom_night_work_hours",
				"custom_mission_work_hours",
				"custom_work_shortage_hours",
				"custom_regular_work_hours",
				"custom_extra_presence_hours",
				"custom_approved_overtime_hours",
				"custom_holiday_work_hours",
			],
			as_dict=True,
		)
		self.assertEqual(row.status, "Absent")
		self.assertAlmostEqual(float(row.working_hours), 7.5, places=5)
		self.assertAlmostEqual(float(row.custom_paid_leave_hours), 1.0, places=5)
		self.assertAlmostEqual(float(row.custom_unpaid_leave_hours), 0.5, places=5)
		self.assertAlmostEqual(float(row.custom_night_work_hours), 2.25, places=5)
		self.assertAlmostEqual(float(row.custom_mission_work_hours), 1.0, places=5)
		self.assertAlmostEqual(float(row.custom_work_shortage_hours), shortage, places=5)
		self.assertAlmostEqual(float(row.custom_regular_work_hours), unauth, places=5)
		self.assertAlmostEqual(float(row.custom_extra_presence_hours), unauth, places=5)
		self.assertEqual(
			float(row.custom_regular_work_hours),
			float(row.custom_extra_presence_hours),
		)
		self.assertAlmostEqual(float(row.custom_approved_overtime_hours), approved, places=5)
		self.assertEqual(approved, 1.75)
		self.assertAlmostEqual(float(row.custom_holiday_work_hours), 0.75, places=5)

	def test_submitted_update_corrects_absent_to_present(self):
		employee = self._employee("77060")
		day = add_days(getdate(nowdate()), -34)
		company = frappe.db.get_value("Employee", employee, "company")
		first = import_attendance_details(
			[
				{
					"employee": employee,
					"attendance_date": str(day),
					"company": company,
					"mode": "create",
					"values": {
						"status": "Absent",
						"standard_working_hours": 8,
						"working_hours": 0,
					},
					"row": 1,
				}
			]
		)
		name = first["created"][0]["name"]
		self.attendance.append(name)
		self.assertEqual(frappe.db.get_value("Attendance", name, "status"), "Absent")
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
						"working_hours": 11.016667,
						"custom_approved_overtime_hours": 2.016667,
						"custom_regular_work_hours": 0,
						"custom_extra_presence_hours": 0,
						"custom_night_work_hours": 0,
						"custom_work_shortage_hours": 0,
					},
					"row": 152,
				}
			]
		)
		self.assertEqual(len(second["updated"]), 1)
		self.assertEqual(frappe.db.get_value("Attendance", name, "status"), "Present")
		self.assertAlmostEqual(
			float(frappe.db.get_value("Attendance", name, "working_hours")),
			11.02,
			places=2,
		)

	def test_submitted_update_keeps_dual_unauthorized_ot_equal(self):
		employee = self._employee("77051")
		day = add_days(getdate(nowdate()), -31)
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
						"custom_regular_work_hours": 0,
						"custom_extra_presence_hours": 0,
					},
					"row": 1,
				}
			]
		)
		name = first["created"][0]["name"]
		self.attendance.append(name)
		unauth = unauthorized_normal_ot_hours(90)
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
						"custom_regular_work_hours": unauth,
						"custom_extra_presence_hours": unauth,
						"custom_work_shortage_hours": 0,
						"custom_night_work_hours": 0,
					},
					"row": 1,
				}
			]
		)
		self.assertEqual(len(second["updated"]), 1)
		reg = float(frappe.db.get_value("Attendance", name, "custom_regular_work_hours"))
		extra = float(frappe.db.get_value("Attendance", name, "custom_extra_presence_hours"))
		self.assertAlmostEqual(reg, 1.5, places=5)
		self.assertAlmostEqual(extra, 1.5, places=5)
		self.assertEqual(reg, extra)

	def test_reapply_restores_excel_fields_after_post_submit_wipe(self):
		employee = self._employee("77052")
		day = add_days(getdate(nowdate()), -32)
		company = frappe.db.get_value("Employee", employee, "company")
		unauth = unauthorized_normal_ot_hours(105)
		shortage = work_shortage_hours_from_components(30, 20, 40)
		result = import_attendance_details(
			[
				{
					"employee": employee,
					"attendance_date": str(day),
					"company": company,
					"mode": "create",
					"values": {
						"status": "Absent",
						"standard_working_hours": 8,
						"working_hours": 6,
						"custom_regular_work_hours": unauth,
						"custom_extra_presence_hours": unauth,
						"custom_work_shortage_hours": shortage,
						"custom_night_work_hours": 3,
						"custom_approved_overtime_hours": 2,
					},
					"row": 2,
				}
			]
		)
		name = result["created"][0]["name"]
		self.attendance.append(name)
		frappe.db.set_value(
			"Attendance",
			name,
			{
				"custom_regular_work_hours": 0,
				"custom_extra_presence_hours": 9,
				"custom_work_shortage_hours": 0,
				"custom_night_work_hours": 0,
				"custom_approved_overtime_hours": 0,
				"status": "Present",
			},
		)
		_reapply_excel_authoritative_fields(
			name,
			{
				"status": "Absent",
				"working_hours": 6,
				"custom_regular_work_hours": unauth,
				"custom_extra_presence_hours": unauth,
				"custom_work_shortage_hours": shortage,
				"custom_night_work_hours": 3,
				"custom_approved_overtime_hours": 2,
			},
		)
		row = frappe.db.get_value(
			"Attendance",
			name,
			[
				"status",
				"custom_regular_work_hours",
				"custom_extra_presence_hours",
				"custom_work_shortage_hours",
				"custom_night_work_hours",
				"custom_approved_overtime_hours",
			],
			as_dict=True,
		)
		self.assertEqual(row.status, "Absent")
		self.assertAlmostEqual(float(row.custom_regular_work_hours), unauth, places=5)
		self.assertAlmostEqual(float(row.custom_extra_presence_hours), unauth, places=5)
		self.assertEqual(
			float(row.custom_regular_work_hours),
			float(row.custom_extra_presence_hours),
		)
		self.assertAlmostEqual(float(row.custom_work_shortage_hours), shortage, places=5)
		self.assertAlmostEqual(float(row.custom_night_work_hours), 3.0, places=5)
		self.assertAlmostEqual(float(row.custom_approved_overtime_hours), 2.0, places=5)

	def test_draft_update_persists_excel_mapping_then_submits(self):
		employee = self._employee("77053")
		day = add_days(getdate(nowdate()), -33)
		company = frappe.db.get_value("Employee", employee, "company")
		draft = frappe.get_doc(
			{
				"doctype": "Attendance",
				"employee": employee,
				"attendance_date": day,
				"company": company,
				"status": "Present",
			}
		)
		draft.insert()
		unauth = unauthorized_normal_ot_hours(60)
		result = import_attendance_details(
			[
				{
					"employee": employee,
					"attendance_date": str(day),
					"company": company,
					"mode": "update",
					"name": draft.name,
					"values": {
						"status": "Present",
						"standard_working_hours": 8,
						"working_hours": 8,
						"custom_regular_work_hours": unauth,
						"custom_extra_presence_hours": unauth,
						"custom_work_shortage_hours": 0.5,
					},
					"row": 3,
				}
			]
		)
		name = result["updated"][0]["name"]
		self.attendance.append(name)
		self.assertEqual(int(frappe.db.get_value("Attendance", name, "docstatus")), 1)
		self.assertAlmostEqual(
			float(frappe.db.get_value("Attendance", name, "custom_regular_work_hours")),
			1.0,
			places=5,
		)
		self.assertAlmostEqual(
			float(frappe.db.get_value("Attendance", name, "custom_extra_presence_hours")),
			1.0,
			places=5,
		)

	def test_jalali_1405_07_25_converts_to_2026_10_17(self):
		self.assertEqual(expected_gregorian_from_jalali_ymd(1405, 7, 25), "2026-10-17")
		# Month / year boundaries (Jalali Nowruz)
		self.assertEqual(expected_gregorian_from_jalali_ymd(1405, 1, 1), "2026-03-21")
		self.assertEqual(expected_gregorian_from_jalali_ymd(1404, 12, 29), "2026-03-20")

	def test_client_script_date_parser_guards(self):
		script = frappe.db.get_value("Client Script", "Employee Attendance Excel Import", "script")
		# Persian / Arabic digit normalization before parse
		self.assertIn("[۰-۹]", script)
		self.assertIn("[٠-٩]", script)
		# No Date.parse on Jalali strings; reject out-of-range Jalali years
		self.assertIn("Never use Date.parse on Jalali strings", script)
		self.assertIn("y < 1200 || y > 1600", script)
		# Excel serial support + raw date diagnostics
		self.assertIn("25569", script)
		self.assertIn("rawDate", script)
		self.assertIn("raw_excel_date", script)
		self.assertIn("rejectOutlierWorkbookYears", script)
		# No silent 1399 inventing
		self.assertNotIn("1399", script)

	def test_no_global_attendance_class_override(self):
		import erpnext_extensions.hooks as hooks

		overrides = getattr(hooks, "override_doctype_class", {}) or {}
		self.assertNotIn("Attendance", overrides)
		override_path = os.path.join(
			frappe.get_app_path("erpnext_extensions"),
			"extentionhrms",
			"attendance_override.py",
		)
		self.assertFalse(os.path.isfile(override_path))

	def test_create_mission_day_as_present_without_punches(self):
		employee = self._employee("77070")
		day = add_days(getdate(nowdate()), -40)
		company = frappe.db.get_value("Employee", employee, "company")
		mission_hours = minutes_to_hours(540)  # 09:00
		result = import_attendance_details(
			[
				{
					"employee": employee,
					"attendance_date": str(day),
					"company": company,
					"mode": "create",
					"values": {
						"status": "Present",
						"standard_working_hours": 9,
						"working_hours": 0,
						"custom_mission_work_hours": mission_hours,
						"custom_regular_work_hours": 0,
						"custom_extra_presence_hours": 0,
						"custom_approved_overtime_hours": 0,
						"custom_calculation_details": (
							'{"source":"Attendance Excel Import","mapping_version":"V2.0.8",'
							'"mission_work_source":"Excel کل مأموریت"}'
						),
					},
					"row": 543,
				}
			]
		)
		self.assertEqual(result["failed"], [], result.get("failed"))
		name = result["created"][0]["name"]
		self.attendance.append(name)
		row = frappe.db.get_value(
			"Attendance",
			name,
			["status", "custom_mission_work_hours", "working_hours", "docstatus"],
			as_dict=True,
		)
		self.assertEqual(row.status, "Present")
		self.assertAlmostEqual(float(row.custom_mission_work_hours), 9.0, places=5)
		self.assertEqual(int(row.docstatus), 1)

	def test_draft_absent_mission_update_to_present(self):
		employee = self._employee("77073")
		day = add_days(getdate(nowdate()), -43)
		company = frappe.db.get_value("Employee", employee, "company")
		# Create as draft Absent via insert, then importer draft update.
		doc = frappe.get_doc(
			{
				"doctype": "Attendance",
				"employee": employee,
				"attendance_date": day,
				"company": company,
				"status": "Absent",
				"standard_working_hours": 9,
				"working_hours": 0,
			}
		)
		doc.insert(ignore_permissions=True)
		self.attendance.append(doc.name)
		result = import_attendance_details(
			[
				{
					"employee": employee,
					"attendance_date": str(day),
					"mode": "update_draft",
					"name": doc.name,
					"values": {
						"status": "Present",
						"standard_working_hours": 9,
						"working_hours": 0,
						"custom_mission_work_hours": 9.0,
						"custom_regular_work_hours": 0,
						"custom_extra_presence_hours": 0,
					},
					"row": 537,
				}
			]
		)
		self.assertEqual(result["failed"], [], result.get("failed"))
		self.assertEqual(len(result["updated"]), 1)
		row = frappe.db.get_value(
			"Attendance",
			doc.name,
			["status", "custom_mission_work_hours", "docstatus"],
			as_dict=True,
		)
		self.assertEqual(row.status, "Present")
		self.assertAlmostEqual(float(row.custom_mission_work_hours), 9.0, places=5)
		self.assertEqual(int(row.docstatus), 1)

	def test_mission_reimport_is_idempotent(self):
		employee = self._employee("77074")
		day = add_days(getdate(nowdate()), -44)
		company = frappe.db.get_value("Employee", employee, "company")
		payload_values = {
			"status": "Present",
			"standard_working_hours": 9,
			"working_hours": 0,
			"custom_mission_work_hours": 9.0,
			"custom_regular_work_hours": 0,
			"custom_extra_presence_hours": 0,
			"custom_approved_overtime_hours": 0,
			"custom_work_shortage_hours": 0,
			"custom_night_work_hours": 0,
		}
		first = import_attendance_details(
			[
				{
					"employee": employee,
					"attendance_date": str(day),
					"company": company,
					"mode": "create",
					"values": payload_values,
					"row": 537,
				}
			]
		)
		name = first["created"][0]["name"]
		self.attendance.append(name)
		second = import_attendance_details(
			[
				{
					"employee": employee,
					"attendance_date": str(day),
					"mode": "update_submitted",
					"name": name,
					"values": payload_values,
					"row": 537,
				}
			]
		)
		self.assertEqual(len(second["updated"]), 1)
		self.assertEqual(second["failed"], [])
		self.assertEqual(frappe.db.get_value("Attendance", name, "status"), "Present")
		self.assertAlmostEqual(
			float(frappe.db.get_value("Attendance", name, "custom_mission_work_hours")),
			9.0,
			places=5,
		)

	def test_submitted_update_sets_mission_present_from_absent(self):
		employee = self._employee("77071")
		day = add_days(getdate(nowdate()), -41)
		company = frappe.db.get_value("Employee", employee, "company")
		first = import_attendance_details(
			[
				{
					"employee": employee,
					"attendance_date": str(day),
					"company": company,
					"mode": "create",
					"values": {
						"status": "Absent",
						"standard_working_hours": 9,
						"working_hours": 0,
						"custom_mission_work_hours": 0,
					},
					"row": 1,
				}
			]
		)
		name = first["created"][0]["name"]
		self.attendance.append(name)
		second = import_attendance_details(
			[
				{
					"employee": employee,
					"attendance_date": str(day),
					"mode": "update_submitted",
					"name": name,
					"values": {
						"status": "Present",
						"standard_working_hours": 9,
						"working_hours": 0,
						"custom_mission_work_hours": 9.0,
						"custom_regular_work_hours": 0,
						"custom_extra_presence_hours": 0,
						"custom_night_work_hours": 0,
						"custom_work_shortage_hours": 0,
					},
					"row": 543,
				}
			]
		)
		self.assertEqual(len(second["updated"]), 1)
		self.assertEqual(frappe.db.get_value("Attendance", name, "status"), "Present")
		self.assertAlmostEqual(
			float(frappe.db.get_value("Attendance", name, "custom_mission_work_hours")),
			9.0,
			places=5,
		)

	def test_reapply_restores_mission_hours_and_present_status(self):
		employee = self._employee("77072")
		day = add_days(getdate(nowdate()), -42)
		company = frappe.db.get_value("Employee", employee, "company")
		result = import_attendance_details(
			[
				{
					"employee": employee,
					"attendance_date": str(day),
					"company": company,
					"mode": "create",
					"values": {
						"status": "Present",
						"standard_working_hours": 9,
						"working_hours": 0,
						"custom_mission_work_hours": 7.5,
					},
					"row": 2,
				}
			]
		)
		name = result["created"][0]["name"]
		self.attendance.append(name)
		frappe.db.set_value(
			"Attendance",
			name,
			{"status": "Absent", "custom_mission_work_hours": 0},
		)
		_reapply_excel_authoritative_fields(
			name,
			{"status": "Present", "custom_mission_work_hours": 7.5, "working_hours": 0},
		)
		self.assertEqual(frappe.db.get_value("Attendance", name, "status"), "Present")
		self.assertAlmostEqual(
			float(frappe.db.get_value("Attendance", name, "custom_mission_work_hours")),
			7.5,
			places=5,
		)

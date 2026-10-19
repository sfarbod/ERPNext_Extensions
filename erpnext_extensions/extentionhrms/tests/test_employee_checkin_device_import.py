# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Regression tests for the manual checkin limit and authorized device import."""

from __future__ import annotations

from multiprocessing import get_context

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, getdate, nowdate

from erpnext_extensions.extentionhrms.employee_checkin_import import (
	import_device_checkins,
)
from erpnext_extensions.extentionhrms.employee_checkin_import_setup import (
	ensure_device_import_setup,
)
from erpnext_extensions.extentionhrms.employee_checkin_manual_limit import (
	in_device_import,
	period_bounds,
)


def _concurrent_import(site, sites_path, record, queue):
	"""Separate process so the two imports use different database connections."""
	import frappe as frappe_local

	frappe_local.init(site=site, sites_path=sites_path)
	frappe_local.connect()
	frappe_local.set_user("Administrator")
	try:
		from erpnext_extensions.extentionhrms.employee_checkin_import import (
			import_device_checkins as import_rows,
		)

		queue.put(import_rows(record))
		frappe_local.db.commit()
	except Exception as exc:
		frappe_local.db.rollback()
		queue.put(repr(exc))
	finally:
		frappe_local.destroy()


class TestManualLimitCalendar(FrappeTestCase):
	def test_shahrivar_period_boundaries(self):
		# Boundaries confirmed against the legacy Server Script.
		self.assertEqual(period_bounds(getdate("2026-08-16"))[0:2], (getdate("2026-07-17"), getdate("2026-08-16")))
		start, end, sy, sm, ey, em = period_bounds(getdate("2026-08-17"))
		self.assertEqual((start, end, sy, sm, ey, em), (getdate("2026-08-17"), getdate("2026-09-16"), 1405, 5, 1405, 6))
		self.assertEqual(period_bounds(getdate("2026-09-16"))[0:2], (getdate("2026-08-17"), getdate("2026-09-16")))
		self.assertEqual(period_bounds(getdate("2026-09-17"))[0:2], (getdate("2026-09-17"), getdate("2026-10-17")))

	def test_jalali_period_label_months(self):
		_start, _end, sy, sm, ey, em = period_bounds(getdate("2026-08-19"))
		self.assertEqual((sy, sm, ey, em), (1405, 5, 1405, 6))


class TestDeviceCheckinImport(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		ensure_device_import_setup()
		frappe.db.commit()

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		self.employees = []
		self.users = []

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()

	def test_five_manual_historical_checkins_allowed_sixth_rejected(self):
		employee = self._employee()
		for offset in range(5):
			self._manual(employee, self._past(offset))
		with self.assertRaises(frappe.ValidationError) as caught:
			self._manual(employee, self._past(5))
		self.assertIn("تردد دستی", str(caught.exception))
		self.assertEqual(self._count(employee), 5)

	def test_same_day_manual_checkins_are_not_capped_at_five(self):
		employee = self._employee()
		day = nowdate()
		for hour in range(6):
			self._manual(employee, f"{day} {8 + hour:02d}:00:00")
		self.assertEqual(self._count(employee), 6)

	def test_authorized_device_import_allows_more_than_five_historical(self):
		employee = self._employee()
		records = [self._record(employee, self._past(i), "IN" if i % 2 == 0 else "OUT") for i in range(8)]
		before_attendance = frappe.db.count("Attendance")
		result = import_device_checkins(records)
		self.assertEqual(len(result["created"]), 8)
		self.assertEqual(result["failed"], [])
		self.assertEqual(self._count(employee), 8)
		self.assertFalse(in_device_import())
		self.assertEqual(frappe.db.count("Attendance"), before_attendance)
		types = [row.log_type for row in self._rows(employee)]
		self.assertEqual(types.count("IN"), 4)
		self.assertEqual(types.count("OUT"), 4)

	def test_device_import_does_not_remove_the_manual_limit(self):
		employee = self._employee()
		records = [self._record(employee, self._past(i), "IN") for i in range(6)]
		result = import_device_checkins(records)
		self.assertEqual(len(result["created"]), 6)
		with self.assertRaises(frappe.ValidationError) as caught:
			self._manual(employee, self._past(6))
		self.assertIn("حداکثر مجاز", str(caught.exception))

	def test_client_bypass_flags_do_not_skip_the_limit(self):
		employee = self._employee()
		for offset in range(5):
			self._manual(employee, self._past(offset))
		frappe.flags.skip_manual_limit = 1
		frappe.flags.is_device_import = 1
		frappe.flags.ignore_validation = 1
		try:
			doc = frappe.get_doc(
				{
					"doctype": "Employee Checkin",
					"employee": employee,
					"time": self._past(5),
					"log_type": "IN",
					"skip_manual_limit": 1,
					"is_device_import": 1,
					"ignore_validation": 1,
				}
			)
			with self.assertRaises(frappe.ValidationError) as caught:
				doc.insert()
			self.assertIn("تردد دستی", str(caught.exception))
		finally:
			frappe.flags.skip_manual_limit = 0
			frappe.flags.is_device_import = 0
			frappe.flags.ignore_validation = 0
		self.assertEqual(self._count(employee), 5)
		self.assertFalse(in_device_import())

	def test_unauthorized_user_cannot_call_the_import_api(self):
		employee = self._employee()
		user = self._user([])
		records = [self._record(employee, self._past(0), "IN")]
		frappe.set_user(user)
		with self.assertRaises(frappe.PermissionError):
			import_device_checkins(records)
		frappe.set_user("Administrator")
		self.assertEqual(self._count(employee), 0)

	def test_create_permission_without_import_role_is_rejected(self):
		employee = self._employee()
		user = self._user(["HR User"])
		if not frappe.has_permission("Employee Checkin", "create", user=user):
			self.skipTest("HR User cannot create Employee Checkin on this site")
		frappe.set_user(user)
		with self.assertRaises(frappe.PermissionError):
			import_device_checkins([self._record(employee, self._past(0), "IN")])
		frappe.set_user("Administrator")
		self.assertEqual(self._count(employee), 0)

	def test_import_role_without_create_permission_is_rejected(self):
		employee = self._employee()
		user = self._user(["Attendance Device Importer"])
		if frappe.has_permission("Employee Checkin", "create", user=user):
			self.skipTest("Attendance Device Importer unexpectedly has create permission")
		frappe.set_user(user)
		with self.assertRaises(frappe.PermissionError):
			import_device_checkins([self._record(employee, self._past(0), "IN")])
		frappe.set_user("Administrator")
		self.assertEqual(self._count(employee), 0)

	def test_inactive_employee_is_rejected_and_status_is_unchanged(self):
		employee = self._employee(status="Inactive")
		active = self._employee()
		result = import_device_checkins(
			[
				self._record(employee, self._past(0), "IN"),
				self._record(active, self._past(0), "OUT"),
			]
		)
		self.assertEqual(len(result["created"]), 1)
		self.assertEqual(result["created"][0]["employee"], active)
		self.assertEqual(result["failed"][0]["category"], "Inactive Employee")
		self.assertEqual(frappe.db.get_value("Employee", employee, "status"), "Inactive")
		self.assertEqual(self._count(employee), 0)

	def test_invalid_timestamp_and_log_type_do_not_reject_the_rest_of_the_batch(self):
		employee = self._employee()
		result = import_device_checkins(
			[
				self._record(employee, self._past(0), "IN"),
				{"employee": employee, "time": "not-a-date", "log_type": "IN"},
				self._record(employee, self._past(1), "MAYBE"),
				self._record(employee, self._past(2), "OUT"),
			]
		)
		self.assertEqual(len(result["created"]), 2)
		categories = sorted(row["category"] for row in result["failed"])
		self.assertEqual(categories, ["Invalid timestamp", "Validation error"])
		self.assertEqual(self._count(employee), 2)

	def test_duplicate_in_batch_and_repeated_import(self):
		employee = self._employee()
		first = self._record(employee, self._past(0), "IN")
		second = self._record(employee, self._past(0), "OUT")
		result = import_device_checkins([first, dict(first), second])
		self.assertEqual(len(result["created"]), 2)
		self.assertEqual(len(result["duplicates"]), 1)
		again = import_device_checkins([first, second])
		self.assertEqual(again["created"], [])
		self.assertEqual(len(again["duplicates"]), 2)
		self.assertEqual(self._count(employee), 2)
		self.assertEqual(
			frappe.db.sql(
				"""
				select count(*) from (
					select employee, time, log_type
					from `tabEmployee Checkin`
					where employee = %s
					group by employee, time, log_type
					having count(*) > 1
				) t
				""",
				employee,
			)[0][0],
			0,
		)

	def test_missing_employee_is_a_row_failure(self):
		employee = self._employee()
		result = import_device_checkins(
			[
				self._record(employee, self._past(0), "IN"),
				self._record("HR-EMP-DOES-NOT-EXIST", self._past(0), "IN"),
			]
		)
		self.assertEqual(len(result["created"]), 1)
		self.assertEqual(result["failed"][0]["category"], "Invalid Employee")

	def test_device_id_fills_blank_and_preserves_existing(self):
		blank = self._employee()
		filled = self._employee()
		before = self._snapshot(filled)
		frappe.db.set_value("Employee", filled, "attendance_device_id", "KEEP-ME")
		result = import_device_checkins(
			[self._record(blank, self._past(0), "IN")],
			device_assignments=[
				{"employee": blank, "personnel_code": "00042"},
				{"employee": filled, "personnel_code": "SHOULD-NOT-REPLACE"},
				{"employee": blank, "skip_manual_limit": 1, "personnel_code": "00042"},
			],
		)
		self.assertEqual(result["device_ids"]["updated"], [blank])
		self.assertIn(filled, result["device_ids"]["preserved"])
		self.assertEqual(frappe.db.get_value("Employee", blank, "attendance_device_id"), "00042")
		self.assertEqual(frappe.db.get_value("Employee", filled, "attendance_device_id"), "KEEP-ME")
		after = self._snapshot(filled)
		after.pop("attendance_device_id", None)
		before_without_device = dict(before)
		before_without_device.pop("attendance_device_id", None)
		self.assertEqual(after, before_without_device)
		self.assertEqual(frappe.db.get_value("Employee", blank, "status"), "Active")

	def test_user_permission_blocks_employee_outside_scope(self):
		allowed = self._employee()
		hidden = self._employee()
		user = self._user(["HR Manager", "Attendance Device Importer"])
		if not frappe.has_permission("Employee Checkin", "create", user=user):
			self.skipTest("HR Manager cannot create Employee Checkin on this site")
		frappe.get_doc(
			{
				"doctype": "User Permission",
				"user": user,
				"allow": "Employee",
				"for_value": allowed,
				"apply_to_all_doctypes": 1,
			}
		).insert(ignore_permissions=True)
		frappe.set_user(user)
		if frappe.has_permission("Employee", "read", hidden):
			self.skipTest("User Permission did not hide the other employee")
		result = import_device_checkins(
			[
				self._record(allowed, self._past(0), "IN"),
				self._record(hidden, self._past(0), "OUT"),
			]
		)
		frappe.set_user("Administrator")
		self.assertEqual([row["employee"] for row in result["created"]], [allowed])
		self.assertEqual(result["failed"][0]["employee"], hidden)
		self.assertEqual(result["failed"][0]["category"], "Permission error")
		self.assertEqual(self._count(hidden), 0)

	def test_concurrent_imports_of_the_same_punch_create_one_row(self):
		employee = self._employee()
		frappe.db.commit()
		record = [self._record(employee, "2026-08-18 06:06:06", "IN")]
		site = frappe.local.site
		sites_path = frappe.utils.get_site_path("..")
		ctx = get_context("spawn")
		queue = ctx.Queue()
		processes = [
			ctx.Process(target=_concurrent_import, args=(site, sites_path, record, queue))
			for _ in range(2)
		]
		try:
			for process in processes:
				process.start()
			for process in processes:
				process.join(60)
			results = [queue.get(timeout=20) for _ in processes]
			self.assertTrue(all(process.exitcode == 0 for process in processes), [p.exitcode for p in processes])
			self.assertTrue(all(isinstance(item, dict) for item in results), results)
			created = sum(len(item["created"]) for item in results)
			duplicates = sum(len(item["duplicates"]) for item in results)
			failed = sum(len(item["failed"]) for item in results)
			self.assertEqual(created, 1, results)
			self.assertEqual(duplicates, 1, results)
			self.assertEqual(failed, 0, results)
			self.assertEqual(self._count(employee), 1)
		finally:
			frappe.set_user("Administrator")
			frappe.db.sql("delete from `tabEmployee Checkin` where employee=%s", employee)
			if frappe.db.exists("Employee", employee):
				frappe.delete_doc("Employee", employee, force=1, ignore_permissions=True)
			frappe.db.commit()

	def _employee(self, status="Active"):
		template = frappe.db.get_value(
			"Employee",
			{"status": "Active", "company": ["is", "set"], "department": ["is", "set"]},
			["company", "department", "payroll_cost_center"],
			as_dict=True,
		)
		doc = frappe.get_doc(
			{
				"doctype": "Employee",
				"naming_series": "HR-EMP-",
				"first_name": "DeviceImport",
				"gender": "Male",
				"date_of_birth": "1990-01-01",
				"date_of_joining": "2020-01-01",
				"status": status,
				"company": template.company,
				"department": template.department,
				"payroll_cost_center": template.payroll_cost_center,
			}
		)
		doc.insert()
		self.employees.append(doc.name)
		return doc.name

	def _user(self, roles):
		email = f"device-import-{frappe.generate_hash(length=8)}@example.com"
		doc = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": "DeviceImport",
				"send_welcome_email": 0,
				"roles": [{"role": role} for role in roles],
			}
		)
		doc.insert(ignore_permissions=True)
		self.users.append(doc.name)
		return doc.name

	def _manual(self, employee, when):
		doc = frappe.get_doc(
			{
				"doctype": "Employee Checkin",
				"employee": employee,
				"time": when,
				"log_type": "IN",
				"skip_auto_attendance": 1,
			}
		)
		doc.insert()
		return doc.name

	def _record(self, employee, when, log_type):
		return {
			"employee": employee,
			"time": when,
			"log_type": log_type,
			"device_id": "dev",
			"skip_auto_attendance": 1,
			"skip_manual_limit": 1,
			"is_device_import": 1,
		}

	def _past(self, offset):
		day = add_days(getdate("2026-08-18"), offset)
		return f"{day} 08:{offset:02d}:00"

	def _count(self, employee):
		return frappe.db.count("Employee Checkin", {"employee": employee})

	def _rows(self, employee):
		return frappe.get_all(
			"Employee Checkin",
			filters={"employee": employee},
			fields=["log_type", "time"],
			order_by="time asc",
		)

	def _snapshot(self, employee):
		return frappe.db.get_value(
			"Employee",
			employee,
			["status", "company", "department", "payroll_cost_center", "attendance_device_id", "first_name"],
			as_dict=True,
		)

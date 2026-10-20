# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Regression tests for the manual checkin limit and authorized device import."""

from __future__ import annotations

from multiprocessing import get_context
from types import SimpleNamespace

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, getdate, nowdate

from erpnext_extensions.extentionhrms.employee_checkin_import import (
	find_existing_checkin_keys,
	import_device_checkins,
	report_employee_code_stored_as_device_id,
)
from erpnext_extensions.extentionhrms.employee_checkin_import_setup import (
	IMPORT_ROLE,
	ensure_device_import_setup,
)
from erpnext_extensions.extentionhrms.employee_checkin_manual_limit import (
	DEFAULT_MAX_MANUAL_CHECKINS,
	ENABLE_FIELD,
	MAX_FIELD,
	clear_manual_checkin_policy_cache,
	get_manual_checkin_policy,
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


class TestManualLimitSettings(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		ensure_device_import_setup()
		frappe.db.commit()

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		self._set_policy(1, DEFAULT_MAX_MANUAL_CHECKINS)
		self.employees = []
		self.users = []

	def tearDown(self):
		frappe.set_user("Administrator")
		self._set_policy(1, DEFAULT_MAX_MANUAL_CHECKINS)
		super().tearDown()

	def test_default_policy_is_enabled_with_five(self):
		clear_manual_checkin_policy_cache()
		frappe.db.delete("Singles", {"doctype": "HR Settings", "field": ENABLE_FIELD})
		frappe.db.delete("Singles", {"doctype": "HR Settings", "field": MAX_FIELD})
		frappe.clear_document_cache("HR Settings", "HR Settings")
		clear_manual_checkin_policy_cache()
		policy = get_manual_checkin_policy()
		self.assertTrue(policy["enabled"])
		self.assertEqual(policy["maximum"], DEFAULT_MAX_MANUAL_CHECKINS)
		self._set_policy(1, DEFAULT_MAX_MANUAL_CHECKINS)

	def test_configured_maximum_takes_effect(self):
		self._set_policy(1, 2)
		employee = self._employee()
		self._manual(employee, self._past(0))
		self._manual(employee, self._past(1))
		with self.assertRaises(frappe.ValidationError):
			self._manual(employee, self._past(2))
		self.assertEqual(self._count(employee), 2)

	def test_disabled_limit_allows_historical_manuals(self):
		self._set_policy(0, 5)
		employee = self._employee()
		for offset in range(8):
			self._manual(employee, self._past(offset))
		self.assertEqual(self._count(employee), 8)

	def test_invalid_maximum_falls_back_to_default_at_runtime(self):
		frappe.db.set_single_value("HR Settings", ENABLE_FIELD, 1, update_modified=False)
		frappe.db.set_single_value("HR Settings", MAX_FIELD, 0, update_modified=False)
		clear_manual_checkin_policy_cache()
		policy = get_manual_checkin_policy()
		self.assertTrue(policy["enabled"])
		self.assertEqual(policy["maximum"], DEFAULT_MAX_MANUAL_CHECKINS)

	def test_hr_manager_cannot_change_quota_settings(self):
		user = self._user(["HR Manager"])
		frappe.set_user(user)
		doc = frappe.get_doc("HR Settings")
		doc.set(ENABLE_FIELD, 0)
		with self.assertRaises(frappe.PermissionError):
			doc.save()
		frappe.set_user("Administrator")

	def _set_policy(self, enabled, maximum):
		frappe.db.set_single_value("HR Settings", ENABLE_FIELD, enabled, update_modified=False)
		frappe.db.set_single_value("HR Settings", MAX_FIELD, maximum, update_modified=False)
		clear_manual_checkin_policy_cache()

	def _employee(self, status="Active"):
		return _make_employee(self, status)

	def _user(self, roles):
		return _make_user(self, roles)

	def _manual(self, employee, when):
		return _manual_insert(employee, when)

	def _past(self, offset):
		return _past(offset)

	def _count(self, employee):
		return frappe.db.count("Employee Checkin", {"employee": employee})


class TestDeviceCheckinImport(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		ensure_device_import_setup()
		frappe.db.set_single_value("HR Settings", ENABLE_FIELD, 1, update_modified=False)
		frappe.db.set_single_value("HR Settings", MAX_FIELD, DEFAULT_MAX_MANUAL_CHECKINS, update_modified=False)
		clear_manual_checkin_policy_cache()
		frappe.db.commit()

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		frappe.db.set_single_value("HR Settings", ENABLE_FIELD, 1, update_modified=False)
		frappe.db.set_single_value("HR Settings", MAX_FIELD, DEFAULT_MAX_MANUAL_CHECKINS, update_modified=False)
		clear_manual_checkin_policy_cache()
		self.employees = []
		self.users = []
		self._restore_request()

	def tearDown(self):
		frappe.set_user("Administrator")
		self._restore_request()
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
		result = import_device_checkins(records, location_device_id="GATE-A")
		self.assertEqual(len(result["created"]), 8)
		self.assertEqual(result["failed"], [])
		self.assertEqual(self._count(employee), 8)
		self.assertFalse(in_device_import())
		self.assertEqual(frappe.db.count("Attendance"), before_attendance)
		device_ids = frappe.get_all(
			"Employee Checkin", filters={"employee": employee}, pluck="device_id"
		)
		self.assertTrue(all(d == "GATE-A" for d in device_ids))
		types = [row.log_type for row in self._rows(employee)]
		self.assertEqual(types.count("IN"), 4)
		self.assertEqual(types.count("OUT"), 4)

	def test_employee_code_is_not_written_as_checkin_device_id(self):
		employee = self._employee()
		code = "EMPCODE-77"
		frappe.db.set_value("Employee", employee, "attendance_device_id", code)
		# Client mistakenly sending personnel code as device_id must be stripped.
		result = import_device_checkins(
			[
				{
					"employee": employee,
					"time": self._past(0),
					"log_type": "IN",
					"device_id": code,
					"skip_auto_attendance": 1,
				}
			]
		)
		self.assertEqual(len(result["created"]), 1)
		stored = frappe.db.get_value("Employee Checkin", result["created"][0]["name"], "device_id")
		self.assertIn(stored, (None, ""))

	def test_missing_location_leaves_device_id_empty(self):
		employee = self._employee()
		result = import_device_checkins([self._record(employee, self._past(0), "IN", device_id="")])
		self.assertEqual(len(result["created"]), 1)
		stored = frappe.db.get_value("Employee Checkin", result["created"][0]["name"], "device_id")
		self.assertIn(stored, (None, ""))

	def test_conflicting_attendance_device_id_is_rejected(self):
		first = self._employee()
		second = self._employee()
		frappe.db.set_value("Employee", first, "attendance_device_id", "SHARED-9")
		result = import_device_checkins(
			[self._record(second, self._past(0), "IN")],
			device_assignments=[{"employee": second, "personnel_code": "SHARED-9"}],
		)
		self.assertEqual(result["device_ids"]["updated"], [])
		self.assertEqual(result["device_ids"]["failed"][0]["category"], "Conflicting attendance_device_id")
		self.assertIn(frappe.db.get_value("Employee", second, "attendance_device_id"), (None, ""))

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
					"device_id": "forged-device",
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

	def test_native_api_path_alone_does_not_bypass_quota(self):
		employee = self._employee()
		for offset in range(5):
			self._manual(employee, self._past(offset))
		self._set_native_request()
		# Administrator is System Manager but not granted exemption without Importer
		# role on the native path. Remove Importer if present on this session by
		# using a System Manager user without that role.
		user = self._user(["System Manager"])
		self._ensure_checkin_create(user)
		frappe.set_user(user)
		with self.assertRaises(frappe.ValidationError) as caught:
			self._manual(employee, self._past(5))
		self.assertIn("تردد دستی", str(caught.exception))
		frappe.set_user("Administrator")
		self.assertEqual(self._count(employee), 5)

	def test_native_api_with_importer_role_is_exempt(self):
		employee = self._employee()
		for offset in range(5):
			self._manual(employee, self._past(offset))
		user = self._user(["Attendance Device Importer", "HR Manager", "System Manager"])
		self._ensure_checkin_create(user)
		self._set_native_request()
		frappe.set_user(user)
		# Even with System Manager, Importer + native path is the trusted boundary.
		name = self._manual(employee, self._past(5))
		frappe.set_user("Administrator")
		self.assertTrue(name)
		self.assertEqual(self._count(employee), 6)

	def test_importer_role_without_native_path_still_limited(self):
		employee = self._employee()
		for offset in range(5):
			self._manual(employee, self._past(offset))
		user = self._user(["Attendance Device Importer", "System Manager"])
		self._ensure_checkin_create(user)
		# Desk-like insert: no native API marker on the request.
		frappe.local.request = SimpleNamespace(path="/api/resource/Employee Checkin")
		frappe.local.form_dict = frappe._dict()
		frappe.set_user(user)
		with self.assertRaises(frappe.ValidationError):
			self._manual(employee, self._past(5))
		frappe.set_user("Administrator")
		self.assertEqual(self._count(employee), 5)

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
		perm_name = frappe.db.get_value(
			"Custom DocPerm",
			{"parent": "Employee Checkin", "role": IMPORT_ROLE, "permlevel": 0},
			"name",
		)
		self.assertTrue(perm_name, "setup should grant Custom DocPerm for the import role")
		frappe.db.set_value("Custom DocPerm", perm_name, {"create": 0, "read": 1, "select": 1}, update_modified=False)
		frappe.clear_cache(doctype="Employee Checkin")
		try:
			frappe.set_user(user)
			self.assertFalse(frappe.has_permission("Employee Checkin", "create"))
			with self.assertRaises(frappe.PermissionError):
				import_device_checkins([self._record(employee, self._past(0), "IN")])
			frappe.set_user("Administrator")
			self.assertEqual(self._count(employee), 0)
		finally:
			frappe.set_user("Administrator")
			ensure_device_import_setup()
			frappe.clear_cache(doctype="Employee Checkin")

	def test_find_existing_checkin_keys_reports_duplicates_only(self):
		employee = self._employee()
		when = self._past(0)
		self._manual(employee, when)
		before = frappe.db.count("Employee Checkin")
		result = find_existing_checkin_keys(
			[
				{"employee": employee, "time": when, "log_type": "IN"},
				{"employee": employee, "time": self._past(1), "log_type": "OUT"},
			]
		)
		self.assertEqual(result["checked"], 2)
		self.assertEqual(result["existing"], 1)
		self.assertEqual(len(result["keys"]), 1)
		self.assertEqual(result["keys"][0]["employee"], employee)
		self.assertEqual(result["keys"][0]["log_type"], "IN")
		self.assertEqual(frappe.db.count("Employee Checkin"), before)

	def test_find_existing_checkin_keys_requires_import_authorization(self):
		employee = self._employee()
		user = self._user(["HR User"])
		frappe.set_user(user)
		with self.assertRaises(frappe.PermissionError):
			find_existing_checkin_keys([{"employee": employee, "time": self._past(0), "log_type": "IN"}])
		frappe.set_user("Administrator")

	def test_import_role_can_use_existing_key_lookup_after_setup(self):
		employee = self._employee()
		when = self._past(0)
		self._manual(employee, when)
		user = self._user(["Attendance Device Importer"])
		frappe.set_user(user)
		self.assertTrue(frappe.has_permission("Employee Checkin", "create"))
		result = find_existing_checkin_keys(
			[{"employee": employee, "time": when, "log_type": "IN"}]
		)
		frappe.set_user("Administrator")
		self.assertEqual(result["existing"], 1)

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
		self.assertTrue(
			filled in result["device_ids"]["preserved"]
			or any(f.get("employee") == filled for f in result["device_ids"]["failed"])
		)
		self.assertEqual(frappe.db.get_value("Employee", blank, "attendance_device_id"), "00042")
		self.assertEqual(frappe.db.get_value("Employee", filled, "attendance_device_id"), "KEEP-ME")
		after = self._snapshot(filled)
		after.pop("attendance_device_id", None)
		before_without_device = dict(before)
		before_without_device.pop("attendance_device_id", None)
		self.assertEqual(after, before_without_device)

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

	def test_batch_size_cap(self):
		employee = self._employee()
		records = [self._record(employee, self._past(i % 20), "IN" if i % 2 == 0 else "OUT") for i in range(201)]
		# Unique timestamps so we hit the batch cap, not duplicates.
		records = [
			self._record(employee, f"2026-07-01 {i // 60:02d}:{i % 60:02d}:00", "IN") for i in range(201)
		]
		with self.assertRaises(frappe.ValidationError) as caught:
			import_device_checkins(records)
		self.assertIn("200", str(caught.exception))

	def test_report_employee_code_as_device_id_is_read_only(self):
		before = frappe.db.count("Employee Checkin")
		report = report_employee_code_stored_as_device_id(limit=10)
		self.assertIn("total", report)
		self.assertEqual(frappe.db.count("Employee Checkin"), before)

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

	def _set_native_request(self):
		frappe.local.request = SimpleNamespace(
			path="/api/method/hrms.hr.doctype.employee_checkin.employee_checkin.add_log_based_on_employee_field"
		)
		frappe.local.form_dict = frappe._dict(
			cmd="hrms.hr.doctype.employee_checkin.employee_checkin.add_log_based_on_employee_field"
		)

	def _restore_request(self):
		frappe.local.request = SimpleNamespace(path="/")
		frappe.local.form_dict = frappe._dict()

	def _ensure_checkin_create(self, user):
		if frappe.has_permission("Employee Checkin", "create", user=user):
			return
		# Grant via role permission temporarily for the test user roles.
		for role in frappe.get_roles(user):
			if role in ("Guest", "All", "Desk User"):
				continue
			if not frappe.db.exists("Custom DocPerm", {"parent": "Employee Checkin", "role": role, "permlevel": 0}):
				frappe.get_doc(
					{
						"doctype": "Custom DocPerm",
						"parent": "Employee Checkin",
						"parenttype": "DocType",
						"parentfield": "permissions",
						"role": role,
						"permlevel": 0,
						"read": 1,
						"write": 1,
						"create": 1,
					}
				).insert(ignore_permissions=True)
		frappe.clear_cache(doctype="Employee Checkin")

	def _employee(self, status="Active"):
		return _make_employee(self, status)

	def _user(self, roles):
		return _make_user(self, roles)

	def _manual(self, employee, when):
		return _manual_insert(employee, when)

	def _record(self, employee, when, log_type, device_id=None):
		return {
			"employee": employee,
			"time": when,
			"log_type": log_type,
			"device_id": "" if device_id is None else device_id,
			"skip_auto_attendance": 1,
			"skip_manual_limit": 1,
			"is_device_import": 1,
		}

	def _past(self, offset):
		return _past(offset)

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


def _make_employee(suite, status="Active"):
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
	suite.employees.append(doc.name)
	return doc.name


def _make_user(suite, roles):
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
	suite.users.append(doc.name)
	return doc.name


def _manual_insert(employee, when):
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


def _past(offset):
	day = add_days(getdate("2026-08-18"), offset)
	return f"{day} 08:{offset:02d}:00"

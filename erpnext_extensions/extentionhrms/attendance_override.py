# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Attendance DocType override for Excel-import mission status support.

HRMS ``Attendance.validate`` hardcodes status options and rejects ``On Duty``.
Mission days imported from Excel require ``On Duty``; this subclass extends the
allowlist only. It does not change Present/Absent calculation for non-import paths.
"""

from __future__ import annotations

from erpnext.controllers.status_updater import validate_status
from hrms.hr.doctype.attendance.attendance import Attendance
from hrms.hr.utils import validate_active_employee

ATTENDANCE_STATUSES = (
	"Present",
	"Absent",
	"On Leave",
	"Half Day",
	"Work From Home",
	"On Duty",
)


class AttendanceWithOnDuty(Attendance):
	def validate(self):
		validate_status(self.status, list(ATTENDANCE_STATUSES))
		validate_active_employee(self.employee)
		self.validate_attendance_date()
		self.validate_duplicate_record()
		self.validate_overlapping_shift_attendance()
		self.validate_employee_status()
		self.check_leave_record()

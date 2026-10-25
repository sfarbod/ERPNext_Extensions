# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Unit / permission / validation tests for payroll_correction API.

Framework-light tests for parameter gates (no DB). Integration gates that need
Frappe run under ``bench --site … run-tests``.
"""

from __future__ import annotations

import importlib
import sys
import types
import unittest
from unittest.mock import MagicMock, patch


def _load_module_with_frappe_stub():
	"""Import payroll_correction with a minimal frappe stub for pure validation tests."""
	if "erpnext_extensions.extentionhrms.payroll_correction" in sys.modules:
		del sys.modules["erpnext_extensions.extentionhrms.payroll_correction"]

	frappe = types.ModuleType("frappe")
	frappe.session = types.SimpleNamespace(user="Administrator")
	frappe.local = types.SimpleNamespace(site="test", conf={})
	frappe.PermissionError = type("PermissionError", (Exception,), {})
	frappe.ValidationError = type("ValidationError", (Exception,), {})
	frappe.Whitelist = lambda **kw: (lambda f: f)

	def throw(msg, exc=None):
		raise (exc or Exception)(str(msg))

	frappe.throw = throw
	frappe._ = lambda x: x
	frappe.whitelist = lambda **kw: (lambda f: f)
	frappe.only_for = MagicMock()
	frappe.logger = lambda *a, **k: MagicMock()
	frappe.cache = MagicMock()
	frappe.db = MagicMock()
	frappe.get_doc = MagicMock()
	frappe.get_all = MagicMock()
	frappe.utils = types.ModuleType("frappe.utils")

	def flt(v):
		return float(v or 0)

	def cint(v):
		return int(v)

	def getdate(v):
		from datetime import date, datetime

		if isinstance(v, date) and not isinstance(v, datetime):
			return v
		return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()

	frappe.utils.flt = flt
	frappe.utils.cint = cint
	frappe.utils.getdate = getdate
	frappe.utils.now_datetime = lambda: "2026-01-01"

	sys.modules["frappe"] = frappe
	sys.modules["frappe.utils"] = frappe.utils

	# Ensure package path
	import erpnext_extensions.extentionhrms.payroll_correction as mod

	importlib.reload(mod)
	return mod, frappe


class TestPayrollCorrectionValidation(unittest.TestCase):
	def setUp(self):
		self.mod, self.frappe = _load_module_with_frappe_stub()

	def test_rejects_guest(self):
		self.frappe.session.user = "Guest"

		def only_for(*a, **k):
			pass

		self.frappe.only_for = only_for
		with self.assertRaises(Exception):
			self.mod._assert_permission()

	def test_employee_id_pattern(self):
		with self.assertRaises(Exception):
			self.mod._validate_params(
				action="dry_run",
				payroll_entry="شهریور 1405",
				employee="نفیسه قربانی",  # name not ID
				salary_slip="Sal Slip/HR-EMP-0035/00030",
				expected_je="ACC-JV-1",
				idempotency_key="abcdefgh",
				required_je_posting_date="2026-09-22",
			)

	def test_salary_slip_prefix(self):
		with self.assertRaises(Exception):
			self.mod._validate_params(
				action="dry_run",
				payroll_entry="شهریور 1405",
				employee="HR-EMP-0035",
				salary_slip="WRONG",
				expected_je="ACC-JV-1",
				idempotency_key="abcdefgh",
				required_je_posting_date="2026-09-22",
			)

	def test_action_must_be_dry_or_execute(self):
		with self.assertRaises(Exception):
			self.mod._validate_params(
				action="delete_all",
				payroll_entry="شهریور 1405",
				employee="HR-EMP-0035",
				salary_slip="Sal Slip/HR-EMP-0035/00030",
				expected_je="ACC-JV-1",
				idempotency_key="abcdefgh",
				required_je_posting_date="2026-09-22",
			)

	def test_valid_params(self):
		action = self.mod._validate_params(
			action="dry_run",
			payroll_entry="شهریور 1405",
			employee="HR-EMP-0035",
			salary_slip="Sal Slip/HR-EMP-0035/00030",
			expected_je="ACC-JV-1405-172040",
			idempotency_key="abcdefgh",
			required_je_posting_date="2026-09-22",
		)
		self.assertEqual(action, "dry_run")

	def test_idempotency_key_too_short(self):
		with self.assertRaises(Exception):
			self.mod._validate_params(
				action="execute",
				payroll_entry="شهریور 1405",
				employee="HR-EMP-0035",
				salary_slip="Sal Slip/HR-EMP-0035/00030",
				expected_je="ACC-JV-1",
				idempotency_key="short",
				required_je_posting_date="2026-09-22",
			)


if __name__ == "__main__":
	unittest.main()

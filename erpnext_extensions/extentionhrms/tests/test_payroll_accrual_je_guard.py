# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Regression tests for Payroll Entry accrual JE regeneration guard (v5.7.26)."""

from __future__ import annotations

import threading
import unittest
from unittest.mock import patch

import frappe

from erpnext_extensions.extentionhrms.payroll_accrual_je_guard import (
	assert_no_active_accrual_journal_entry,
	classify_linked_journal_entries,
	clear_inactive_salary_slip_journal_links,
	get_active_accrual_journal_entries,
	prepare_accrual_journal_regeneration,
)


class TestPayrollAccrualJeGuardUnit(unittest.TestCase):
	"""Pure classification behaviour with DB mocked."""

	def test_no_links_means_no_active(self):
		with patch(
			"erpnext_extensions.extentionhrms.payroll_accrual_je_guard.collect_linked_journal_entry_names",
			return_value=[],
		):
			self.assertEqual(get_active_accrual_journal_entries("PE-1"), [])

	def test_cancelled_ignored(self):
		with (
			patch(
				"erpnext_extensions.extentionhrms.payroll_accrual_je_guard.collect_linked_journal_entry_names",
				return_value=["JE-C"],
			),
			patch("frappe.db.get_value", return_value=2),
		):
			classified = classify_linked_journal_entries("PE-1")
			self.assertEqual(classified["cancelled"], ["JE-C"])
			self.assertEqual(classified["active"], [])

	def test_draft_blocks(self):
		with (
			patch(
				"erpnext_extensions.extentionhrms.payroll_accrual_je_guard.collect_linked_journal_entry_names",
				return_value=["JE-D"],
			),
			patch("frappe.db.get_value", return_value=0),
		):
			self.assertEqual(get_active_accrual_journal_entries("PE-1"), ["JE-D"])
			with self.assertRaises(Exception):
				assert_no_active_accrual_journal_entry("PE-1")

	def test_submitted_blocks(self):
		with (
			patch(
				"erpnext_extensions.extentionhrms.payroll_accrual_je_guard.collect_linked_journal_entry_names",
				return_value=["JE-S"],
			),
			patch("frappe.db.get_value", return_value=1),
		):
			self.assertEqual(get_active_accrual_journal_entries("PE-1"), ["JE-S"])

	def test_missing_link_allowed(self):
		with (
			patch(
				"erpnext_extensions.extentionhrms.payroll_accrual_je_guard.collect_linked_journal_entry_names",
				return_value=["JE-GONE"],
			),
			patch("frappe.db.get_value", return_value=None),
		):
			classified = classify_linked_journal_entries("PE-1")
			self.assertEqual(classified["missing"], ["JE-GONE"])
			self.assertEqual(classified["active"], [])

	def test_multiple_cancelled_allowed(self):
		vals = {"JE-1": 2, "JE-2": 2}

		def _gv(dt, name, field):
			return vals.get(name)

		with (
			patch(
				"erpnext_extensions.extentionhrms.payroll_accrual_je_guard.collect_linked_journal_entry_names",
				return_value=["JE-1", "JE-2"],
			),
			patch("frappe.db.get_value", side_effect=_gv),
		):
			self.assertEqual(get_active_accrual_journal_entries("PE-1"), [])

	def test_cancelled_plus_draft_blocks(self):
		vals = {"JE-C": 2, "JE-D": 0}

		def _gv(dt, name, field):
			return vals.get(name)

		with (
			patch(
				"erpnext_extensions.extentionhrms.payroll_accrual_je_guard.collect_linked_journal_entry_names",
				return_value=["JE-C", "JE-D"],
			),
			patch("frappe.db.get_value", side_effect=_gv),
		):
			self.assertEqual(get_active_accrual_journal_entries("PE-1"), ["JE-D"])

	def test_cancelled_plus_submitted_blocks(self):
		vals = {"JE-C": 2, "JE-S": 1}

		def _gv(dt, name, field):
			return vals.get(name)

		with (
			patch(
				"erpnext_extensions.extentionhrms.payroll_accrual_je_guard.collect_linked_journal_entry_names",
				return_value=["JE-C", "JE-S"],
			),
			patch("frappe.db.get_value", side_effect=_gv),
		):
			self.assertEqual(get_active_accrual_journal_entries("PE-1"), ["JE-S"])


class TestPayrollAccrualJeClearLinks(unittest.TestCase):
	def test_clear_inactive_links_for_cancelled_and_missing(self):
		rows = [
			{"name": "SS-1", "journal_entry": "JE-C"},
			{"name": "SS-2", "journal_entry": "JE-GONE"},
			{"name": "SS-3", "journal_entry": "JE-D"},
		]
		vals = {"JE-C": 2, "JE-GONE": None, "JE-D": 0}
		cleared_targets = []

		def _gv(dt, name, field):
			return vals.get(name)

		def _set(dt, name, field, value, update_modified=True):
			cleared_targets.append((name, value))

		with (
			patch("frappe.db.sql", return_value=rows),
			patch("frappe.db.get_value", side_effect=_gv),
			patch("frappe.db.set_value", side_effect=_set),
		):
			cleared = clear_inactive_salary_slip_journal_links("PE-X")
		self.assertEqual(sorted(cleared), ["JE-C", "JE-GONE"])
		self.assertEqual(len(cleared_targets), 2)
		self.assertTrue(all(v == "" for _, v in cleared_targets))


class TestPayrollAccrualJeConcurrency(unittest.TestCase):
	def test_second_prepare_blocks_when_active_appears(self):
		"""Simulate two callers: first sees none, second sees Draft."""
		state = {"active": []}

		def fake_assert(pe):
			if state["active"]:
				frappe.throw("blocked")
			return {"active": [], "cancelled": [], "missing": []}

		with (
			patch(
				"erpnext_extensions.extentionhrms.payroll_accrual_je_guard.frappe.db.exists",
				return_value=True,
			),
			patch(
				"erpnext_extensions.extentionhrms.payroll_accrual_je_guard.lock_payroll_entry",
			),
			patch(
				"erpnext_extensions.extentionhrms.payroll_accrual_je_guard.frappe.db.get_value",
				return_value=1,
			),
			patch(
				"erpnext_extensions.extentionhrms.payroll_accrual_je_guard.assert_no_active_accrual_journal_entry",
				side_effect=fake_assert,
			),
			patch(
				"erpnext_extensions.extentionhrms.payroll_accrual_je_guard.clear_inactive_salary_slip_journal_links",
				return_value=[],
			),
		):
			prepare_accrual_journal_regeneration("PE-1")
			state["active"] = ["JE-NEW"]
			with self.assertRaises(Exception):
				prepare_accrual_journal_regeneration("PE-1")


class TestPayrollAccrualJeConcurrentThreads(unittest.TestCase):
	def test_only_one_winner_under_lock_simulation(self):
		winner = []
		lock = threading.Lock()
		barrier = threading.Barrier(2)

		def worker(label):
			barrier.wait(timeout=5)
			with lock:
				if not winner:
					winner.append(label)

		t1 = threading.Thread(target=worker, args=("a",))
		t2 = threading.Thread(target=worker, args=("b",))
		t1.start()
		t2.start()
		t1.join()
		t2.join()
		self.assertEqual(len(winner), 1)

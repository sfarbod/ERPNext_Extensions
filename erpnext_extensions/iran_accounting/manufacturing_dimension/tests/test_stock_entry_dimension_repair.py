# Copyright (c) 2026, ERPNext Extensions contributors
"""R1–R17: Stock Entry Dimension Repair Scan / Preview / Dry Run / Apply."""

from __future__ import annotations

import unittest
from decimal import Decimal

import frappe
from frappe.utils import nowdate, nowtime

from erpnext_extensions.iran_accounting.e2e_bootstrap import (
	enable_perpetual_inventory,
	ensure_test_item,
	get_irr_company,
	get_second_warehouse,
	get_warehouse,
)
from erpnext_extensions.iran_accounting.manufacturing_dimension.guard import (
	validate_manufacturing_dimension_uniformity,
)
from erpnext_extensions.iran_accounting.manufacturing_dimension.repair import (
	_fetch_active_gl,
	_fetch_sed_snapshot,
	_fetch_sle_invariants,
	_gl_compare_rows,
	apply_dimension_repair,
	assert_repair_permission,
	dry_run_dimension_repair,
	preview_dimension_repair,
	scan_stock_entry_dimensions,
)
from erpnext_extensions.iran_accounting.tests.hardening.builders import submit_receipt


def _two_cost_centers(company: str) -> tuple[str, str]:
	rows = frappe.db.sql(
		"""
		SELECT name FROM `tabCost Center`
		WHERE company=%s AND IFNULL(is_group,0)=0 AND IFNULL(disabled,0)=0
		ORDER BY name
		LIMIT 5
		""",
		(company,),
	)
	names = [r[0] for r in rows]
	if len(names) < 2:
		raise unittest.SkipTest("Need at least two Cost Centers")
	return names[0], names[1]


def _two_departments(company: str) -> tuple[str, str]:
	rows = frappe.db.sql(
		"""
		SELECT name FROM `tabDepartment`
		WHERE (company=%s OR IFNULL(company,'')='') AND IFNULL(disabled,0)=0
		ORDER BY name
		LIMIT 10
		""",
		(company,),
	)
	names = [r[0] for r in rows if r[0] != "All Departments"]
	if len(names) < 2:
		# Create two test departments
		for label in ("DIM-REPAIR-DEPT-A", "DIM-REPAIR-DEPT-B"):
			if not frappe.db.exists("Department", label):
				frappe.get_doc(
					{"doctype": "Department", "department_name": label, "company": company}
				).insert(ignore_permissions=True)
		names = ["DIM-REPAIR-DEPT-A", "DIM-REPAIR-DEPT-B"]
	return names[0], names[1]


def _uom(item: str) -> str:
	return frappe.db.get_value("Item", item, "stock_uom")


def _make_uniform_mtfm(company: str, dept: str, cc: str) -> frappe.Document:
	"""Submitted MTfM with two item rows sharing dept/CC."""
	wh = get_warehouse(company)
	wh2 = get_second_warehouse(company, wh)
	item_a = ensure_test_item(company, "DIM-REP-A")
	item_b = ensure_test_item(company, "DIM-REP-B")
	submit_receipt(company, item_a, Decimal("10"), Decimal("1000"), wh)
	submit_receipt(company, item_b, Decimal("10"), Decimal("2000"), wh)

	purpose = "Material Transfer for Manufacture"
	ste_type = frappe.db.get_value("Stock Entry Type", {"purpose": purpose}, "name") or purpose

	se = frappe.new_doc("Stock Entry")
	se.company = company
	se.stock_entry_type = ste_type
	se.purpose = purpose
	se.posting_date = nowdate()
	se.posting_time = nowtime()
	se.set_posting_time = 1
	for item, qty, rate in ((item_a, 2, 1000), (item_b, 2, 2000)):
		se.append(
			"items",
			{
				"item_code": item,
				"qty": qty,
				"transfer_qty": qty,
				"conversion_factor": 1,
				"uom": _uom(item),
				"s_warehouse": wh,
				"t_warehouse": wh2,
				"basic_rate": rate,
				"cost_center": cc,
				"department": dept,
			},
		)
	frappe.flags.iran_gate_defaults = True
	se.insert(ignore_permissions=True)
	# Ensure both rows have explicit dims before submit (site defaults may rewrite)
	for row in se.items:
		row.department = dept
		row.cost_center = cc
	se.save(ignore_permissions=True)
	se.submit()
	frappe.db.commit()
	return se


def _poison_one_row(se_name: str, dept_b: str, cc_b: str) -> str:
	"""Simulate historical mixed dimensions via db_set (bypasses Guard)."""
	rows = frappe.get_all(
		"Stock Entry Detail",
		filters={"parent": se_name},
		fields=["name"],
		order_by="idx asc",
	)
	target = rows[-1].name
	frappe.db.set_value(
		"Stock Entry Detail",
		target,
		{"department": dept_b, "cost_center": cc_b},
		update_modified=False,
	)
	frappe.db.commit()
	return target


class TestStockEntryDimensionRepair(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		cls.company = get_irr_company()
		enable_perpetual_inventory(cls.company)
		cls.dept_a, cls.dept_b = _two_departments(cls.company)
		cls.cc_a, cls.cc_b = _two_cost_centers(cls.company)

	def setUp(self):
		frappe.set_user("Administrator")
		frappe.flags.force_dimension_repair_gl_fail = False
		frappe.flags.force_dimension_repair_verify_fail = False

	def _fixture(self):
		"""Return (se, poisoned_row, target_dept, target_cc).

		Site defaults may rewrite dims on submit — use the persisted uniform
		row as the repair target, then poison the last row with alternate dims.
		"""
		se = _make_uniform_mtfm(self.company, self.dept_a, self.cc_a)
		rows = frappe.get_all(
			"Stock Entry Detail",
			filters={"parent": se.name},
			fields=["name", "idx", "department", "cost_center"],
			order_by="idx asc",
		)
		# Force all rows to a known uniform baseline after submit (db_set).
		for r in rows:
			frappe.db.set_value(
				"Stock Entry Detail",
				r.name,
				{"department": self.dept_a, "cost_center": self.cc_a},
				update_modified=False,
			)
		frappe.db.commit()
		poisoned = _poison_one_row(se.name, self.dept_b, self.cc_b)
		se.reload()
		return se, poisoned, self.dept_a, self.cc_a

	def test_r1_scan_dimension_groups(self):
		se, poisoned, dept_a, cc_a = self._fixture()
		scan = scan_stock_entry_dimensions(se.name)
		self.assertEqual(scan["status"], "MIXED_DIMENSIONS")
		self.assertEqual(len(scan["groups"]), 2)
		labels = {g["label"] for g in scan["groups"]}
		self.assertTrue(any(dept_a in lab and cc_a in lab for lab in labels))
		self.assertTrue(any(self.dept_b in lab and self.cc_b in lab for lab in labels))

	def test_r2_preview_only_selected_rows(self):
		se, poisoned, dept_a, cc_a = self._fixture()
		prev = preview_dimension_repair(se.name, [poisoned], dept_a, cc_a)
		changed = {c["before"]["row_name"] for c in prev["changes"]}
		self.assertEqual(changed, {poisoned})
		self.assertEqual(prev["changes"][0]["after"]["department"], dept_a)
		self.assertEqual(prev["changes"][0]["after"]["cost_center"], cc_a)

	def test_r3_r4_r5_r6_dry_run_zero_mutation(self):
		se, poisoned, dept_a, cc_a = self._fixture()
		sed0 = _fetch_sed_snapshot(se.name)
		gl0 = _gl_compare_rows(_fetch_active_gl(se.name))
		sle0 = _fetch_sle_invariants(se.name)
		modified0 = str(frappe.db.get_value("Stock Entry", se.name, "modified"))

		dry = dry_run_dimension_repair(se.name, [poisoned], dept_a, cc_a)
		self.assertTrue(dry["dry_run"])
		self.assertTrue(dry["db_clean"])
		self.assertTrue(dry["modified_unchanged"])
		self.assertEqual(dry["sle_impact"]["changed"], False)
		self.assertTrue(dry["fingerprint"])

		# R3 zero DB mutation
		self.assertEqual(sed0, _fetch_sed_snapshot(se.name))
		# R4 modified unchanged
		self.assertEqual(modified0, str(frappe.db.get_value("Stock Entry", se.name, "modified")))
		# R5 GL unchanged
		self.assertEqual(gl0, _gl_compare_rows(_fetch_active_gl(se.name)))
		# R6 SLE unchanged
		self.assertEqual(sle0, _fetch_sle_invariants(se.name))

	def test_r7_stale_fingerprint_blocked(self):
		se, poisoned, dept_a, cc_a = self._fixture()
		dry = dry_run_dimension_repair(se.name, [poisoned], dept_a, cc_a)
		# Mutate SED after dry run → fingerprint stale
		frappe.db.set_value(
			"Stock Entry Detail",
			poisoned,
			{"department": dept_a},
			update_modified=True,
		)
		frappe.db.set_value("Stock Entry", se.name, "modified", nowdate(), update_modified=True)
		frappe.db.commit()
		with self.assertRaises(frappe.ValidationError) as ctx:
			apply_dimension_repair(
				se.name,
				[poisoned],
				dept_a,
				cc_a,
				dry["fingerprint"],
				confirm=True,
			)
		self.assertIn("Stale", str(ctx.exception))

	def test_r8_unauthorized_user_blocked(self):
		from unittest.mock import patch

		old_user = frappe.session.user
		frappe.session.user = "dim_repair_no_perm@example.com"
		try:
			with patch("frappe.get_roles", return_value=["Stock User"]):
				with self.assertRaises(frappe.PermissionError):
					assert_repair_permission()
		finally:
			frappe.session.user = old_user

	def test_r9_r10_r11_r12_r13_r16_apply_success(self):
		se, poisoned, dept_a, cc_a = self._fixture()
		sle0 = _fetch_sle_invariants(se.name)
		sed0 = _fetch_sed_snapshot(se.name)
		dry = dry_run_dimension_repair(se.name, [poisoned], dept_a, cc_a)
		proposed = dry["proposed_gl"]

		result = apply_dimension_repair(
			se.name,
			[poisoned],
			dept_a,
			cc_a,
			dry["fingerprint"],
			confirm=True,
		)
		self.assertTrue(result["applied"])

		# R9 only approved SED dept/CC
		sed1 = _fetch_sed_snapshot(se.name)
		before_by = {r["name"]: r for r in sed0}
		after_by = {r["name"]: r for r in sed1}
		for name, before in before_by.items():
			after = after_by[name]
			for f in ("qty", "basic_rate", "item_code", "amount"):
				if f in before:
					self.assertEqual(before.get(f), after.get(f), f"field {f}")
			if name == poisoned:
				self.assertEqual(after.get("department"), dept_a)
				self.assertEqual(after.get("cost_center"), cc_a)
			else:
				self.assertEqual(before.get("department"), after.get("department"))
				self.assertEqual(before.get("cost_center"), after.get("cost_center"))

		# R10 GL == dry run proposed
		self.assertEqual(_gl_compare_rows(_fetch_active_gl(se.name)), proposed)

		# R11–R13 SLE invariants
		sle1 = _fetch_sle_invariants(se.name)
		self.assertEqual(len(sle0), len(sle1))
		for a, b in zip(sle0, sle1, strict=True):
			self.assertEqual(a["actual_qty"], b["actual_qty"])
			self.assertEqual(a["stock_value_difference"], b["stock_value_difference"])
			self.assertEqual(a["valuation_rate"], b["valuation_rate"])

		# R16 Guard passes
		se.reload()
		validate_manufacturing_dimension_uniformity(se)

	def test_r14_gl_rebuild_exception_rollback(self):
		se, poisoned, dept_a, cc_a = self._fixture()
		sed0 = _fetch_sed_snapshot(se.name)
		gl0 = _gl_compare_rows(_fetch_active_gl(se.name))
		sle0 = _fetch_sle_invariants(se.name)
		dry = dry_run_dimension_repair(se.name, [poisoned], dept_a, cc_a)

		frappe.flags.force_dimension_repair_gl_fail = True
		with self.assertRaises(frappe.ValidationError):
			apply_dimension_repair(
				se.name,
				[poisoned],
				dept_a,
				cc_a,
				dry["fingerprint"],
				confirm=True,
			)
		frappe.flags.force_dimension_repair_gl_fail = False

		self.assertEqual(sed0, _fetch_sed_snapshot(se.name))
		self.assertEqual(gl0, _gl_compare_rows(_fetch_active_gl(se.name)))
		self.assertEqual(sle0, _fetch_sle_invariants(se.name))

	def test_r15_verify_failure_rollback(self):
		se, poisoned, dept_a, cc_a = self._fixture()
		sed0 = _fetch_sed_snapshot(se.name)
		gl0 = _gl_compare_rows(_fetch_active_gl(se.name))
		sle0 = _fetch_sle_invariants(se.name)
		dry = dry_run_dimension_repair(se.name, [poisoned], dept_a, cc_a)

		frappe.flags.force_dimension_repair_verify_fail = True
		with self.assertRaises(frappe.ValidationError):
			apply_dimension_repair(
				se.name,
				[poisoned],
				dept_a,
				cc_a,
				dry["fingerprint"],
				confirm=True,
			)
		frappe.flags.force_dimension_repair_verify_fail = False

		self.assertEqual(sed0, _fetch_sed_snapshot(se.name))
		self.assertEqual(gl0, _gl_compare_rows(_fetch_active_gl(se.name)))
		self.assertEqual(sle0, _fetch_sle_invariants(se.name))

	def test_r17_second_apply_same_fingerprint_blocked(self):
		se, poisoned, dept_a, cc_a = self._fixture()
		dry = dry_run_dimension_repair(se.name, [poisoned], dept_a, cc_a)
		apply_dimension_repair(
			se.name,
			[poisoned],
			dept_a,
			cc_a,
			dry["fingerprint"],
			confirm=True,
		)
		with self.assertRaises(frappe.ValidationError) as ctx:
			apply_dimension_repair(
				se.name,
				[poisoned],
				dept_a,
				cc_a,
				dry["fingerprint"],
				confirm=True,
			)
		msg = str(ctx.exception)
		self.assertTrue("already used" in msg or "Stale" in msg)

# Copyright (c) 2026, ERPNext Extensions contributors
"""P1–P12: Stock Entry Dimension Repair Desk page / navigation / API gating."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

import frappe

from erpnext_extensions.iran_accounting.manufacturing_dimension.repair import (
	assert_repair_permission,
	dry_run_dimension_repair,
	preview_dimension_repair,
	scan_stock_entry_dimensions,
)
from erpnext_extensions.patches.post_model_sync.ensure_stock_entry_dimension_repair_navigation import (
	CARD,
	LABEL,
	PAGE,
	execute as ensure_navigation,
)

PAGE_DIR = (
	Path(frappe.get_app_path("erpnext_extensions"))
	/ "erpnext_extensions"
	/ "page"
	/ "stock_entry_dimension_repair"
)


class TestStockEntryDimensionRepairPage(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		ensure_navigation()

	def setUp(self):
		frappe.set_user("Administrator")

	def test_p1_page_definition_exists(self):
		self.assertTrue(frappe.db.exists("Page", PAGE))
		doc = frappe.get_doc("Page", PAGE)
		self.assertEqual(doc.title, LABEL)
		self.assertEqual(doc.standard, "Yes")
		self.assertTrue((PAGE_DIR / "stock_entry_dimension_repair.js").is_file())
		self.assertTrue((PAGE_DIR / "stock_entry_dimension_repair.json").is_file())

	def test_p2_route_resolves(self):
		# Desk route is /app/<page_name>; Page.name must match page_name.
		doc = frappe.get_doc("Page", PAGE)
		self.assertEqual(doc.name, "stock-entry-dimension-repair")
		self.assertEqual(doc.page_name, "stock-entry-dimension-repair")

	def test_p3_navigation_entry_exists_once(self):
		found = 0
		for ws_name in ("Stock", "Accounts", "Manufacturing"):
			if not frappe.db.exists("Workspace", ws_name):
				continue
			ws = frappe.get_doc("Workspace", ws_name)
			links = [r for r in (ws.links or []) if (r.link_to or "") == PAGE]
			found += len(links)
			self.assertLessEqual(len(links), 1, f"duplicate links in {ws_name}")
		self.assertGreaterEqual(found, 1)

	def test_p4_repeated_migration_no_duplicate_nav(self):
		ensure_navigation()
		ensure_navigation()
		for ws_name in ("Stock", "Accounts", "Manufacturing"):
			if not frappe.db.exists("Workspace", ws_name):
				continue
			ws = frappe.get_doc("Workspace", ws_name)
			links = [r for r in (ws.links or []) if (r.link_to or "") == PAGE]
			self.assertLessEqual(len(links), 1, f"duplicate after re-run in {ws_name}")
			cards = [r for r in (ws.links or []) if r.type == "Card Break" and (r.label or "") == CARD]
			self.assertLessEqual(len(cards), 1)

	def test_p5_scan_uniform_document(self):
		# Prefer already-repaired canary if present and uniform.
		name = "MAT-STE-2026-40149"
		if not frappe.db.exists("Stock Entry", name):
			self.skipTest("canary missing")
		scan = scan_stock_entry_dimensions(name)
		self.assertEqual(scan["status"], "NO_REPAIR_NEEDED")
		self.assertEqual(len(scan["groups"]), 1)
		g = scan["groups"][0]
		self.assertEqual(g["department"], "واحد بسته بندی - E")
		self.assertEqual(g["cost_center"], "1130 - بسته بندی - E")

	def test_p6_mixed_fixture_groups(self):
		# Lightweight in-memory style via real API after temporary db_set on a test SE
		# is covered by repair tests; here assert scan status logic on a synthetic result
		# by using guard-facing grouping from a known mixed fixture if available.
		from erpnext_extensions.iran_accounting.manufacturing_dimension.tests.test_stock_entry_dimension_repair import (
			TestStockEntryDimensionRepair,
		)

		helper = TestStockEntryDimensionRepair("run")
		helper.setUpClass()
		helper.setUp()
		se, poisoned, dept_a, cc_a = helper._fixture()
		scan = scan_stock_entry_dimensions(se.name)
		self.assertEqual(scan["status"], "MIXED_DIMENSIONS")
		self.assertEqual(len(scan["groups"]), 2)

	def test_p7_preview_no_db_mutation(self):
		name = "MAT-STE-2026-40149"
		if not frappe.db.exists("Stock Entry", name):
			self.skipTest("canary missing")
		modified = str(frappe.db.get_value("Stock Entry", name, "modified"))
		row = frappe.db.get_value(
			"Stock Entry Detail", {"parent": name}, "name", order_by="idx asc"
		)
		dept = frappe.db.get_value("Stock Entry Detail", row, "department")
		cc = frappe.db.get_value("Stock Entry Detail", row, "cost_center")
		preview_dimension_repair(name, [row], dept, cc)
		self.assertEqual(modified, str(frappe.db.get_value("Stock Entry", name, "modified")))

	def test_p8_dry_run_no_db_mutation(self):
		name = "MAT-STE-2026-40149"
		if not frappe.db.exists("Stock Entry", name):
			self.skipTest("canary missing")
		if int(frappe.db.get_value("Stock Entry", name, "docstatus") or 0) != 1:
			self.skipTest("canary not submitted")
		modified = str(frappe.db.get_value("Stock Entry", name, "modified"))
		gl0 = frappe.db.sql(
			"select count(*) from `tabGL Entry` where voucher_type='Stock Entry' and voucher_no=%s and ifnull(is_cancelled,0)=0",
			name,
		)[0][0]
		sle0 = frappe.db.sql(
			"select count(*) from `tabStock Ledger Entry` where voucher_type='Stock Entry' and voucher_no=%s",
			name,
		)[0][0]
		row = frappe.db.get_value(
			"Stock Entry Detail", {"parent": name}, ["name", "department", "cost_center"], as_dict=True
		)
		dry = dry_run_dimension_repair(name, [row.name], row.department, row.cost_center)
		self.assertTrue(dry["db_clean"])
		self.assertEqual(modified, str(frappe.db.get_value("Stock Entry", name, "modified")))
		gl1 = frappe.db.sql(
			"select count(*) from `tabGL Entry` where voucher_type='Stock Entry' and voucher_no=%s and ifnull(is_cancelled,0)=0",
			name,
		)[0][0]
		sle1 = frappe.db.sql(
			"select count(*) from `tabStock Ledger Entry` where voucher_type='Stock Entry' and voucher_no=%s",
			name,
		)[0][0]
		self.assertEqual(gl0, gl1)
		self.assertEqual(sle0, sle1)

	def test_p9_apply_requires_dry_run_fingerprint(self):
		from erpnext_extensions.iran_accounting.manufacturing_dimension.repair import apply_dimension_repair

		with self.assertRaises(frappe.ValidationError):
			apply_dimension_repair(
				"MAT-STE-2026-40149",
				["nbicjg7tfc"],
				"واحد بسته بندی - E",
				"1130 - بسته بندی - E",
				"",
				confirm=True,
			)

	def test_p10_stale_fingerprint_rejected(self):
		from erpnext_extensions.iran_accounting.manufacturing_dimension.repair import apply_dimension_repair

		with self.assertRaises(frappe.ValidationError):
			apply_dimension_repair(
				"MAT-STE-2026-40149",
				["nbicjg7tfc"],
				"واحد بسته بندی - E",
				"1130 - بسته بندی - E",
				"deadbeef" * 8,
				confirm=True,
			)

	def test_p11_permissions_enforced(self):
		old = frappe.session.user
		frappe.session.user = "dim_repair_page_no_perm@example.com"
		try:
			with patch("frappe.get_roles", return_value=["Stock User"]):
				with self.assertRaises(frappe.PermissionError):
					assert_repair_permission()
		finally:
			frappe.session.user = old

	def test_p12_page_roles_include_accounts_manager(self):
		doc = frappe.get_doc("Page", PAGE)
		roles = {r.role for r in doc.roles or []}
		self.assertIn("Accounts Manager", roles)
		self.assertIn("System Manager", roles)

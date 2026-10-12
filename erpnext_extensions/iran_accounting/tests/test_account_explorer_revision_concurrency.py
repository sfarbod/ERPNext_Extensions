# Copyright (c) 2026, Farbod Siyahpoosh and contributors
"""Concurrency tests for Account Explorer accounting revision."""

from __future__ import annotations

import threading
import unittest
import uuid

import frappe

from erpnext_extensions.iran_accounting.account_explorer.cache_revision import (
	REVISION_DOCTYPE,
	bump_accounting_revision,
	get_accounting_revision,
)


class TestAccountingRevisionConcurrency(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		if not frappe.db:
			raise unittest.SkipTest("Database not available")
		cls.company = f"_AE Rev {uuid.uuid4().hex[:8]}"
		if frappe.db.exists("Company", cls.company):
			frappe.delete_doc("Company", cls.company, force=True)
		doc = frappe.get_doc(
			{
				"doctype": "Company",
				"company_name": cls.company,
				"abbr": uuid.uuid4().hex[:5].upper(),
				"default_currency": "IRR",
				"country": "Iran",
			}
		)
		doc.insert(ignore_permissions=True)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.db.delete(REVISION_DOCTYPE, {"company": cls.company})
		if frappe.db.exists("Company", cls.company):
			frappe.delete_doc("Company", cls.company, force=True)
		frappe.db.commit()

	def setUp(self):
		frappe.db.delete(REVISION_DOCTYPE, {"company": self.company})
		frappe.db.commit()

	def test_single_and_sequential_increments(self):
		self.assertEqual(get_accounting_revision(self.company), 1)
		self.assertEqual(bump_accounting_revision(company=self.company), 2)
		self.assertEqual(bump_accounting_revision(company=self.company), 3)
		self.assertEqual(get_accounting_revision(self.company), 3)

	def test_rollback_discards_increment(self):
		bump_accounting_revision(company=self.company)
		frappe.db.commit()
		before = get_accounting_revision(self.company)
		frappe.db.savepoint("ae_rev_rollback")
		bump_accounting_revision(company=self.company)
		mid = get_accounting_revision(self.company)
		self.assertEqual(mid, before + 1)
		frappe.db.rollback(save_point="ae_rev_rollback")
		self.assertEqual(get_accounting_revision(self.company), before)

	def test_ten_concurrent_increments(self):
		bump_accounting_revision(company=self.company)
		frappe.db.commit()
		before = get_accounting_revision(self.company)
		site = frappe.local.site
		errors = []
		barrier = threading.Barrier(10)

		def worker():
			try:
				frappe.init(site=site, sites_path="/workspace/development/frappe-bench/sites")
				frappe.connect()
				barrier.wait(timeout=30)
				bump_accounting_revision(company=self.company)
				frappe.db.commit()
			except Exception as exc:  # noqa: BLE001
				errors.append(exc)
			finally:
				frappe.destroy()

		threads = [threading.Thread(target=worker) for _ in range(10)]
		for thread in threads:
			thread.start()
		for thread in threads:
			thread.join(timeout=60)
		self.assertEqual(errors, [])
		frappe.connect()
		after = get_accounting_revision(self.company)
		self.assertEqual(after, before + 10)

	def test_two_companies_isolated(self):
		other = f"_AE Rev B {uuid.uuid4().hex[:6]}"
		doc = frappe.get_doc(
			{
				"doctype": "Company",
				"company_name": other,
				"abbr": uuid.uuid4().hex[:5].upper(),
				"default_currency": "IRR",
				"country": "Iran",
			}
		)
		doc.insert(ignore_permissions=True)
		frappe.db.commit()
		try:
			a1 = bump_accounting_revision(company=self.company)
			b1 = bump_accounting_revision(company=other)
			a2 = bump_accounting_revision(company=self.company)
			self.assertEqual(a2, a1 + 1)
			self.assertEqual(get_accounting_revision(other), b1)
		finally:
			frappe.db.delete(REVISION_DOCTYPE, {"company": other})
			frappe.delete_doc("Company", other, force=True)
			frappe.db.commit()

	def test_concurrent_initialization(self):
		site = frappe.local.site
		errors = []
		results = []
		barrier = threading.Barrier(5)

		def worker():
			try:
				frappe.init(site=site, sites_path="/workspace/development/frappe-bench/sites")
				frappe.connect()
				barrier.wait(timeout=30)
				results.append(bump_accounting_revision(company=self.company))
				frappe.db.commit()
			except Exception as exc:  # noqa: BLE001
				errors.append(exc)
			finally:
				frappe.destroy()

		threads = [threading.Thread(target=worker) for _ in range(5)]
		for thread in threads:
			thread.start()
		for thread in threads:
			thread.join(timeout=60)
		self.assertEqual(errors, [])
		frappe.connect()
		self.assertEqual(frappe.db.count(REVISION_DOCTYPE, {"company": self.company}), 1)
		self.assertEqual(get_accounting_revision(self.company), 6)
		self.assertEqual(sorted(results), [2, 3, 4, 5, 6])

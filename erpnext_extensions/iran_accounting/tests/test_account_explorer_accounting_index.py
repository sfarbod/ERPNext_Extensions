# Copyright (c) 2026, Farbod Siyahpoosh and contributors
"""Prototype tests for the disabled Redis accounting index."""

from __future__ import annotations

import time
import unittest
from decimal import Decimal

import frappe

from erpnext_extensions.iran_accounting.account_explorer import accounting_index as index


COMPANY = "اسپاد فارمد دارو"
PARENT_NUMBER = "11100409"
FROM_DATE = "2026-03-21"
TO_DATE = "2027-03-20"
EXPECTED_DEBIT_BALANCE = Decimal("994353195185")


class TestAccountingIndexPrototype(unittest.TestCase):
	def setUp(self):
		if not frappe.db or not frappe.db.exists("Company", COMPANY):
			raise unittest.SkipTest("Espad company not available")
		self._previous = getattr(frappe.conf, index.CONFIG_KEY, None)
		frappe.conf[index.CONFIG_KEY] = 0
		index.drop_company_index(COMPANY)

	def tearDown(self):
		index.drop_company_index(COMPANY)
		if self._previous is None:
			try:
				delattr(frappe.conf, index.CONFIG_KEY)
			except Exception:
				frappe.conf[index.CONFIG_KEY] = 0
		else:
			frappe.conf[index.CONFIG_KEY] = self._previous

	def _leaves(self) -> list[str]:
		parent = frappe.db.get_value("Account", {"company": COMPANY, "account_number": PARENT_NUMBER}, "name")
		lft, rgt = frappe.db.get_value("Account", parent, ["lft", "rgt"])
		rows = frappe.db.sql(
			"""
			select name from `tabAccount`
			where company=%s and lft>%s and rgt<%s and is_group=0
			""",
			(COMPANY, lft, rgt),
		)
		return [row[0] for row in rows]

	def test_disabled_by_default_does_not_read_balances(self):
		result = index.get_turnover(COMPANY, ["x"], FROM_DATE, TO_DATE)
		self.assertEqual(result["status"], "disabled")
		self.assertNotIn("debit_balance", result)
		built = index.rebuild_company(COMPANY)
		self.assertEqual(built["status"], "disabled")

	def test_segments_track_leaf_dates_only(self):
		fields = index.segments_for_gl_rows(
			[
				{"account": "11100401", "posting_date": "2026-04-01", "finance_book": ""},
				{"account": "21100101", "posting_date": "2026-04-01", "finance_book": None},
				{"account": "11100401", "posting_date": "2026-04-01", "finance_book": ""},
			]
		)
		self.assertEqual(len(fields), 2)
		self.assertTrue(all("\x1f" in field for field in fields))

	def test_rebuild_matches_gl_and_invalidation_is_targeted(self):
		frappe.conf[index.CONFIG_KEY] = 1
		leaves = self._leaves()
		self.assertGreaterEqual(len(leaves), 1)
		started = time.perf_counter()
		built = index.rebuild_company(COMPANY)
		cold_ms = int((time.perf_counter() - started) * 1000)
		self.assertEqual(built["status"], "ready", built)
		self.assertGreater(built["fields"], 0)
		self.assertGreater(built["memory_bytes"], 0)

		started = time.perf_counter()
		ready = index.get_turnover(COMPANY, leaves, FROM_DATE, TO_DATE, finance_books=[""])
		warm_ms = int((time.perf_counter() - started) * 1000)
		self.assertEqual(ready["status"], "ready", ready)
		self.assertEqual(ready["debit_balance"], EXPECTED_DEBIT_BALANCE)
		self.assertEqual(ready["opening_debit"], Decimal("0"))
		self.assertLess(warm_ms, 1000, f"warm read {warm_ms}ms cold rebuild {cold_ms}ms")

		other = index.segment_field("unrelated-account", "", "2026-05-01")
		index.publish_segment(COMPANY, other, 10, 0, 0)
		kept = index._client().hget(index._keys(COMPANY)["data"], other)

		victim = leaves[0]
		sample_date = frappe.db.sql(
			"""
			select posting_date from `tabGL Entry`
			where company=%s and account=%s and is_cancelled=0
			order by posting_date limit 1
			""",
			(COMPANY, victim),
		)[0][0]
		field = index.segment_field(victim, "", sample_date)
		invalid = index.invalidate_segments(COMPANY, [field])
		self.assertEqual(invalid["invalidated"], 1)
		self.assertEqual(index._client().hget(index._keys(COMPANY)["data"], other), kept)
		blocked = index.get_turnover(COMPANY, leaves, FROM_DATE, TO_DATE, finance_books=[""])
		self.assertEqual(blocked["status"], "incomplete")
		untouched = index.get_turnover(COMPANY, ["unrelated-account"], FROM_DATE, TO_DATE, finance_books=[""])
		self.assertEqual(untouched["status"], "ready")
		self.assertEqual(untouched["debit_balance"], Decimal("10"))

		stamp = invalid["stamps"][field]
		self.assertFalse(index.publish_segment(COMPANY, field, 1, 1, stamp - 1))
		index.invalidate_segments(COMPANY, [field])
		stale_stamp = stamp
		fresh = index.invalidate_segments(COMPANY, [field])
		self.assertFalse(index.publish_segment(COMPANY, field, 1, 1, stale_stamp))
		self.assertTrue(index.publish_segment(COMPANY, field, 5, 1, fresh["stamps"][field]))

		index.drop_company_index(COMPANY)
		missing = index.get_turnover(COMPANY, leaves, FROM_DATE, TO_DATE, finance_books=[""])
		self.assertEqual(missing["status"], "missing")
		self.assertNotIn("debit_balance", missing)
		rebuilt = index.rebuild_company(COMPANY)
		self.assertEqual(rebuilt["status"], "ready")
		again = index.get_turnover(COMPANY, leaves, FROM_DATE, TO_DATE, finance_books=[""])
		self.assertEqual(again["debit_balance"], EXPECTED_DEBIT_BALANCE)
		print(f"INDEX_BENCH cold_ms={cold_ms} warm_ms={warm_ms} fields={built['fields']} memory={built['memory_bytes']}")

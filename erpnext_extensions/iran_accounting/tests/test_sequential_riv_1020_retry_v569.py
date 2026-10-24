# Copyright (c) 2026, ERPNext Extensions contributors
"""Sequential RIV must retry MariaDB 1020 / QueryDeadlockError on RIV progress."""

from __future__ import annotations

import inspect
import unittest

import frappe

from erpnext_extensions.iran_accounting.historical_stock.db_concurrency import (
	DB_1020_RETRY,
	classify_db_concurrency_error,
)


class TestSequentialRiv1020RetryV569(unittest.TestCase):
	def test_classify_1020_on_repost_item_valuation(self):
		exc = frappe.QueryDeadlockError(
			(1020, "Record has changed since last read in table 'tabRepost Item Valuation'")
		)
		self.assertEqual(classify_db_concurrency_error(exc), DB_1020_RETRY)

	def test_execute_one_identity_wraps_concurrency_retry(self):
		from erpnext_extensions.iran_accounting.historical_rebuild import sequential_rebuild as sr

		src = inspect.getsource(sr.execute_one_identity)
		# v5.6.12+: resilient retry module (supersedes short-backoff-only wrapper)
		self.assertIn("run_resilient_riv_execution", src)
		self.assertIn("execute_reposting_entry", src)


if __name__ == "__main__":
	unittest.main()

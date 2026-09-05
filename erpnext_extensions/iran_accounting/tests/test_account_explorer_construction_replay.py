# Copyright (c) 2026, Farbod Siyahpoosh and contributors
"""Diagnostic-only construction helpers — Account summary uses voucher-scoped posted GL.

See ``test_account_explorer_voucher_scoped_gl.py`` for the FINAL Account contract.
"""

from __future__ import annotations

import unittest

from erpnext_extensions.iran_accounting.tests.test_account_explorer_voucher_scoped_gl import (
	TestVoucherScopedAccountContract,
	TestVoucherScopedAccountStockEntries,
)

# Historical discovery names → final contract suite
TestAccountConstructionReplayContract = TestVoucherScopedAccountContract
TestAccountConstructionReplayStockEntries = TestVoucherScopedAccountStockEntries


class TestConstructionReplayDeprecatedAsAccountEngine(unittest.TestCase):
	def test_account_engine_is_sle_scoped_not_construction(self):
		from erpnext_extensions.iran_accounting.account_explorer.filter_axis_matrix import (
			inventory_filters_construction_account_axis,
			inventory_filters_sle_scoped_account_axis,
			inventory_filters_voucher_scope_gl_axis,
		)

		self.assertFalse(inventory_filters_construction_account_axis("account_level"))
		self.assertFalse(inventory_filters_voucher_scope_gl_axis("account_level"))
		self.assertTrue(inventory_filters_sle_scoped_account_axis("account_level"))

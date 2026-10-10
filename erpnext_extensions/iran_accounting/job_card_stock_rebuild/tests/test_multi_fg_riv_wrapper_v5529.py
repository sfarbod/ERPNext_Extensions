# Copyright (c) 2026, ERPNext Extensions contributors
"""Multi-FG Manufacture RIV must keep Iran wrapper active (experiment phase 2).

Reproduces the PO-JOB08604 failure mode: Core-only recalculate assigns the full
material pool to each MAIN_FG row. With the Iran wrapper, Multi-FG closes the
pool once. Without the wrapper, operations must fail closed before persistence.
"""

from __future__ import annotations

import unittest

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.domain.riv_valuation_guard import (
	ensure_iran_riv_recalculate_wrapper_active,
	is_iran_riv_recalculate_wrapper_active,
	make_recalculate_amounts_wrapper,
)
from erpnext_extensions.iran_accounting.integration.bootstrap import apply as bootstrap_apply
from erpnext_extensions.iran_accounting.manufacture_output_contract import (
	apply_same_item_multi_fg,
)


class TestMultiFgRivWrapperV5529(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		# Site-bound frappe.local required for frappe.throw / bootstrap.
		if not getattr(frappe.local, "site", None):
			import os

			# tests/.../job_card_stock_rebuild → bench/sites
			sites_path = os.path.abspath(
				os.path.join(os.path.dirname(__file__), *([".."] * 6), "sites")
			)
			frappe.init("jc-riv-phase2.localhost", sites_path=sites_path)
			frappe.connect()

	def test_wrapper_active_after_bootstrap(self):
		bootstrap_apply()
		self.assertTrue(is_iran_riv_recalculate_wrapper_active())
		ensure_iran_riv_recalculate_wrapper_active(bootstrap=False)

	def test_ensure_fail_closed_when_wrapper_missing(self):
		from erpnext.stock import stock_ledger as sl

		bootstrap_apply()
		live = sl.update_entries_after.recalculate_amounts_in_stock_entry
		# Temporarily simulate missing wrapper (Core-only path).
		core = getattr(live, "_iran_original", live)
		sl.update_entries_after.recalculate_amounts_in_stock_entry = core
		try:
			with self.assertRaises(frappe.ValidationError) as ctx:
				ensure_iran_riv_recalculate_wrapper_active(bootstrap=False)
			self.assertIn("IRAN_RIV_WRAPPER_INACTIVE", str(ctx.exception))
		finally:
			# Restore via bootstrap (idempotent; re-wraps Core original).
			bootstrap_apply()
			# Force reinstall if process flag prevented _patch_stock_ledger_engine path
			if not is_iran_riv_recalculate_wrapper_active():
				sl.update_entries_after.recalculate_amounts_in_stock_entry = (
					make_recalculate_amounts_wrapper(core)
				)
			self.assertTrue(is_iran_riv_recalculate_wrapper_active())

	def test_multi_fg_closes_pool_not_double(self):
		"""Reuse 40687 fixture: Iran Multi-FG allocates pool once, not 2×."""
		from erpnext_extensions.iran_accounting.tests.test_manufacture_output_contract_v5519 import (
			_doc_40687_fixture,
			_env,
			_sa,
		)

		doc = _doc_40687_fixture(core_poisoned=True)
		fg_before = sum(flt(r.basic_amount) for r in doc.items if r.get("is_finished_item"))
		self.assertGreater(fg_before, 1.5 * 1_196_114_744)
		with _env(
			stock_uoms={"FG": "Nos", "RM": "Nos", "OTHER": "Nos", "ZRM": "Nos"},
			main_item_codes={"ZRM": "RM"},
		):
			self.assertTrue(apply_same_item_multi_fg(doc))
		fg_sum = sum(flt(r.basic_amount) for r in doc.items if r.get("is_finished_item"))
		self.assertEqual(fg_sum, 1_196_114_744)
		self.assertEqual(_sa(doc), 0)

	def test_repeated_bootstrap_does_not_stack_wrappers(self):
		bootstrap_apply()
		from erpnext.stock import stock_ledger as sl

		first = sl.update_entries_after.recalculate_amounts_in_stock_entry
		bootstrap_apply()
		bootstrap_apply()
		second = sl.update_entries_after.recalculate_amounts_in_stock_entry
		self.assertTrue(getattr(first, "_iran_riv_recalculate_wrapper", False))
		self.assertTrue(getattr(second, "_iran_riv_recalculate_wrapper", False))
		# Inner original must not itself be a wrapper (no stacking).
		inner = getattr(second, "_iran_original", None)
		self.assertIsNotNone(inner)
		self.assertFalse(getattr(inner, "_iran_riv_recalculate_wrapper", False))


if __name__ == "__main__":
	unittest.main()

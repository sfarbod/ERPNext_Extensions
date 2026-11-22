# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

"""MAT-STE-2026-44477 — post-SLE set_total must not rewrite FG after ABSORB remirror.

After ``refresh_sle_authoritative_sed_from_ledger`` aligns SED to SLE, calling
``patched_set_total_incoming_outgoing_value`` used to re-run STAGE_CO restore and
drop FG amount by exactly ``qty × 1 IRR`` (981), breaking the ledger contract.

Run::

    bench --site <site> run-tests --module \\
      erpnext_extensions.iran_accounting.tests.test_ste_post_sle_set_total_v586
"""

from __future__ import annotations

import unittest
from unittest import mock

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.stock_entry import (
	_stock_entry_sle_already_posted,
	patched_set_total_incoming_outgoing_value,
)


def _row(**kwargs):
	return frappe._dict(kwargs)


class _FakeSE:
	"""Minimal Stock Entry stand-in — avoid frappe._dict (shadows ``.items``)."""

	def __init__(self, **kwargs):
		self.__dict__.update(kwargs)

	def get(self, key, default=None):
		return getattr(self, key, default)


class TestPostSleSetTotalSkipsRestoreV586(unittest.TestCase):
	def test_sle_posted_helper(self):
		doc = _FakeSE(name="STE-POSTED")
		with mock.patch(
			"erpnext_extensions.iran_accounting.stock_entry.frappe.db.exists",
			return_value=1,
		):
			self.assertTrue(_stock_entry_sle_already_posted(doc))
		with mock.patch(
			"erpnext_extensions.iran_accounting.stock_entry.frappe.db.exists",
			return_value=None,
		):
			self.assertFalse(_stock_entry_sle_already_posted(doc))
		self.assertFalse(_stock_entry_sle_already_posted(_FakeSE(name=None)))

	def test_set_total_after_sle_preserves_fg_amount(self):
		"""STAGE_CO restore must not run once SLE exists — FG stays at remirror amount."""
		fg_amt = 1095565104.0
		consume_amt = 1106952098.0
		scrap_amt = 11387448.0
		doc = _FakeSE(
			name="MAT-STE-2026-44477",
			doctype="Stock Entry",
			purpose="Manufacture",
			company="اسپاد فارمد",
			docstatus=1,
			items=[
				_row(
					idx=1,
					item_code="30300052",
					qty=981,
					amount=consume_amt,
					basic_amount=consume_amt,
					basic_rate=1128392,
					valuation_rate=1128392,
					additional_cost=0,
					s_warehouse="WH-SRC",
					t_warehouse=None,
					is_finished_item=0,
					is_scrap_item=0,
				),
				_row(
					idx=2,
					item_code="30200047",
					qty=981,
					amount=fg_amt,
					basic_amount=fg_amt,
					basic_rate=1116784,
					valuation_rate=1116784,
					additional_cost=0,
					s_warehouse=None,
					t_warehouse="WH-FG",
					is_finished_item=1,
					is_scrap_item=0,
				),
				_row(
					idx=3,
					item_code="13200394",
					qty=981,
					amount=scrap_amt,
					basic_amount=scrap_amt,
					basic_rate=11608,
					valuation_rate=11608,
					additional_cost=0,
					s_warehouse=None,
					t_warehouse="WH-SCRAP",
					is_finished_item=0,
					is_scrap_item=1,
				),
			],
		)

		restore_calls = []

		def fake_restore(d):
			restore_calls.append(1)
			# Simulate the historical bug: STAGE_CO restore drops FG by qty×1.
			for row in d.items:
				if row.is_finished_item:
					row.amount = 1095564123.0
					row.basic_amount = 1095564123.0
					row.basic_rate = 1116783
					row.valuation_rate = 1116783

		with mock.patch(
			"erpnext_extensions.iran_accounting.stock_entry.is_irr_company",
			return_value=True,
		), mock.patch(
			"erpnext_extensions.iran_accounting.stock_entry._stock_entry_sle_already_posted",
			return_value=True,
		), mock.patch(
			"erpnext_extensions.iran_accounting.stock_entry._restore_closed_manufacture_contract_after_core_rates",
			side_effect=fake_restore,
		), mock.patch(
			"erpnext_extensions.iran_accounting.stock_entry.align_manufacture_finished_good_residual",
		) as align, mock.patch(
			"erpnext_extensions.iran_accounting.stock_entry.align_zero_value_transfer_totals",
		) as zvt:
			patched_set_total_incoming_outgoing_value(doc)

		self.assertEqual(restore_calls, [])
		align.assert_not_called()
		zvt.assert_not_called()
		fg = doc.items[1]
		self.assertEqual(flt(fg.amount), fg_amt)
		self.assertEqual(flt(fg.basic_rate), 1116784)
		self.assertEqual(flt(doc.total_incoming_value), fg_amt + scrap_amt)
		self.assertEqual(flt(doc.total_outgoing_value), consume_amt)
		self.assertEqual(flt(doc.value_difference), (fg_amt + scrap_amt) - consume_amt)

	def test_set_total_before_sle_still_restores(self):
		doc = _FakeSE(
			name="STE-DRAFT",
			doctype="Stock Entry",
			purpose="Manufacture",
			company="C",
			docstatus=0,
			items=[
				_row(
					idx=1,
					amount=100,
					s_warehouse="S",
					t_warehouse=None,
					is_finished_item=0,
				),
				_row(
					idx=2,
					amount=100,
					s_warehouse=None,
					t_warehouse="T",
					is_finished_item=1,
				),
			],
		)

		with mock.patch(
			"erpnext_extensions.iran_accounting.stock_entry.is_irr_company",
			return_value=True,
		), mock.patch(
			"erpnext_extensions.iran_accounting.stock_entry._stock_entry_sle_already_posted",
			return_value=False,
		), mock.patch(
			"erpnext_extensions.iran_accounting.stock_entry._restore_closed_manufacture_contract_after_core_rates",
		) as restore, mock.patch(
			"erpnext_extensions.iran_accounting.stock_entry.align_manufacture_finished_good_residual",
		) as align, mock.patch(
			"erpnext_extensions.iran_accounting.stock_entry.align_zero_value_transfer_totals",
		) as zvt:
			patched_set_total_incoming_outgoing_value(doc)

		restore.assert_called_once()
		align.assert_called_once()
		zvt.assert_called_once()


class TestMatSte44477SubmitContractV586(unittest.TestCase):
	"""Document-level verification when the canary Stock Entry exists on site."""

	SITE_SE = "MAT-STE-2026-44477"

	def test_submit_passes_ledger_contract(self):
		if not frappe.db.exists("Stock Entry", self.SITE_SE):
			self.skipTest(f"{self.SITE_SE} not on site")

		from erpnext_extensions.iran_accounting.integration.bootstrap import apply as apply_iran
		from erpnext_extensions.iran_accounting.domain.stock_entry_ledger_contract import (
			collect_ledger_contract_failures,
		)
		from erpnext_extensions.iran_accounting.domain.stock_entry_sync import stock_entry_row_amount

		apply_iran()
		se = frappe.get_doc("Stock Entry", self.SITE_SE)

		# Already submitted by E2E QA: verify durable ledger contract + remirror FG.
		if se.docstatus == 1:
			failures = collect_ledger_contract_failures(se.name, se.company)
			self.assertEqual(failures, [], failures)
			fg = next(r for r in se.items if r.item_code == "30200047")
			self.assertEqual(flt(fg.amount), 1095565104.0)
			self.assertEqual(flt(fg.basic_rate), 1116784.0)
			return

		if se.docstatus != 0:
			self.skipTest(f"{self.SITE_SE} docstatus={se.docstatus}")

		for row in se.items:
			has_batch = frappe.db.get_value("Item", row.item_code, "has_batch_no")
			if has_batch and not row.batch_no and not row.serial_and_batch_bundle:
				batch = frappe.db.get_value("Batch", {"item": row.item_code}, "name")
				if batch:
					row.batch_no = batch

		frappe.db.savepoint("v586_se_contract")
		try:
			se.flags.ignore_permissions = True
			se.save()
			se.submit()
			failures = collect_ledger_contract_failures(se.name, se.company)
			self.assertEqual(failures, [], failures)
			fg = next(r for r in se.items if r.item_code == "30200047")
			self.assertEqual(flt(stock_entry_row_amount(fg, se.company)), 1095565104.0)
		finally:
			frappe.db.rollback(save_point="v586_se_contract")


if __name__ == "__main__":
	unittest.main()

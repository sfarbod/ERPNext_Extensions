# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.5.43 — STAGE_CO no longer grants R2 amount-authoritative exemptions.

Superseded by ``test_stage_co_rate_authoritative_sa_v5543.py``. These negatives
prove hidden R2 residuals on STAGE_CO secondaries are rejected by the ledger.
"""

from __future__ import annotations

import unittest

from frappe.utils import flt

from erpnext_extensions.iran_accounting.domain.stock_entry_ledger_contract import (
	_assert_row_composition,
	_manufacture_output_amount_authoritative,
	_manufacture_stage_secondary_amount_authoritative,
)
from erpnext_extensions.iran_accounting.manufacture_output_contract import r2_representation_bound


class _Row:
	def __init__(self, **fields):
		self.__dict__.setdefault("additional_cost", 0)
		self.__dict__.setdefault("landed_cost_voucher_amount", 0)
		self.__dict__.setdefault("is_finished_item", 0)
		self.__dict__.setdefault("s_warehouse", None)
		self.__dict__.setdefault("idx", 0)
		self.__dict__.update(fields)

	def get(self, key, default=None):
		return self.__dict__.get(key, default)


class _Doc:
	def __init__(self, **fields):
		self.__dict__.setdefault("doctype", "Stock Entry")
		self.__dict__.setdefault("purpose", "Manufacture")
		self.__dict__.setdefault("company", "اسپاد فارمد دارو")
		self.__dict__.setdefault("docstatus", 0)
		self.__dict__.setdefault("name", "STE-R2-NEG")
		self.__dict__.setdefault(
			"custom_manufacturing_costing_contract_version", "5.3.43"
		)
		self.__dict__.update(fields)

	def get(self, key, default=None):
		return self.__dict__.get(key, default)


def _by(*, basic_rate=547, basic_amount=7_654_007, qty=14000, name="by"):
	return _Row(
		item_code="30100096",
		qty=qty,
		transfer_qty=qty,
		basic_rate=basic_rate,
		basic_amount=basic_amount,
		amount=basic_amount,
		valuation_rate=basic_rate,
		t_warehouse="CO",
		secondary_item_type="By-Product",
		custom_output_class="CO_PRODUCT",
		name=name,
		idx=2,
	)


class TestStageSecondaryR2RejectedV5543(unittest.TestCase):
	def test_r2_shape_within_bound_still_rejected(self):
		residual = 7_654_007 - 14_000 * 547
		self.assertEqual(residual, -3993)
		self.assertLessEqual(abs(residual), r2_representation_bound(14000))
		by_item = _by()
		doc = _Doc(
			items=[
				_Row(
					item_code="RM",
					qty=1,
					transfer_qty=1,
					basic_rate=7_654_007,
					basic_amount=7_654_007,
					amount=7_654_007,
					valuation_rate=7_654_007,
					s_warehouse="WIP",
					t_warehouse=None,
					name="rm",
					idx=1,
				),
				by_item,
			]
		)
		self.assertFalse(_manufacture_stage_secondary_amount_authoritative(doc, by_item, doc.company))
		self.assertFalse(_manufacture_output_amount_authoritative(doc, by_item, doc.company))
		failures = _assert_row_composition(doc, doc.company)
		self.assertTrue(any("basic_amount == transfer_qty" in f for f in failures))

	def test_qty_times_rate_passes(self):
		by_item = _by(basic_amount=7_658_000, basic_rate=547)
		by_item.valuation_rate = 547
		doc = _Doc(
			items=[
				_Row(
					item_code="RM",
					qty=1,
					transfer_qty=1,
					basic_rate=7_654_007,
					basic_amount=7_654_007,
					amount=7_654_007,
					valuation_rate=7_654_007,
					s_warehouse="WIP",
					t_warehouse=None,
					name="rm",
					idx=1,
				),
				by_item,
			]
		)
		self.assertEqual(flt(by_item.basic_amount), 14_000 * 547)
		self.assertEqual(_assert_row_composition(doc, doc.company), [])


if __name__ == "__main__":
	unittest.main()

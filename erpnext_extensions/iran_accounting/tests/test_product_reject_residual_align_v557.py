# Copyright (c) 2026, ERPNext Extensions contributors
"""TYPE-C Product Reject + operating cost must not be undone by FG residual align."""

from __future__ import annotations

import unittest
from unittest import mock

from frappe.utils import flt

from erpnext_extensions.iran_accounting.manufacture_rounding import (
	align_manufacture_finished_good_residual,
)


class _Row:
	def __init__(self, **fields):
		self.__dict__.setdefault("allow_zero_valuation_rate", 0)
		self.__dict__.setdefault("landed_cost_voucher_amount", 0)
		self.__dict__.setdefault("valuation_type", "")
		self.__dict__.setdefault("custom_output_class", None)
		self.__dict__.setdefault("secondary_item_type", None)
		self.__dict__.update(fields)

	def get(self, key, default=None):
		return self.__dict__.get(key, default)

	def set(self, key, value):
		self.__dict__[key] = value


class _Doc:
	def __init__(self, **fields):
		self.__dict__.setdefault("doctype", "Stock Entry")
		self.__dict__.setdefault("purpose", "Manufacture")
		self.__dict__.setdefault("company", "اسپاد فارمد دارو")
		self.__dict__.setdefault("additional_costs", [])
		self.__dict__.setdefault("custom_manufacturing_costing_contract_version", "5.3.43")
		self.__dict__.update(fields)

	def get(self, key, default=None):
		return self.__dict__.get(key, default)


class _TypeA:
	classification = "TYPE_A_REAL_ADDITIONAL_COST"


class TestProductRejectResidualAlignV557(unittest.TestCase):
	def test_residual_align_preserves_equal_rate_with_header_operating_cost(self):
		"""After equal-rate Product Reject + shared operating cost, do not rewrite FG."""
		issued = 357483
		good_qty = 1223
		scrap_qty = 521
		op_fg = 41309905
		op_rj = 17598087

		consumed = _Row(
			name="c1",
			item_code="30100277",
			qty=good_qty + scrap_qty,
			transfer_qty=good_qty + scrap_qty,
			basic_rate=issued,
			basic_amount=issued * (good_qty + scrap_qty),
			amount=issued * (good_qty + scrap_qty),
			s_warehouse="WIP",
			t_warehouse=None,
			is_finished_item=0,
			additional_cost=0,
		)
		fg = _Row(
			name="fg1",
			item_code="30200076",
			qty=good_qty,
			transfer_qty=good_qty,
			basic_rate=issued,
			basic_amount=issued * good_qty,
			amount=issued * good_qty + op_fg,
			valuation_rate=issued,
			s_warehouse=None,
			t_warehouse="Quarantine",
			is_finished_item=1,
			additional_cost=op_fg,
			custom_output_class="MAIN_FG",
		)
		reject = _Row(
			name="rj1",
			item_code="30200076",
			qty=scrap_qty,
			transfer_qty=scrap_qty,
			basic_rate=issued,
			basic_amount=issued * scrap_qty,
			amount=issued * scrap_qty + op_rj,
			valuation_rate=issued,
			s_warehouse=None,
			t_warehouse="Reject",
			is_finished_item=0,
			secondary_item_type="Scrap",
			valuation_type="Valuation Rate",
			additional_cost=op_rj,
			custom_output_class="MAIN_PRODUCT_REJECT",
		)
		doc = _Doc(items=[consumed, fg, reject])

		with mock.patch(
			"erpnext_extensions.iran_accounting.manufacture_rounding.is_irr_company",
			return_value=True,
		), mock.patch(
			"erpnext_extensions.iran_accounting.manufacture_rounding.get_company_currency",
			return_value="IRR",
		), mock.patch(
			"erpnext_extensions.iran_accounting.domain.manufacture_irr_residual.uses_type_c_sa_residual_policy",
			return_value=True,
		), mock.patch(
			"erpnext_extensions.iran_accounting.domain.manufacture_irr_residual.classify_manufacture_irr_residual",
			return_value=_TypeA(),
		), mock.patch(
			"erpnext_extensions.iran_accounting.scrap_costing._has_product_reject",
			return_value=True,
		):
			align_manufacture_finished_good_residual(doc)
			self.assertEqual(flt(fg.basic_rate), issued)
			self.assertEqual(flt(fg.basic_amount), issued * good_qty)
			self.assertEqual(flt(reject.basic_rate), issued)


if __name__ == "__main__":
	unittest.main()

# Copyright (c) 2026, ERPNext Extensions contributors
"""CASE F — historical Manufacture MAIN_PRODUCT_REJECT left at rate 0.

MAT-STE-2026-27800 row 3 (30200041 → Reject) stayed at basic_rate=0 after a
Completed HRC because unstamped historical Manufacture skipped the output
contract and coverage did not inspect product-reject SED rates.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from frappe.utils import flt

from erpnext_extensions.iran_accounting.scrap_costing import (
	CLASS_MAIN_FG,
	CLASS_MAIN_PRODUCT_REJECT,
	allocate_scrap_absorbed_cost,
	apply_iran_manufacture_output_contract,
	historical_zero_product_reject_needs_repair,
	repair_historical_zero_product_reject,
)


def _row(**kwargs):
	base = {
		"name": "r",
		"idx": 1,
		"item_code": "30200041",
		"qty": 1,
		"transfer_qty": None,
		"basic_rate": 0,
		"basic_amount": 0,
		"valuation_rate": 0,
		"amount": 0,
		"additional_cost": 0,
		"landed_cost_voucher_amount": 0,
		"allow_zero_valuation_rate": 0,
		"is_finished_item": 0,
		"is_scrap_item": 0,
		"secondary_item_type": None,
		"s_warehouse": None,
		"t_warehouse": None,
		"get": None,
	}
	base.update(kwargs)

	def _get(key, default=None):
		return base.get(key, default)

	base["get"] = _get
	return SimpleNamespace(**base)


def _manufacture_doc(fg_qty=493.0, rej_qty=21.0, fg_rate=407414.0, rej_rate=0.0, consume_amount=200855266.0):
	consume = _row(
		name="c1",
		idx=1,
		item_code="30100055",
		qty=514,
		basic_rate=390769,
		basic_amount=consume_amount,
		amount=consume_amount,
		s_warehouse="WIP",
		t_warehouse=None,
		is_finished_item=0,
		secondary_item_type=None,
	)
	fg = _row(
		name="fg1",
		idx=2,
		item_code="30200041",
		qty=fg_qty,
		transfer_qty=fg_qty,
		basic_rate=fg_rate,
		basic_amount=fg_rate * fg_qty,
		valuation_rate=fg_rate,
		amount=fg_rate * fg_qty,
		is_finished_item=1,
		t_warehouse="Quarantine",
		secondary_item_type=None,
	)
	rej = _row(
		name="rej1",
		idx=3,
		item_code="30200041",
		qty=rej_qty,
		transfer_qty=rej_qty,
		basic_rate=rej_rate,
		basic_amount=rej_rate * rej_qty,
		valuation_rate=rej_rate,
		amount=rej_rate * rej_qty,
		is_finished_item=0,
		t_warehouse="Reject",
		secondary_item_type="Scrap",
	)
	doc = SimpleNamespace(
		doctype="Stock Entry",
		purpose="Manufacture",
		docstatus=1,
		company="اسپاد فارمد دارو",
		additional_costs=[],
		items=[consume, fg, rej],
		get=lambda k, d=None: getattr(doc, k, d),
	)
	return doc


class TestCaseFProductRejectUnitV569(unittest.TestCase):
	def test_needs_repair_when_reject_zero(self):
		doc = _manufacture_doc()
		with mock.patch(
			"erpnext_extensions.iran_accounting.scrap_costing.is_irr_company",
			return_value=True,
		), mock.patch(
			"erpnext_extensions.iran_accounting.scrap_costing.classify_manufacture_outputs",
			return_value={
				CLASS_MAIN_FG: [doc.items[1]],
				CLASS_MAIN_PRODUCT_REJECT: [doc.items[2]],
			},
		):
			self.assertTrue(historical_zero_product_reject_needs_repair(doc))

	def test_allocate_shares_integer_rate(self):
		doc = _manufacture_doc()
		with mock.patch(
			"erpnext_extensions.iran_accounting.scrap_costing.is_irr_company",
			return_value=True,
		), mock.patch(
			"erpnext_extensions.iran_accounting.scrap_costing.classify_manufacture_outputs",
			return_value={
				CLASS_MAIN_FG: [doc.items[1]],
				CLASS_MAIN_PRODUCT_REJECT: [doc.items[2]],
				"COMPONENT_SCRAP": [],
				"BULK_SCRAP": [],
				"CO_PRODUCT": [],
				"CO_PRODUCT_REJECT": [],
				"OTHER_OUTPUT": [],
			},
		), mock.patch(
			"erpnext_extensions.iran_accounting.scrap_costing.get_company_currency",
			return_value="IRR",
		), mock.patch(
			"erpnext_extensions.iran_accounting.scrap_costing.get_currency_precision",
			return_value=0,
		), mock.patch(
			"erpnext_extensions.iran_accounting.scrap_costing.is_scrap_row",
			side_effect=lambda r: r is doc.items[2],
		), mock.patch(
			"erpnext_extensions.iran_accounting.domain.manufacture_irr_residual.uses_type_c_sa_residual_policy",
			return_value=False,
		):
			self.assertTrue(allocate_scrap_absorbed_cost(doc))
		fg, rej = doc.items[1], doc.items[2]
		self.assertEqual(flt(fg.basic_rate), flt(rej.basic_rate))
		self.assertEqual(flt(fg.basic_rate), 390769.0)
		self.assertEqual(flt(fg.basic_amount) + flt(rej.basic_amount), 200855266.0)

	def test_unstamped_apply_routes_to_historical_repair(self):
		doc = _manufacture_doc()
		with mock.patch(
			"erpnext_extensions.iran_accounting.scrap_costing.is_irr_company",
			return_value=True,
		), mock.patch(
			"erpnext_extensions.iran_accounting.manufacture_stage_costing.uses_v533_contract",
			return_value=False,
		), mock.patch(
			"erpnext_extensions.iran_accounting.scrap_costing.repair_historical_zero_product_reject",
			return_value=True,
		) as repair:
			self.assertTrue(apply_iran_manufacture_output_contract(doc))
			repair.assert_called_once_with(doc)

	def test_verify_basic_refuses_zero_reject(self):
		from erpnext_extensions.iran_accounting.historical_rebuild.processor import (
			manufacture_product_reject_rate_issues,
		)

		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.processor.frappe"
		) as m_frappe:
			m_frappe.db.get_value.return_value = "Manufacture"
			m_frappe.get_doc.return_value = _manufacture_doc()
			with mock.patch(
				"erpnext_extensions.iran_accounting.scrap_costing.historical_zero_product_reject_needs_repair",
				return_value=True,
			), mock.patch(
				"erpnext_extensions.iran_accounting.scrap_costing.classify_manufacture_outputs",
				return_value={
					CLASS_MAIN_PRODUCT_REJECT: [_manufacture_doc().items[2]],
				},
			):
				issues = manufacture_product_reject_rate_issues("MAT-STE-2026-27800")
		self.assertTrue(issues)
		self.assertEqual(issues[0]["reason"], "zero_main_product_reject_rate")

	def test_morning_before_afternoon_time_normalize(self):
		from erpnext_extensions.iran_accounting.historical_rebuild.sequential_rebuild import (
			normalize_posting_time,
		)

		self.assertLess(normalize_posting_time("7:54:56"), normalize_posting_time("17:17:48"))


class TestCaseFRepairHelperV569(unittest.TestCase):
	def test_repair_helper_calls_allocate_when_needed(self):
		doc = _manufacture_doc()
		with mock.patch(
			"erpnext_extensions.iran_accounting.scrap_costing.historical_zero_product_reject_needs_repair",
			return_value=True,
		), mock.patch(
			"erpnext_extensions.iran_accounting.scrap_costing.allocate_scrap_absorbed_cost",
			return_value=True,
		) as alloc, mock.patch(
			"erpnext_extensions.iran_accounting.manufacture_output_contract.claim_product_reject_ownership"
		) as claim:
			self.assertTrue(repair_historical_zero_product_reject(doc))
			alloc.assert_called_once_with(doc)
			claim.assert_called_once_with(doc)


if __name__ == "__main__":
	unittest.main()

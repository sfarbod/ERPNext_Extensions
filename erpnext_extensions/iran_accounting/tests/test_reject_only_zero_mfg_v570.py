# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.7.0 — reject-only Manufacture with illegal zero MAIN_PRODUCT_REJECT.

Mirrors MAT-STE-2026-27570: Job Card finished_good = scrap item, no MAIN_FG,
valued consume, reject at rate 0 → exaggerated Stock Adjustment unless repaired.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from frappe.utils import flt

from erpnext_extensions.iran_accounting.historical_rebuild.campaign import _resolve_start_date
from erpnext_extensions.iran_accounting.scrap_costing import (
	CLASS_MAIN_FG,
	CLASS_MAIN_PRODUCT_REJECT,
	allocate_reject_only_absorbed_cost,
	apply_iran_manufacture_output_contract,
	historical_zero_reject_only_needs_repair,
	repair_historical_zero_reject_only,
)


def _row(**kwargs):
	base = {
		"name": "r",
		"idx": 1,
		"item_code": "30100015",
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


def _reject_only_doc(consume_amount=721096838.0, rej_qty=1694.0, rej_rate=0.0):
	consume = _row(
		name="c1",
		idx=1,
		item_code="30100059",
		qty=1694,
		basic_rate=425677,
		basic_amount=consume_amount,
		amount=consume_amount,
		s_warehouse="WIP",
		t_warehouse=None,
	)
	rej = _row(
		name="rej1",
		idx=2,
		item_code="30100015",
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
	return SimpleNamespace(
		doctype="Stock Entry",
		purpose="Manufacture",
		docstatus=1,
		company="اسپاد فارمد دارو",
		work_order="MFG-WO-2026-00593",
		job_card="PO-JOB07953",
		items=[consume, rej],
		additional_costs=[],
		get=lambda k, d=None: getattr(
			SimpleNamespace(
				doctype="Stock Entry",
				purpose="Manufacture",
				docstatus=1,
				company="اسپاد فارمد دارو",
				work_order="MFG-WO-2026-00593",
				job_card="PO-JOB07953",
				items=[consume, rej],
				additional_costs=[],
			),
			k,
			d,
		),
	)


class TestRejectOnlyZeroManufactureV570(unittest.TestCase):
	def test_needs_repair_when_no_main_fg(self):
		doc = _reject_only_doc()
		with (
			mock.patch(
				"erpnext_extensions.iran_accounting.scrap_costing.is_irr_company",
				return_value=True,
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.scrap_costing.classify_manufacture_outputs",
				return_value={
					CLASS_MAIN_FG: [],
					CLASS_MAIN_PRODUCT_REJECT: [doc.items[1]],
					"CO_PRODUCT": [],
					"CO_PRODUCT_REJECT": [],
					"COMPONENT_SCRAP": [],
					"BULK_SCRAP": [],
					"OTHER_OUTPUT": [],
				},
			),
		):
			self.assertTrue(historical_zero_reject_only_needs_repair(doc))

	def test_allocate_prices_reject_from_consume_pool(self):
		doc = _reject_only_doc()
		with (
			mock.patch(
				"erpnext_extensions.iran_accounting.scrap_costing.is_irr_company",
				return_value=True,
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.scrap_costing.get_company_currency",
				return_value="IRR",
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.scrap_costing.classify_manufacture_outputs",
				return_value={
					CLASS_MAIN_FG: [],
					CLASS_MAIN_PRODUCT_REJECT: [doc.items[1]],
					"CO_PRODUCT": [],
					"CO_PRODUCT_REJECT": [],
					"COMPONENT_SCRAP": [],
					"BULK_SCRAP": [],
					"OTHER_OUTPUT": [],
				},
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.scrap_costing.apply_bulk_scrap_zero_valuation",
				return_value=False,
			),
		):
			self.assertTrue(allocate_reject_only_absorbed_cost(doc))
		rej = doc.items[1]
		self.assertEqual(flt(rej.basic_rate), 425677.0)
		self.assertEqual(flt(rej.basic_amount), 425677.0 * 1694.0)
		# Consume unchanged.
		self.assertEqual(flt(doc.items[0].basic_amount), 721096838.0)

	def test_intentional_zero_not_repaired(self):
		doc = _reject_only_doc(rej_rate=0.0)
		# Keep .get() backing store in sync with the attribute (SimpleNamespace stub).
		rej = doc.items[1]
		rej.allow_zero_valuation_rate = 1
		orig_get = rej.get

		def _get(key, default=None):
			if key == "allow_zero_valuation_rate":
				return 1
			return orig_get(key, default)

		rej.get = _get
		with (
			mock.patch(
				"erpnext_extensions.iran_accounting.scrap_costing.is_irr_company",
				return_value=True,
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.scrap_costing.classify_manufacture_outputs",
				return_value={
					CLASS_MAIN_FG: [],
					CLASS_MAIN_PRODUCT_REJECT: [doc.items[1]],
					"CO_PRODUCT": [],
					"CO_PRODUCT_REJECT": [],
					"COMPONENT_SCRAP": [],
					"BULK_SCRAP": [],
					"OTHER_OUTPUT": [],
				},
			),
		):
			self.assertFalse(historical_zero_reject_only_needs_repair(doc))

	def test_contract_calls_reject_only_when_unstamped(self):
		doc = _reject_only_doc()
		with (
			mock.patch(
				"erpnext_extensions.iran_accounting.scrap_costing.is_irr_company",
				return_value=True,
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.manufacture_stage_costing.uses_v533_contract",
				return_value=False,
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.scrap_costing.repair_historical_zero_product_reject",
				return_value=False,
			) as fg_path,
			mock.patch(
				"erpnext_extensions.iran_accounting.scrap_costing.repair_historical_zero_reject_only",
				return_value=True,
			) as only_path,
		):
			self.assertTrue(apply_iran_manufacture_output_contract(doc))
			fg_path.assert_called_once()
			only_path.assert_called_once()

	def test_repair_claims_ownership(self):
		doc = _reject_only_doc()
		with (
			mock.patch(
				"erpnext_extensions.iran_accounting.scrap_costing.historical_zero_reject_only_needs_repair",
				return_value=True,
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.scrap_costing.allocate_reject_only_absorbed_cost",
				return_value=True,
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.manufacture_output_contract.claim_product_reject_ownership"
			) as claim,
		):
			self.assertTrue(repair_historical_zero_reject_only(doc))
			claim.assert_called_once_with(doc)


class TestCampaignStartDateV570(unittest.TestCase):
	def test_start_date_alias_resolves(self):
		self.assertEqual(str(_resolve_start_date(start_date="2026-06-16")), "2026-06-16")
		self.assertEqual(str(_resolve_start_date(posting_boundary_from="2026-06-16")), "2026-06-16")
		self.assertIsNone(_resolve_start_date())
		self.assertEqual(
			str(_resolve_start_date(start_date="2026-07-01", posting_boundary_from="2026-01-01")),
			"2026-07-01",
		)

	def test_start_date_omitted_is_none(self):
		self.assertIsNone(_resolve_start_date(start_date=None, posting_boundary_from=None))

# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.7.0 — Reject-only Manufacture absorbed-cost allocation.

Mirrors SFG-stage docs such as MAT-STE-2026-27570: Job Card finished_good is the
stage SFG item; STE records Scrap → Reject WH with no MAIN_FG; valued consume must
capitalise onto Reject (material pool + header additional_costs), not full SA.
"""

from __future__ import annotations

import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest import mock

from frappe.utils import flt

from erpnext_extensions.iran_accounting.historical_rebuild.campaign import _resolve_start_date
from erpnext_extensions.iran_accounting.scrap_costing import (
	CLASS_BULK_SCRAP,
	CLASS_COMPONENT_SCRAP,
	CLASS_MAIN_FG,
	CLASS_MAIN_PRODUCT_REJECT,
	allocate_reject_only_absorbed_cost,
	allocate_scrap_absorbed_cost,
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
	}
	base.update(kwargs)
	ns = SimpleNamespace(**base)

	def _get(key, default=None):
		# Read live attributes so _apply / _spread_operating_cost updates are visible.
		return ns.__dict__.get(key, default)

	ns.get = _get
	return ns


def _empty_classes(
	*,
	main_fg=None,
	main_reject=None,
	component=None,
	bulk=None,
	co_product=None,
	co_product_reject=None,
	other=None,
):
	return {
		CLASS_MAIN_FG: list(main_fg or []),
		CLASS_MAIN_PRODUCT_REJECT: list(main_reject or []),
		"CO_PRODUCT": list(co_product or []),
		"CO_PRODUCT_REJECT": list(co_product_reject or []),
		CLASS_COMPONENT_SCRAP: list(component or []),
		CLASS_BULK_SCRAP: list(bulk or []),
		"OTHER_OUTPUT": list(other or []),
	}


def _reject_only_doc(
	consume_amount=721096838.0,
	rej_qty=1694.0,
	rej_rate=0.0,
	*,
	additional_costs=None,
	extra_rejects=None,
	component=None,
	bulk=None,
	docstatus=1,
):
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
	items = [consume, rej]
	if extra_rejects:
		items.extend(extra_rejects)
	if component:
		items.append(component)
	if bulk:
		items.append(bulk)
	addl = list(additional_costs or [])
	ns = SimpleNamespace(
		doctype="Stock Entry",
		purpose="Manufacture",
		docstatus=docstatus,
		company="اسپاد فارمد دارو",
		work_order="MFG-WO-2026-00593",
		job_card="PO-JOB07953",
		items=items,
		additional_costs=addl,
	)
	ns.get = lambda k, d=None: getattr(ns, k, d)
	return ns


def _main_plus_reject_doc(pool=200855266.0, good_qty=512.0, scrap_qty=2.0):
	"""Shape used by CASE F unit tests (shared integer rate)."""
	consume = _row(
		name="c1",
		idx=1,
		item_code="RM",
		qty=1,
		basic_amount=pool,
		amount=pool,
		s_warehouse="WIP",
	)
	fg = _row(
		name="fg1",
		idx=2,
		item_code="30100015",
		qty=good_qty,
		transfer_qty=good_qty,
		is_finished_item=1,
		t_warehouse="Quarantine",
	)
	rej = _row(
		name="rej1",
		idx=3,
		item_code="30100015",
		qty=scrap_qty,
		transfer_qty=scrap_qty,
		t_warehouse="Reject",
		secondary_item_type="Scrap",
	)
	doc = SimpleNamespace(
		doctype="Stock Entry",
		purpose="Manufacture",
		docstatus=1,
		company="اسپاد فارمد دارو",
		items=[consume, fg, rej],
		additional_costs=[],
	)
	doc.get = lambda k, d=None: getattr(doc, k, d)
	return doc


def _addl_cost(amount: float):
	data = {"amount": amount, "base_amount": amount}
	ns = SimpleNamespace(**data)
	ns.get = lambda k, d=None, _data=data: _data.get(k, d)
	return ns


def _allocator_stack(classified, *, type_c: bool = True):
	stack = ExitStack()
	stack.enter_context(
		mock.patch(
			"erpnext_extensions.iran_accounting.scrap_costing.is_irr_company",
			return_value=True,
		)
	)
	stack.enter_context(
		mock.patch(
			"erpnext_extensions.iran_accounting.scrap_costing.classify_manufacture_outputs",
			return_value=classified,
		)
	)
	stack.enter_context(
		mock.patch(
			"erpnext_extensions.iran_accounting.scrap_costing.get_company_currency",
			return_value="IRR",
		)
	)
	stack.enter_context(
		mock.patch(
			"erpnext_extensions.iran_accounting.scrap_costing.get_currency_precision",
			return_value=0,
		)
	)
	stack.enter_context(
		mock.patch(
			"erpnext_extensions.iran_accounting.scrap_costing.apply_bulk_scrap_zero_valuation",
			return_value=False,
		)
	)
	stack.enter_context(
		mock.patch(
			"erpnext_extensions.iran_accounting.domain.manufacture_irr_residual.uses_type_c_sa_residual_policy",
			return_value=type_c,
		)
	)
	return stack


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
				return_value=_empty_classes(main_reject=[doc.items[1]]),
			),
		):
			self.assertTrue(historical_zero_reject_only_needs_repair(doc))

	def test_allocate_prices_reject_from_consume_pool(self):
		doc = _reject_only_doc()
		classified = _empty_classes(main_reject=[doc.items[1]])
		with _allocator_stack(classified):
			self.assertTrue(allocate_reject_only_absorbed_cost(doc))
		rej = doc.items[1]
		self.assertEqual(flt(rej.basic_rate), 425677.0)
		self.assertEqual(flt(rej.basic_amount), 425677.0 * 1694.0)
		self.assertEqual(flt(doc.items[0].basic_amount), 721096838.0)
		# TYPE C: residual SA = pool − composed = 0 for exact division.
		self.assertEqual(721096838.0 - flt(rej.basic_amount), 0.0)

	def test_reject_only_with_header_additional_cost(self):
		"""Header additional_costs capitalise onto Reject via amount, not basic_rate."""
		doc = _reject_only_doc(consume_amount=100000.0, rej_qty=10.0)
		doc.additional_costs = [_addl_cost(5000)]
		classified = _empty_classes(main_reject=[doc.items[1]])
		with _allocator_stack(classified):
			self.assertTrue(allocate_scrap_absorbed_cost(doc))
		rej = doc.items[1]
		self.assertEqual(flt(rej.basic_rate), 10000.0)
		self.assertEqual(flt(rej.basic_amount), 100000.0)
		self.assertEqual(flt(rej.additional_cost), 5000.0)
		self.assertEqual(flt(rej.amount), 105000.0)

	def test_reject_only_multiple_reject_rows_shared_rate(self):
		rej2 = _row(
			name="rej2",
			idx=3,
			item_code="30100015",
			qty=6.0,
			transfer_qty=6.0,
			t_warehouse="Reject",
			secondary_item_type="Scrap",
		)
		doc = _reject_only_doc(consume_amount=100000.0, rej_qty=4.0, extra_rejects=[rej2])
		classified = _empty_classes(main_reject=[doc.items[1], doc.items[2]])
		with _allocator_stack(classified):
			self.assertTrue(allocate_reject_only_absorbed_cost(doc))
		# Shared ROUND_HALF_UP(100000/10) = 10000
		self.assertEqual(flt(doc.items[1].basic_rate), 10000.0)
		self.assertEqual(flt(doc.items[2].basic_rate), 10000.0)
		self.assertEqual(flt(doc.items[1].basic_amount) + flt(doc.items[2].basic_amount), 100000.0)

	def test_reject_only_zero_pool_not_forced(self):
		doc = _reject_only_doc(consume_amount=0.0, rej_qty=10.0)
		doc.items[0].basic_amount = 0
		doc.items[0].amount = 0
		classified = _empty_classes(main_reject=[doc.items[1]])
		with _allocator_stack(classified):
			self.assertFalse(allocate_reject_only_absorbed_cost(doc))
		self.assertEqual(flt(doc.items[1].basic_rate), 0.0)

	def test_main_plus_reject_unchanged_shared_integer_rate(self):
		doc = _main_plus_reject_doc()
		classified = _empty_classes(
			main_fg=[doc.items[1]],
			main_reject=[doc.items[2]],
		)
		with _allocator_stack(classified, type_c=False):
			self.assertTrue(allocate_scrap_absorbed_cost(doc))
		fg, rej = doc.items[1], doc.items[2]
		self.assertEqual(flt(fg.basic_rate), flt(rej.basic_rate))
		self.assertEqual(flt(fg.basic_rate), 390769.0)
		self.assertEqual(flt(fg.basic_amount) + flt(rej.basic_amount), 200855266.0)

	def test_bulk_scrap_stays_zero_on_reject_only_doc(self):
		bulk = _row(
			name="bulk1",
			idx=3,
			item_code="BULK",
			qty=5,
			transfer_qty=5,
			t_warehouse="Scrap WH",
			secondary_item_type="Scrap",
			allow_zero_valuation_rate=1,
			custom_output_class=CLASS_BULK_SCRAP,
		)
		doc = _reject_only_doc(consume_amount=100000.0, rej_qty=10.0, bulk=bulk)
		classified = _empty_classes(
			main_reject=[doc.items[1]],
			bulk=[bulk],
		)
		with _allocator_stack(classified):
			self.assertTrue(allocate_scrap_absorbed_cost(doc))
		self.assertEqual(flt(doc.items[1].basic_rate), 10000.0)
		# Bulk not in scrap_rows → untouched by absorbed allocator rates.
		self.assertEqual(flt(bulk.basic_rate), 0.0)

	def test_component_scrap_keeps_issued_rate(self):
		comp = _row(
			name="comp1",
			idx=3,
			item_code="STOPPER",
			qty=2,
			transfer_qty=2,
			t_warehouse="Reject",
			secondary_item_type="Scrap",
		)
		doc = _reject_only_doc(consume_amount=100000.0, rej_qty=10.0, component=comp)
		# Component priced at issued 500; removed from pool → reject gets 99000/10.
		classified = _empty_classes(
			main_reject=[doc.items[1]],
			component=[comp],
		)
		with _allocator_stack(classified), mock.patch(
			"erpnext_extensions.iran_accounting.scrap_costing._issued_rate_for_component",
			return_value=500.0,
		):
			self.assertTrue(allocate_scrap_absorbed_cost(doc))
		self.assertEqual(flt(comp.basic_rate), 500.0)
		self.assertEqual(flt(comp.basic_amount), 1000.0)
		self.assertEqual(flt(doc.items[1].basic_rate), 9900.0)

	def test_intentional_zero_not_repaired(self):
		doc = _reject_only_doc(rej_rate=0.0)
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
				return_value=_empty_classes(main_reject=[doc.items[1]]),
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

	def test_v533_contract_routes_reject_only_allocator(self):
		"""Live / stamped path Priority 2b must price Reject-only (parity with RIV)."""
		doc = _reject_only_doc(docstatus=0)
		classified = _empty_classes(main_reject=[doc.items[1]])
		with ExitStack() as stack:
			for target, kwargs in (
				("erpnext_extensions.iran_accounting.scrap_costing.is_irr_company", {"return_value": True}),
				(
					"erpnext_extensions.iran_accounting.manufacture_stage_costing.uses_v533_contract",
					{"return_value": True},
				),
				("erpnext_extensions.iran_accounting.manufacture_stage_costing.stamp_contract_version", {}),
				(
					"erpnext_extensions.iran_accounting.manufacture_stage_costing.clear_core_auto_valuation_for_stage_bridge",
					{},
				),
				(
					"erpnext_extensions.iran_accounting.scrap_costing.clear_core_auto_valuation_for_product_reject_bridge",
					{},
				),
				(
					"erpnext_extensions.iran_accounting.manufacture_stage_costing.validate_job_card_secondary_match",
					{},
				),
				(
					"erpnext_extensions.iran_accounting.manufacture_stage_costing.snapshot_equivalent_factors_from_sources",
					{},
				),
				(
					"erpnext_extensions.iran_accounting.scrap_costing.apply_component_scrap_issued_rates",
					{"return_value": False},
				),
				(
					"erpnext_extensions.iran_accounting.manufacture_stage_costing.allocate_stage_output_cost",
					{"return_value": False},
				),
				(
					"erpnext_extensions.iran_accounting.manufacture_stage_costing.has_stage_participating_co_product",
					{"return_value": False},
				),
				(
					"erpnext_extensions.iran_accounting.manufacture_stage_costing.assert_bridged_stage_outputs_priced",
					{},
				),
				(
					"erpnext_extensions.iran_accounting.manufacture_output_contract.same_item_output_family_supported",
					{"return_value": False},
				),
				(
					"erpnext_extensions.iran_accounting.scrap_costing._has_product_reject",
					{"return_value": False},
				),
				(
					"erpnext_extensions.iran_accounting.scrap_costing._has_reject_only",
					{"return_value": True},
				),
				(
					"erpnext_extensions.iran_accounting.scrap_costing.classify_manufacture_outputs",
					{"return_value": classified},
				),
				(
					"erpnext_extensions.iran_accounting.scrap_costing.get_company_currency",
					{"return_value": "IRR"},
				),
				(
					"erpnext_extensions.iran_accounting.scrap_costing.get_currency_precision",
					{"return_value": 0},
				),
				(
					"erpnext_extensions.iran_accounting.scrap_costing.apply_bulk_scrap_zero_valuation",
					{"return_value": False},
				),
				(
					"erpnext_extensions.iran_accounting.domain.manufacture_irr_residual.uses_type_c_sa_residual_policy",
					{"return_value": True},
				),
				("erpnext_extensions.iran_accounting.scrap_costing.assert_bridged_product_reject_priced", {}),
				("erpnext_extensions.iran_accounting.scrap_costing.assert_bridged_component_scrap_priced", {}),
			):
				stack.enter_context(mock.patch(target, **kwargs))
			claim = stack.enter_context(
				mock.patch(
					"erpnext_extensions.iran_accounting.manufacture_output_contract.claim_product_reject_ownership"
				)
			)
			self.assertTrue(apply_iran_manufacture_output_contract(doc))
			claim.assert_called_once_with(doc)
		self.assertEqual(flt(doc.items[1].basic_rate), 425677.0)

	def test_allocate_idempotent_on_retry(self):
		doc = _reject_only_doc()
		classified = _empty_classes(main_reject=[doc.items[1]])
		with _allocator_stack(classified):
			self.assertTrue(allocate_reject_only_absorbed_cost(doc))
			rate1 = flt(doc.items[1].basic_rate)
			amt1 = flt(doc.items[1].basic_amount)
			self.assertTrue(allocate_reject_only_absorbed_cost(doc))
			self.assertEqual(flt(doc.items[1].basic_rate), rate1)
			self.assertEqual(flt(doc.items[1].basic_amount), amt1)

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

	def test_expected_rates_for_six_known_documents(self):
		"""Controlled expected IRR rates from Dev SED consume pools (material only)."""
		cases = [
			(721096838.0, 1694.0, 425677.0, 0.0),
			(1776316890.0, 45.0, 39473709.0, -15.0),
			(113357932.0, 317.0, 357596.0, 0.0),
			(71519200.0, 200.0, 357596.0, 0.0),
			# Live Dev/Prod consume pool for MAT-STE-2026-33323 (846 × 357,596).
			(302526216.0, 49.0, 6174004.0, 20.0),
			(2235304925.0, 133.0, 16806804.0, -7.0),
		]
		for pool, qty, exp_rate, exp_residual in cases:
			doc = _reject_only_doc(consume_amount=pool, rej_qty=qty)
			classified = _empty_classes(main_reject=[doc.items[1]])
			with _allocator_stack(classified):
				self.assertTrue(allocate_reject_only_absorbed_cost(doc))
			rej = doc.items[1]
			self.assertEqual(flt(rej.basic_rate), exp_rate)
			self.assertEqual(pool - flt(rej.basic_amount), exp_residual)


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

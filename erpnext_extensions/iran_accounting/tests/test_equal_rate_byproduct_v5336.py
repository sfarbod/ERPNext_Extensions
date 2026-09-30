# Copyright (c) 2026, ERPNext Extensions contributors
"""Equal-rate By-Product historical repair + apply-runner gates (no DB writes)."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from erpnext_extensions.iran_accounting.historical_stock.manufacture_equal_rate_byproduct import (
	STRATEGY,
	cint_safe,
	find_zero_rate_stage_byproducts,
)
from erpnext_extensions.iran_accounting.historical_stock.next_downtime.apply_runner import (
	root_eligible,
)
from erpnext_extensions.iran_accounting.historical_stock.next_downtime.cross_time import (
	INDEPENDENT_PRODUCTION_FLOWS,
)
from erpnext_extensions.iran_accounting.manufacture_stage_costing import (
	STAGE_EQUIV_BRIDGE_FLAG,
	clear_core_vr_auto_default_for_zero_byproduct,
	is_stage_equivalent_output_candidate,
)


class _Row(SimpleNamespace):
	def get(self, key, default=None):
		return getattr(self, key, default)

	def set(self, key, value):
		setattr(self, key, value)


class _Doc(SimpleNamespace):
	def get(self, key, default=None):
		return getattr(self, key, default)

	def set(self, key, value):
		setattr(self, key, value)


class TestEqualRateByproductHelpers(unittest.TestCase):
	def test_find_zero_rate_byproduct(self):
		doc = _Doc(
			items=[
				_Row(
					item_code="FG",
					qty=10,
					basic_rate=100,
					basic_amount=1000,
					is_finished_item=1,
					t_warehouse="FG-WH",
					s_warehouse=None,
					secondary_item_type=None,
				),
				_Row(
					item_code="BP",
					qty=1,
					basic_rate=0,
					basic_amount=0,
					is_finished_item=0,
					t_warehouse="FG-WH",
					s_warehouse=None,
					secondary_item_type="By-Product",
					valuation_type="Valuation Rate",
				),
			]
		)
		found = find_zero_rate_stage_byproducts(doc)
		self.assertEqual(len(found), 1)
		self.assertEqual(found[0].item_code, "BP")

	def test_clear_vr_only_under_historical_flag_when_rate_zero(self):
		doc = _Doc(_iran_historical_stage_repair=True, company="X")
		row = _Row(
			valuation_type="Valuation Rate",
			basic_rate=0,
			set_basic_rate_manually=0,
			allow_zero_valuation_rate=0,
		)
		self.assertTrue(clear_core_vr_auto_default_for_zero_byproduct(doc, row))
		self.assertFalse(row.valuation_type)
		self.assertTrue(getattr(row, STAGE_EQUIV_BRIDGE_FLAG))

		row2 = _Row(valuation_type="Valuation Rate", basic_rate=500, set_basic_rate_manually=0)
		doc2 = _Doc(_iran_historical_stage_repair=True)
		self.assertFalse(clear_core_vr_auto_default_for_zero_byproduct(doc2, row2))
		self.assertEqual(row2.valuation_type, "Valuation Rate")

	def test_historical_origin_flag_allows_candidate_after_vr_clear(self):
		doc = _Doc(
			doctype="Stock Entry",
			purpose="Manufacture",
			company="اسپاد فارمد دارو",
			docstatus=1,
			custom_manufacturing_costing_contract_version="5.3.35",
			_iran_historical_stage_repair=True,
			job_card="JC-1",
		)
		row = _Row(
			item_code="BP",
			qty=1,
			transfer_qty=1,
			basic_rate=0,
			t_warehouse="WH",
			s_warehouse=None,
			secondary_item_type="By-Product",
			valuation_type=None,
			set_basic_rate_manually=0,
		)
		with patch(
			"erpnext_extensions.iran_accounting.manufacture_stage_costing.is_irr_company",
			return_value=True,
		):
			self.assertTrue(is_stage_equivalent_output_candidate(doc, row))


class TestApplyRunnerGates(unittest.TestCase):
	def test_independent_wo_cross_time_blocked(self):
		root = {
			"strategy": "CROSS_TIME_EXACT",
			"category": "AUTO_REPAIRABLE",
			"detection": "CROSS_TIME",
			"outbound_work_order": "WO-A",
			"inbound_work_order": "WO-B",
			"dry_run_passed": True,
			"deterministic_provenance": True,
			"iran_accounting_compatible": True,
		}
		gate = root_eligible(root)
		self.assertFalse(gate["eligible"])
		self.assertIn("independent_production_flows_no_timestamp_repair", gate["reasons"])

	def test_equal_rate_strategy_eligible_when_gates_pass(self):
		root = {
			"strategy": STRATEGY,
			"category": AUTO_REPAIRABLE if False else "AUTO_REPAIRABLE",
			"dry_run_passed": True,
			"deterministic_provenance": True,
			"iran_accounting_compatible": True,
		}
		from erpnext_extensions.iran_accounting.historical_stock.next_downtime.categories import (
			AUTO_REPAIRABLE as AR,
		)

		root["category"] = AR
		gate = root_eligible(root)
		self.assertTrue(gate["eligible"])

	def test_manual_p1_blocked(self):
		root = {
			"strategy": STRATEGY,
			"category": "MANUAL_BUSINESS_EVIDENCE_REQUIRED",
			"priority": "P1",
			"dry_run_passed": True,
			"deterministic_provenance": True,
		}
		gate = root_eligible(root)
		self.assertFalse(gate["eligible"])

	def test_cint_safe(self):
		self.assertEqual(cint_safe(None), 0)
		self.assertEqual(cint_safe("1"), 1)


if __name__ == "__main__":
	unittest.main()

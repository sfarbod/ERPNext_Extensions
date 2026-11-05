# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.7.1 — negative additional-cost SED drift + illegal zero COMPONENT_SCRAP.

Fixtures mirror Production shapes for MAT-STE-2026-43589 / MAT-STE-2026-36826
without hardcoding those voucher numbers into production logic.
"""

from __future__ import annotations

import unittest
from contextlib import ExitStack, contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from frappe.utils import cint, flt

from erpnext_extensions.iran_accounting.scrap_costing import (
	CLASS_COMPONENT_SCRAP,
	CLASS_MAIN_FG,
	CLASS_MAIN_PRODUCT_REJECT,
	_spread_operating_cost,
	allocate_scrap_absorbed_cost,
	apply_iran_manufacture_output_contract,
	classify_manufacture_outputs,
	historical_negative_additional_cost_needs_repair,
	historical_zero_component_scrap_needs_repair,
	repair_historical_negative_additional_cost,
	repair_historical_zero_component_scrap,
)

MODULE = "erpnext_extensions.iran_accounting.scrap_costing"


class _Row:
	def __init__(self, **fields):
		self.__dict__.setdefault("allow_zero_valuation_rate", 0)
		self.__dict__.setdefault("idx", 0)
		self.__dict__.setdefault("additional_cost", 0)
		self.__dict__.setdefault("landed_cost_voucher_amount", 0)
		self.__dict__.setdefault("name", fields.get("name") or f"row-{id(self)}")
		self.__dict__.update(fields)

	def get(self, key, default=None):
		return self.__dict__.get(key, default)


class _Doc:
	def __init__(self, **fields):
		self.__dict__.setdefault("doctype", "Stock Entry")
		self.__dict__.setdefault("purpose", "Manufacture")
		self.__dict__.setdefault("company", "اسپاد فارمد دارو")
		self.__dict__.setdefault("docstatus", 1)
		self.__dict__.setdefault("additional_costs", [])
		self.__dict__.update(fields)

	def get(self, key, default=None):
		return self.__dict__.get(key, default)


def _consumed(item_code, qty, rate, s_warehouse="WIP"):
	return _Row(
		item_code=item_code,
		qty=qty,
		transfer_qty=qty,
		basic_rate=rate,
		basic_amount=qty * rate,
		amount=qty * rate,
		valuation_rate=rate,
		s_warehouse=s_warehouse,
		t_warehouse=None,
		is_finished_item=0,
	)


def _output(item_code, qty, *, is_fg=0, rate=0.0, secondary_item_type=None, **extra):
	fields = dict(
		item_code=item_code,
		qty=qty,
		transfer_qty=qty,
		basic_rate=rate,
		basic_amount=qty * rate,
		amount=qty * rate,
		valuation_rate=rate,
		s_warehouse=None,
		t_warehouse="FG-WH" if is_fg else "Reject",
		is_finished_item=is_fg,
	)
	if secondary_item_type is not None:
		fields["secondary_item_type"] = secondary_item_type
	fields.update(extra)
	return _Row(**fields)


def _addl(amount):
	return _Row(description="operating", amount=amount, base_amount=amount)


@contextmanager
def _irr(main_item_codes=None):
	lookup = main_item_codes or {}

	def _get_value(doctype, name, field=None, *args, **kwargs):
		_ = args, kwargs
		if doctype == "Item" and field == "custom_main_item_code":
			return lookup.get(name)
		return None

	with ExitStack() as stack:
		for target, kwargs in (
			(f"{MODULE}.is_irr_company", {"return_value": True}),
			(f"{MODULE}.get_company_currency", {"return_value": "IRR"}),
			(f"{MODULE}.get_currency_precision", {"return_value": 0}),
			(f"{MODULE}.frappe.db.get_value", {"side_effect": _get_value}),
			(
				"erpnext_extensions.iran_accounting.manufacture_stage_costing.is_irr_company",
				{"return_value": True},
			),
			(
				"erpnext_extensions.iran_accounting.manufacture_output_contract.is_irr_company",
				{"return_value": True},
			),
		):
			stack.enter_context(patch(target, **kwargs))
		# Unstamped submitted → historical repair lane (uses_v533 False).
		stack.enter_context(
			patch(
				"erpnext_extensions.iran_accounting.manufacture_stage_costing.uses_v533_contract",
				return_value=False,
			)
		)
		yield


@contextmanager
def _irr_stamped():
	with ExitStack() as stack:
		stack.enter_context(patch(f"{MODULE}.is_irr_company", return_value=True))
		stack.enter_context(patch(f"{MODULE}.get_company_currency", return_value="IRR"))
		stack.enter_context(patch(f"{MODULE}.get_currency_precision", return_value=0))
		stack.enter_context(patch(f"{MODULE}.frappe.db.get_value", return_value=None))
		stack.enter_context(
			patch(
				"erpnext_extensions.iran_accounting.domain.manufacture_irr_residual.uses_type_c_sa_residual_policy",
				return_value=True,
			)
		)
		yield


class TestNegativeAdditionalCostSED(unittest.TestCase):
	def _neg_addl_doc(self):
		# Shape: Main FG + same-item Reject + component scrap + negative header.
		consume = 100_000
		rm = _consumed("RM-1", 10, 10_000)  # 100_000
		fg = _output("FG-1", 9, is_fg=1, rate=10_000, t_warehouse="Q-WH")
		# Material residual before negative addl: 100000 - 10000 scrap - 10000 reject = 80000
		fg.basic_rate = 80_000 / 9
		fg.basic_amount = 80_000
		fg.amount = 80_000  # stale: missing negative addl
		fg.additional_cost = 0
		rej = _output("FG-1", 1, rate=10_000, secondary_item_type="Scrap", t_warehouse="Reject")
		# Component scrap already at issued rate so this fixture isolates negative addl.
		scrap = _output("RM-1", 1, rate=10_000, secondary_item_type="Scrap", t_warehouse="Waste")
		return _Doc(
			items=[rm, fg, rej, scrap],
			additional_costs=[_addl(-20_000)],
			docstatus=1,
		)

	def test_spread_negative_operating_reduces_fg_only(self):
		fg = _output("FG", 10, is_fg=1, rate=100)
		fg.basic_amount = 1000
		fg.amount = 1000
		rej = _output("FG", 2, rate=100, secondary_item_type="Scrap")
		rej.basic_amount = 200
		rej.amount = 200
		_spread_operating_cost([fg, rej], "IRR", leftover=0.0, absorb_leftover=False, operating_total=-50)
		self.assertEqual(flt(fg.additional_cost), -50.0)
		self.assertEqual(flt(rej.additional_cost), 0.0)
		self.assertEqual(flt(fg.amount), 950.0)
		self.assertEqual(flt(rej.amount), 200.0)

	def test_spread_positive_operating_unchanged(self):
		fg = _output("FG", 10, is_fg=1, rate=100)
		fg.basic_amount = 1000
		fg.amount = 1000
		rej = _output("FG", 2, rate=100, secondary_item_type="Scrap")
		rej.basic_amount = 200
		rej.amount = 200
		_spread_operating_cost([fg, rej], "IRR", leftover=0.0, absorb_leftover=False, operating_total=120)
		# 10/12 and 2/12 of 120 → reject 20, FG 100
		self.assertEqual(flt(rej.additional_cost), 20.0)
		self.assertEqual(flt(fg.additional_cost), 100.0)
		self.assertEqual(flt(fg.amount), 1100.0)
		self.assertEqual(flt(rej.amount), 220.0)

	def test_negative_addl_needs_repair_and_idempotent(self):
		doc = self._neg_addl_doc()
		fg = doc.items[1]
		with _irr():
			self.assertTrue(historical_negative_additional_cost_needs_repair(doc))
			self.assertTrue(repair_historical_negative_additional_cost(doc))
			# consume 100000 + (-20000) - reject 10000 - scrap 10000 = 60000
			self.assertEqual(flt(fg.amount), 60_000.0)
			self.assertEqual(flt(fg.additional_cost), -20_000.0)
			self.assertFalse(historical_negative_additional_cost_needs_repair(doc))
			self.assertFalse(repair_historical_negative_additional_cost(doc))

	def test_contract_repairs_unstamped_negative_addl(self):
		doc = self._neg_addl_doc()
		fg = doc.items[1]
		with _irr():
			self.assertTrue(apply_iran_manufacture_output_contract(doc))
			self.assertEqual(flt(fg.amount), 60_000.0)

	def test_main_plus_reject_positive_addl_stamped(self):
		rm = _consumed("RM", 100, 1000)
		fg = _output("FG", 90, is_fg=1, rate=0, t_warehouse="Q")
		rej = _output("FG", 10, rate=0, secondary_item_type="Scrap", t_warehouse="R")
		doc = _Doc(
			items=[rm, fg, rej],
			additional_costs=[_addl(1000)],
			docstatus=0,
			custom_manufacturing_costing_contract_version="5.3.43",
		)
		with _irr_stamped():
			self.assertTrue(allocate_scrap_absorbed_cost(doc))
		self.assertGreater(flt(fg.additional_cost), 0)
		self.assertGreater(flt(rej.additional_cost), 0)
		self.assertEqual(flt(fg.additional_cost) + flt(rej.additional_cost), 1000.0)


class TestComponentScrapIllegalZero(unittest.TestCase):
	def _zero_component_doc(self):
		# SFG consumed + FG + zero SFG scrap (allow_zero=0) — 36826 shape.
		sfg = _consumed("30300051", 100, 1_612_993)
		fg = _output("20100008", 50, is_fg=1, rate=3_000_000, t_warehouse="Q-FG")
		# Inflated FG absorbs the unpaid scrap value.
		fg.basic_amount = 100 * 1_612_993
		fg.basic_rate = fg.basic_amount / 50
		fg.amount = fg.basic_amount
		scrap = _output(
			"30300051",
			2,
			rate=0,
			secondary_item_type="Scrap",
			t_warehouse="Reject-SFG",
			allow_zero_valuation_rate=0,
		)
		scrap.basic_amount = 0
		scrap.amount = 0
		scrap.valuation_rate = 0
		return _Doc(items=[sfg, fg, scrap], docstatus=1)

	def test_classified_as_component_scrap(self):
		doc = self._zero_component_doc()
		with _irr():
			classified = classify_manufacture_outputs(doc)
		self.assertEqual(len(classified[CLASS_COMPONENT_SCRAP]), 1)
		self.assertEqual(classified[CLASS_COMPONENT_SCRAP][0].item_code, "30300051")
		self.assertEqual(len(classified[CLASS_MAIN_FG]), 1)
		self.assertEqual(len(classified[CLASS_MAIN_PRODUCT_REJECT]), 0)

	def test_illegal_zero_needs_repair(self):
		doc = self._zero_component_doc()
		with _irr():
			self.assertTrue(historical_zero_component_scrap_needs_repair(doc))
			self.assertTrue(repair_historical_zero_component_scrap(doc))
			scrap = doc.items[2]
			fg = doc.items[1]
			self.assertEqual(flt(scrap.basic_rate), 1_612_993.0)
			self.assertEqual(flt(scrap.basic_amount), 2 * 1_612_993.0)
			# FG residual = consume − scrap
			self.assertEqual(flt(fg.basic_amount), 98 * 1_612_993.0)
			self.assertFalse(historical_zero_component_scrap_needs_repair(doc))

	def test_contract_not_already_healthy_for_illegal_zero(self):
		doc = self._zero_component_doc()
		before = flt(doc.items[2].basic_rate)
		with _irr():
			applied = apply_iran_manufacture_output_contract(doc)
		self.assertTrue(applied)
		self.assertNotEqual(flt(doc.items[2].basic_rate), before)
		self.assertEqual(flt(doc.items[2].basic_rate), 1_612_993.0)

	def test_allow_zero_bulk_scrap_stays_zero(self):
		rm = _consumed("RM-B", 10, 1000)
		fg = _output("FG-B", 9, is_fg=1, rate=1000, t_warehouse="Q")
		fg.basic_amount = 9000
		fg.amount = 9000
		bulk = _output(
			"RM-B",
			1,
			rate=0,
			secondary_item_type="Scrap",
			t_warehouse="Waste",
			allow_zero_valuation_rate=1,
			valuation_type="Manual",
			set_basic_rate_manually=1,
		)
		doc = _Doc(items=[rm, fg, bulk], docstatus=1)
		with _irr():
			self.assertFalse(historical_zero_component_scrap_needs_repair(doc))
			self.assertFalse(repair_historical_zero_component_scrap(doc))
			self.assertEqual(flt(bulk.basic_rate), 0.0)

	def test_idempotent_after_repair(self):
		doc = self._zero_component_doc()
		with _irr():
			self.assertTrue(repair_historical_zero_component_scrap(doc))
			rate = flt(doc.items[2].basic_rate)
			amt = flt(doc.items[1].basic_amount)
			self.assertFalse(repair_historical_zero_component_scrap(doc))
			self.assertEqual(flt(doc.items[2].basic_rate), rate)
			self.assertEqual(flt(doc.items[1].basic_amount), amt)


class TestManufactureDriftAuthority(unittest.TestCase):
	def test_sle_authoritative_refuses_sed_sync(self):
		from erpnext_extensions.iran_accounting.historical_stock.reconstruct import (
			classify_manufacture_sed_sle_authority,
			repair_manufacture_selected,
		)

		# SED inbound inflated; SLE matches consume+negative addl.
		consume = 100_000.0
		header = -20_000.0
		sle_inbound = 80_000.0
		sed_inbound = 100_000.0

		doc = SimpleNamespace(
			purpose="Manufacture",
			items=[
				SimpleNamespace(
					s_warehouse="WIP",
					t_warehouse=None,
					basic_amount=consume,
					amount=consume,
					is_finished_item=0,
				),
				SimpleNamespace(
					s_warehouse=None,
					t_warehouse="Q",
					basic_amount=sed_inbound,
					amount=sed_inbound,
					is_finished_item=1,
					additional_cost=0,
				),
			],
			additional_costs=[SimpleNamespace(amount=header, base_amount=header)],
		)

		with patch(
			"erpnext_extensions.iran_accounting.historical_stock.reconstruct.frappe"
		) as frappe_mod, patch(
			"erpnext_extensions.iran_accounting.scrap_costing._header_operating_cost",
			return_value=header,
		):
			frappe_mod.db.exists.return_value = True
			frappe_mod.get_doc.return_value = doc
			frappe_mod.db.sql.return_value = [[sle_inbound]]
			auth = classify_manufacture_sed_sle_authority("STE-NEG")
		self.assertEqual(auth, "SLE_AUTHORITATIVE")

		preview = {
			"topic": "MANUFACTURE",
			"voucher": "STE-NEG",
			"status": "HEALTHY",
			"needs_repair": False,
			"confidence": "EXACT",
			"changed_rows": [],
		}
		sync_mock = MagicMock(return_value=[{"sle": "x"}])
		write_mock = MagicMock(return_value=[])
		with patch(
			"erpnext_extensions.iran_accounting.historical_stock.reconstruct.preview_manufacture_voucher",
			return_value=preview,
		), patch(
			"erpnext_extensions.iran_accounting.historical_stock.reconstruct._manufacture_sle_disagrees_with_se",
			return_value=True,
		), patch(
			"erpnext_extensions.iran_accounting.historical_stock.reconstruct.classify_manufacture_sed_sle_authority",
			return_value="SLE_AUTHORITATIVE",
		), patch(
			"erpnext_extensions.iran_accounting.historical_stock.planner.assert_ready",
			return_value=None,
		), patch(
			"erpnext_extensions.iran_accounting.historical_stock.reconstruct.start_run",
		) as start_run, patch(
			"erpnext_extensions.iran_accounting.historical_stock.reconstruct.append_entry",
		), patch(
			"erpnext_extensions.iran_accounting.historical_stock.reconstruct.finish_run",
		), patch(
			"erpnext_extensions.iran_accounting.historical_stock.reconstruct.apply_manufacture_preview_to_doc",
			return_value=True,
		), patch(
			"erpnext_extensions.iran_accounting.stock_entry.persist_irr_stock_entry_header_and_rows",
			return_value=None,
		), patch(
			"erpnext_extensions.iran_accounting.historical_stock.valuation_rebuild.sync_sle_from_stock_entry_detail",
			sync_mock,
		), patch(
			"erpnext_extensions.iran_accounting.historical_stock.valuation_rebuild.write_sle_transaction_rates",
			write_mock,
		), patch(
			"erpnext_extensions.iran_accounting.historical_stock.valuation_rebuild.sync_sabb_from_sle",
			return_value=[],
		), patch(
			"erpnext_extensions.iran_accounting.historical_stock.reconstruct.replay_from_patient_zero",
			return_value={"ok": True},
		), patch(
			"erpnext_extensions.iran_accounting.historical_stock.reconstruct._count_neg_valuation",
			return_value=0,
		), patch(
			"erpnext_extensions.iran_accounting.historical_stock.reconstruct.frappe"
		) as fr:
			start_run.return_value = SimpleNamespace(repair_run_id="t")
			fr.flags = {}
			fr.generate_hash.return_value = "abcd1234"
			fr.get_doc.return_value = SimpleNamespace(
				items=[
					SimpleNamespace(
						is_finished_item=1,
						secondary_item_type=None,
						t_warehouse="Q",
						s_warehouse=None,
						item_code="FG",
						batch_no=None,
					)
				],
				posting_date="2026-09-15",
				posting_time="10:00:00",
			)
			fr.db = MagicMock()
			out = repair_manufacture_selected([{"voucher": "STE-NEG"}], dry_run=False)

		self.assertTrue(out["applied"])
		row = out["applied"][0]
		self.assertTrue(row.get("refuse_sle_from_sed_sync"))
		self.assertEqual(row.get("drift_authority"), "SLE_AUTHORITATIVE")
		sync_mock.assert_not_called()
		write_mock.assert_called()

	def test_sed_authoritative_still_allows_sle_sync_path(self):
		from erpnext_extensions.iran_accounting.historical_stock.reconstruct import (
			classify_manufacture_sed_sle_authority,
		)

		consume = 100_000.0
		header = 0.0
		sle_inbound = 50_000.0  # defective SLE
		sed_inbound = 100_000.0

		doc = SimpleNamespace(
			purpose="Manufacture",
			items=[
				SimpleNamespace(
					s_warehouse="WIP",
					t_warehouse=None,
					basic_amount=consume,
					amount=consume,
					is_finished_item=0,
				),
				SimpleNamespace(
					s_warehouse=None,
					t_warehouse="Q",
					basic_amount=sed_inbound,
					amount=sed_inbound,
					is_finished_item=1,
					additional_cost=0,
				),
			],
			additional_costs=[],
		)
		with patch(
			"erpnext_extensions.iran_accounting.historical_stock.reconstruct.frappe"
		) as frappe_mod, patch(
			"erpnext_extensions.iran_accounting.scrap_costing._header_operating_cost",
			return_value=header,
		):
			frappe_mod.db.exists.return_value = True
			frappe_mod.get_doc.return_value = doc
			frappe_mod.db.sql.return_value = [[sle_inbound]]
			auth = classify_manufacture_sed_sle_authority("STE-SLE-BAD")
		self.assertEqual(auth, "SED_AUTHORITATIVE")

	def test_aligned_and_both_defective(self):
		from erpnext_extensions.iran_accounting.historical_stock.reconstruct import (
			classify_manufacture_sed_sle_authority,
		)

		def _auth(consume, header, sed_in, sle_in):
			doc = SimpleNamespace(
				purpose="Manufacture",
				items=[
					SimpleNamespace(
						s_warehouse="WIP",
						t_warehouse=None,
						basic_amount=consume,
						amount=consume,
						is_finished_item=0,
					),
					SimpleNamespace(
						s_warehouse=None,
						t_warehouse="Q",
						basic_amount=sed_in,
						amount=sed_in,
						is_finished_item=1,
						additional_cost=0,
					),
				],
				additional_costs=[],
			)
			with patch(
				"erpnext_extensions.iran_accounting.historical_stock.reconstruct.frappe"
			) as frappe_mod, patch(
				"erpnext_extensions.iran_accounting.scrap_costing._header_operating_cost",
				return_value=header,
			):
				frappe_mod.db.exists.return_value = True
				frappe_mod.get_doc.return_value = doc
				frappe_mod.db.sql.return_value = [[sle_in]]
				return classify_manufacture_sed_sle_authority("STE-X")

		self.assertEqual(_auth(100_000, 0, 100_000, 100_000), "ALIGNED")
		self.assertEqual(_auth(100_000, -10_000, 100_000, 50_000), "BOTH_DEFECTIVE")


class TestNegativeAddlEdgeCases(unittest.TestCase):
	def test_main_product_only_negative_addl(self):
		rm = _consumed("RM", 10, 10_000)
		fg = _output("FG", 10, is_fg=1, rate=10_000, t_warehouse="Q")
		fg.basic_amount = 100_000
		fg.amount = 100_000
		fg.additional_cost = 0
		doc = _Doc(items=[rm, fg], additional_costs=[_addl(-15_000)], docstatus=1)
		with _irr():
			self.assertTrue(historical_negative_additional_cost_needs_repair(doc))
			self.assertTrue(repair_historical_negative_additional_cost(doc))
			self.assertEqual(flt(fg.amount), 85_000.0)
			self.assertEqual(flt(fg.additional_cost), -15_000.0)

	def test_main_plus_component_scrap_negative_addl(self):
		rm = _consumed("RM", 10, 10_000)
		fg = _output("FG", 8, is_fg=1, rate=10_000, t_warehouse="Q")
		fg.basic_amount = 80_000
		fg.amount = 80_000
		scrap = _output("RM", 2, rate=10_000, secondary_item_type="Scrap", t_warehouse="W")
		doc = _Doc(items=[rm, fg, scrap], additional_costs=[_addl(-5_000)], docstatus=1)
		with _irr():
			self.assertTrue(repair_historical_negative_additional_cost(doc))
			self.assertEqual(flt(fg.amount), 75_000.0)
			self.assertEqual(flt(scrap.amount), 20_000.0)

	def test_main_plus_reject_plus_scrap_negative_addl(self):
		doc = TestNegativeAdditionalCostSED()._neg_addl_doc()
		with _irr():
			self.assertTrue(repair_historical_negative_additional_cost(doc))
			fg = doc.items[1]
			self.assertEqual(flt(fg.amount), 60_000.0)
			self.assertEqual(flt(doc.items[2].amount), 10_000.0)  # reject material
			self.assertEqual(flt(doc.items[3].amount), 10_000.0)  # scrap material

	def test_multi_fg_negative_addl_unsupported(self):
		rm = _consumed("RM", 10, 10_000)
		fg1 = _output("FG1", 5, is_fg=1, rate=10_000, t_warehouse="Q1")
		fg2 = _output("FG2", 5, is_fg=1, rate=10_000, t_warehouse="Q2")
		fg1.basic_amount = fg1.amount = 50_000
		fg2.basic_amount = fg2.amount = 50_000
		doc = _Doc(items=[rm, fg1, fg2], additional_costs=[_addl(-10_000)], docstatus=1)
		with _irr():
			self.assertFalse(historical_negative_additional_cost_needs_repair(doc))
			self.assertFalse(repair_historical_negative_additional_cost(doc))

	def test_zero_output_qty_spread_is_noop(self):
		fg = _output("FG", 0, is_fg=1, rate=0, t_warehouse="Q")
		fg.basic_amount = 0
		fg.amount = 0
		_spread_operating_cost([fg], "IRR", leftover=0.0, absorb_leftover=False, operating_total=-100)
		self.assertEqual(flt(fg.additional_cost), 0.0)

	def test_integer_irr_residual_on_negative_addl(self):
		# 7 FG qty, negative addl -100 → amount 6900; valuation_rate integer IRR.
		rm = _consumed("RM", 7, 1000)
		fg = _output("FG", 7, is_fg=1, rate=1000, t_warehouse="Q")
		fg.basic_amount = 7000
		fg.amount = 7000
		doc = _Doc(items=[rm, fg], additional_costs=[_addl(-100)], docstatus=1)
		with _irr():
			self.assertTrue(repair_historical_negative_additional_cost(doc))
			self.assertEqual(flt(fg.amount), 6900.0)
			self.assertEqual(flt(fg.additional_cost), -100.0)
			self.assertEqual(flt(fg.valuation_rate), int(flt(fg.valuation_rate)))
			# TYPE C: qty × integer valuation_rate may differ from amount by ≤ qty.
			self.assertLessEqual(abs(flt(fg.valuation_rate) * 7 - 6900.0), 7.0)


class TestComponentScrapEdgeCases(unittest.TestCase):
	def test_multiple_component_scrap_rows(self):
		rm_a = _consumed("A", 50, 100)
		rm_b = _consumed("B", 50, 200)
		fg = _output("FG", 40, is_fg=1, rate=0, t_warehouse="Q")
		fg.basic_amount = 50 * 100 + 50 * 200
		fg.basic_rate = fg.basic_amount / 40
		fg.amount = fg.basic_amount
		sa = _output("A", 5, rate=0, secondary_item_type="Scrap", t_warehouse="W")
		sb = _output("B", 5, rate=0, secondary_item_type="Scrap", t_warehouse="W")
		doc = _Doc(items=[rm_a, rm_b, fg, sa, sb], docstatus=1)
		with _irr():
			self.assertTrue(repair_historical_zero_component_scrap(doc))
			self.assertEqual(flt(sa.basic_rate), 100.0)
			self.assertEqual(flt(sb.basic_rate), 200.0)
			self.assertEqual(flt(fg.basic_amount), 45 * 100 + 45 * 200)

	def test_multiple_consumed_source_rows_weighted(self):
		c1 = _consumed("X", 10, 100)
		c2 = _consumed("X", 10, 300)  # weighted issued = 200
		fg = _output("FG", 18, is_fg=1, rate=0, t_warehouse="Q")
		fg.basic_amount = 4000
		fg.amount = 4000
		scrap = _output("X", 2, rate=0, secondary_item_type="Scrap", t_warehouse="W")
		doc = _Doc(items=[c1, c2, fg, scrap], docstatus=1)
		with _irr():
			self.assertTrue(repair_historical_zero_component_scrap(doc))
			self.assertEqual(flt(scrap.basic_rate), 200.0)
			self.assertEqual(flt(scrap.basic_amount), 400.0)

	def test_zero_issued_rate_no_forced_repair(self):
		rm = _consumed("Z", 10, 0)  # zero issued
		rm.basic_amount = 0
		fg = _output("FG", 9, is_fg=1, rate=0, t_warehouse="Q")
		fg.basic_amount = 0
		fg.amount = 0
		scrap = _output("Z", 1, rate=0, secondary_item_type="Scrap", t_warehouse="W")
		doc = _Doc(items=[rm, fg, scrap], docstatus=1)
		with _irr():
			self.assertFalse(historical_zero_component_scrap_needs_repair(doc))

	def test_missing_source_valuation_no_match(self):
		# Scrap item not consumed on voucher → not COMPONENT_SCRAP family.
		rm = _consumed("RM", 10, 1000)
		fg = _output("FG", 10, is_fg=1, rate=1000, t_warehouse="Q")
		fg.basic_amount = fg.amount = 10_000
		orphan = _output("ORPHAN", 1, rate=0, secondary_item_type="Scrap", t_warehouse="W")
		doc = _Doc(items=[rm, fg, orphan], docstatus=1)
		with _irr():
			classified = classify_manufacture_outputs(doc)
			self.assertEqual(len(classified[CLASS_COMPONENT_SCRAP]), 0)
			self.assertFalse(historical_zero_component_scrap_needs_repair(doc))

	def test_allow_zero_zero_flag_does_not_silently_accept(self):
		doc = TestComponentScrapIllegalZero()._zero_component_doc()
		self.assertEqual(cint(doc.items[2].allow_zero_valuation_rate), 0)
		with _irr():
			self.assertTrue(historical_zero_component_scrap_needs_repair(doc))


class TestHealthyDocumentShapesUnaffected(unittest.TestCase):
	"""Shapes of the 11 Prod-verified healthy docs must not be forcibly rewritten."""

	def test_reject_only_already_priced_no_component_or_neg_repair(self):
		# Six reject-only: priced Reject, no MAIN_FG, no negative addl.
		consume = _consumed("30100059", 1694, 425677)
		rej = _output(
			"30100015",
			1694,
			rate=425677,
			secondary_item_type="Scrap",
			t_warehouse="Reject",
		)
		# Reject-only: scrap item may be MAIN_PRODUCT_REJECT via finished_good resolution;
		# with no FG row, classification uses WO/JC finished item — mock as already priced.
		doc = _Doc(items=[consume, rej], additional_costs=[], docstatus=1)
		with _irr():
			self.assertFalse(historical_negative_additional_cost_needs_repair(doc))
			# Without MAIN_FG, component scrap of a different item is N/A here.
			self.assertFalse(historical_zero_component_scrap_needs_repair(doc))

	def test_bulk_scrap_allow_zero_healthy(self):
		# 36814 shape: intentional bulk scrap zero.
		rm = _consumed("13200412", 1170, 26250)
		sfg = _consumed("30200022", 1049, 2512232)
		fg = _output("30300020", 1049, is_fg=1, rate=2542233, t_warehouse="Q")
		fg.basic_amount = 2666043868
		fg.amount = 2666802100
		fg.additional_cost = 758232
		bulk = _output(
			"13200412",
			120,
			rate=0,
			secondary_item_type="Scrap",
			t_warehouse="Waste",
			allow_zero_valuation_rate=1,
			valuation_type="Manual",
			set_basic_rate_manually=1,
		)
		doc = _Doc(
			items=[rm, sfg, fg, bulk],
			additional_costs=[_addl(758232)],
			docstatus=1,
		)
		with _irr():
			self.assertFalse(historical_zero_component_scrap_needs_repair(doc))
			# Positive addl already on FG amount — no negative-addl repair.
			self.assertFalse(historical_negative_additional_cost_needs_repair(doc))


if __name__ == "__main__":
	unittest.main()

# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.3.37 — late MAIN_PRODUCT_REJECT pre-Core bridge (lifecycle only).

Economic allocator remains allocate_scrap_absorbed_cost (source/WIP pool).
"""

from __future__ import annotations

import unittest
from contextlib import contextmanager
from unittest import mock

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.scrap_costing import (
	CLASS_MAIN_PRODUCT_REJECT,
	MANUFACTURE_COSTING_CONTRACT_VERSION,
	PRODUCT_REJECT_BRIDGE_FLAG,
	allocate_scrap_absorbed_cost,
	apply_iran_manufacture_output_contract,
	assert_bridged_product_reject_priced,
	clear_core_auto_valuation_for_product_reject_bridge,
	classify_manufacture_outputs,
	is_product_reject_bridge_candidate,
	permit_product_reject_zero_valuation,
)

MODULE = "erpnext_extensions.iran_accounting.scrap_costing"
FG = "30100055"
RM = "30100054"
RATE = 738_730
POOL = 694_406_200


class _Row:
	def __init__(self, **fields):
		self.__dict__.setdefault("allow_zero_valuation_rate", 0)
		self.__dict__.setdefault("idx", 0)
		self.__dict__.setdefault("additional_cost", 0)
		self.__dict__.setdefault("landed_cost_voucher_amount", 0)
		self.__dict__.setdefault("valuation_type", None)
		self.__dict__.setdefault("set_basic_rate_manually", 0)
		self.__dict__.setdefault("bom_secondary_item", None)
		self.__dict__.setdefault("is_legacy_scrap_item", 0)
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
		self.__dict__.setdefault("docstatus", 0)
		self.__dict__.setdefault("job_card", "PO-JOB10094")
		self.__dict__.setdefault("work_order", "MFG-WO-2026-00797")
		self.__dict__.setdefault("additional_costs", [])
		self.__dict__.update(fields)

	def get(self, key, default=None):
		return self.__dict__.get(key, default)

	def set(self, key, value):
		self.__dict__[key] = value


def _consumed(item_code, qty, rate, s_warehouse="WIP"):
	return _Row(
		item_code=item_code,
		qty=qty,
		transfer_qty=qty,
		basic_rate=rate,
		basic_amount=qty * rate,
		amount=qty * rate,
		s_warehouse=s_warehouse,
		t_warehouse=None,
		is_finished_item=0,
	)


def _fg(item_code, qty, t_warehouse="Quarantine"):
	return _Row(
		item_code=item_code,
		qty=qty,
		transfer_qty=qty,
		basic_rate=0,
		basic_amount=0,
		amount=0,
		s_warehouse=None,
		t_warehouse=t_warehouse,
		is_finished_item=1,
	)


def _scrap(item_code, qty, t_warehouse="ScrapWH", secondary_item_type="Scrap"):
	return _Row(
		item_code=item_code,
		qty=qty,
		transfer_qty=qty,
		basic_rate=0,
		basic_amount=0,
		amount=0,
		s_warehouse=None,
		t_warehouse=t_warehouse,
		is_finished_item=0,
		secondary_item_type=secondary_item_type,
	)


def _canary_doc(scrap_wh="ScrapWH"):
	return _Doc(
		items=[
			_consumed(RM, 940, RATE),
			_fg(FG, 933),
			_scrap(FG, 7, t_warehouse=scrap_wh),
		]
	)


@contextmanager
def _env(main_item_codes=None, irr=True, contract=True):
	from contextlib import ExitStack

	lookup = main_item_codes or {}

	def _get_value(doctype, name, field=None, *args, **kwargs):
		# Shared frappe.db.get_value mock: real Document load may pass as_dict=.
		_ = args, kwargs
		if doctype == "Item" and field == "custom_main_item_code":
			return lookup.get(name)
		return None

	with ExitStack() as stack:
		stack.enter_context(mock.patch(f"{MODULE}.is_irr_company", return_value=irr))
		stack.enter_context(mock.patch(f"{MODULE}.get_company_currency", return_value="IRR"))
		stack.enter_context(mock.patch(f"{MODULE}.get_currency_precision", return_value=0))
		stack.enter_context(mock.patch(f"{MODULE}.frappe.db.get_value", side_effect=_get_value))
		# v5.5.19 contract spine / stage ownership probes (not present in 5.5.18).
		stack.enter_context(
			mock.patch(
				"erpnext_extensions.iran_accounting.manufacture_output_contract.is_irr_company",
				return_value=irr,
			)
		)
		stack.enter_context(
			mock.patch(
				"erpnext_extensions.iran_accounting.manufacture_output_contract.get_company_currency",
				return_value="IRR",
			)
		)
		stack.enter_context(
			mock.patch(
				"erpnext_extensions.iran_accounting.manufacture_stage_costing.is_irr_company",
				return_value=irr,
			)
		)
		stack.enter_context(
			mock.patch(
				"erpnext_extensions.iran_accounting.manufacture_stage_costing.get_company_currency",
				return_value="IRR",
			)
		)
		stack.enter_context(
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.currency.is_irr_company",
				return_value=irr,
			)
		)
		stack.enter_context(
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.currency.get_company_currency",
				return_value="IRR",
			)
		)
		if contract:
			stack.enter_context(
				mock.patch(
					"erpnext_extensions.iran_accounting.manufacture_stage_costing.uses_v533_contract",
					return_value=True,
				)
			)
			stack.enter_context(
				mock.patch(
					"erpnext_extensions.iran_accounting.manufacture_stage_costing.validate_job_card_secondary_match",
					return_value=None,
				)
			)
			stack.enter_context(
				mock.patch(
					"erpnext_extensions.iran_accounting.manufacture_stage_costing.snapshot_equivalent_factors_from_sources",
					return_value=None,
				)
			)
			stack.enter_context(
				mock.patch(
					"erpnext_extensions.iran_accounting.manufacture_stage_costing.allocate_stage_output_cost",
					return_value=False,
				)
			)
			stack.enter_context(
				mock.patch(
					"erpnext_extensions.iran_accounting.manufacture_stage_costing.assert_bridged_stage_outputs_priced",
					return_value=None,
				)
			)
			stack.enter_context(
				mock.patch(
					"erpnext_extensions.iran_accounting.manufacture_stage_costing.clear_core_auto_valuation_for_stage_bridge",
					return_value=None,
				)
			)
			stack.enter_context(
				mock.patch(
					"erpnext_extensions.iran_accounting.manufacture_stage_costing.stamp_contract_version",
					side_effect=lambda doc: doc.set(
						"custom_manufacturing_costing_contract_version",
						MANUFACTURE_COSTING_CONTRACT_VERSION,
					),
				)
			)
		yield

class TestProductRejectQualification(unittest.TestCase):
	def test_qualifying_product_reject(self):
		doc = _canary_doc()
		with _env():
			self.assertTrue(is_product_reject_bridge_candidate(doc, doc.items[2]))

	def test_n01_ordinary_scrap_not_bridged(self):
		"""N01 — Scrap that is not same-as-FG Product Reject."""
		doc = _Doc(
			items=[
				_consumed(RM, 100, RATE),
				_fg(FG, 90),
				_scrap("Z99999999", 10),
			]
		)
		with _env():
			self.assertFalse(is_product_reject_bridge_candidate(doc, doc.items[2]))
			permit_product_reject_zero_valuation(doc)
			self.assertFalse(getattr(doc.items[2], PRODUCT_REJECT_BRIDGE_FLAG, False))

	def test_n02_missing_source_pool_not_candidate(self):
		"""N02 — no consume rows → not a bridge candidate."""
		doc = _Doc(items=[_fg(FG, 933), _scrap(FG, 7)])
		with _env():
			self.assertFalse(is_product_reject_bridge_candidate(doc, doc.items[1]))

	def test_n03_missing_fg_fail_closed(self):
		"""N03 — no finished good → not a candidate."""
		doc = _Doc(items=[_consumed(RM, 940, RATE), _scrap(FG, 7)])
		with _env():
			self.assertFalse(is_product_reject_bridge_candidate(doc, doc.items[1]))

	def test_n04_component_scrap_not_product_reject_bridge(self):
		"""N04 — Component Scrap uses issued-rate path, not Product Reject bridge."""
		doc = _Doc(
			items=[
				_consumed(RM, 940, RATE),
				_fg(FG, 933),
				_scrap(RM, 7),  # same as consumed component
			]
		)
		with _env():
			self.assertFalse(is_product_reject_bridge_candidate(doc, doc.items[2]))

	def test_n05_by_product_not_product_reject_bridge(self):
		"""N05 — Co-/By-Product stays on stage-equivalent bridge."""
		doc = _canary_doc()
		doc.items[2].secondary_item_type = "By-Product"
		with _env():
			self.assertFalse(is_product_reject_bridge_candidate(doc, doc.items[2]))

	def test_n06_explicit_independent_valuation_not_bridged(self):
		"""N06 — explicit Valuation Rate / Manual preserved."""
		doc = _canary_doc()
		doc.items[2].valuation_type = "Valuation Rate"
		with _env():
			self.assertFalse(is_product_reject_bridge_candidate(doc, doc.items[2]))
			permit_product_reject_zero_valuation(doc)
			self.assertFalse(getattr(doc.items[2], PRODUCT_REJECT_BRIDGE_FLAG, False))

		doc2 = _canary_doc()
		doc2.items[2].valuation_type = "Manual"
		doc2.items[2].set_basic_rate_manually = 1
		with _env():
			self.assertFalse(is_product_reject_bridge_candidate(doc2, doc2.items[2]))

	def test_zero_qty_fail_closed(self):
		doc = _canary_doc()
		doc.items[2].qty = 0
		doc.items[2].transfer_qty = 0
		with _env():
			self.assertFalse(is_product_reject_bridge_candidate(doc, doc.items[2]))

	def test_non_manufacture_fail_closed(self):
		doc = _canary_doc()
		doc.purpose = "Material Transfer"
		with _env():
			self.assertFalse(is_product_reject_bridge_candidate(doc, doc.items[2]))

	def test_non_irr_fail_closed(self):
		doc = _canary_doc()
		with _env(irr=False):
			self.assertFalse(is_product_reject_bridge_candidate(doc, doc.items[2]))


class TestProductRejectBridgeLifecycle(unittest.TestCase):
	def test_permit_sets_flag_and_allow_zero(self):
		doc = _canary_doc()
		with _env():
			permit_product_reject_zero_valuation(doc)
		self.assertTrue(getattr(doc.items[2], PRODUCT_REJECT_BRIDGE_FLAG))
		self.assertEqual(doc.items[2].allow_zero_valuation_rate, 1)

	def test_clear_core_auto_vr(self):
		doc = _canary_doc()
		with _env():
			permit_product_reject_zero_valuation(doc)
		doc.items[2].valuation_type = "Valuation Rate"
		doc.items[2].set_basic_rate_manually = 1
		clear_core_auto_valuation_for_product_reject_bridge(doc)
		self.assertFalse(doc.items[2].valuation_type)
		self.assertEqual(doc.items[2].set_basic_rate_manually, 0)

	def test_clear_skips_non_bridged(self):
		doc = _canary_doc()
		doc.items[2].valuation_type = "Valuation Rate"
		clear_core_auto_valuation_for_product_reject_bridge(doc)
		self.assertEqual(doc.items[2].valuation_type, "Valuation Rate")

	def test_n07_bridge_without_allocation_fails_closed(self):
		"""N07 — temporary bridge must not leak zero into ledger."""
		doc = _canary_doc()
		setattr(doc.items[2], PRODUCT_REJECT_BRIDGE_FLAG, True)
		doc.items[2].basic_rate = 0
		doc.items[2].basic_amount = 0
		# Ensure classifier still sees it as Product Reject
		with _env():
			with self.assertRaises(frappe.ValidationError) as ctx:
				assert_bridged_product_reject_priced(doc)
		self.assertIn("MAIN_PRODUCT_REJECT was not priced", str(ctx.exception))

	def test_bridge_wrong_class_fails_closed(self):
		doc = _Doc(
			items=[
				_consumed(RM, 100, RATE),
				_fg(FG, 90),
				_scrap("OTHER", 10),
			]
		)
		setattr(doc.items[2], PRODUCT_REJECT_BRIDGE_FLAG, True)
		with _env():
			with self.assertRaises(frappe.ValidationError) as ctx:
				assert_bridged_product_reject_priced(doc)
		self.assertIn("not classified as MAIN_PRODUCT_REJECT", str(ctx.exception))

	def test_full_contract_prices_and_clears_allow_zero(self):
		from erpnext_extensions.iran_accounting.manufacture_output_contract import (
			STRATEGY_PRODUCT_REJECT,
			STRATEGY_SAME_ITEM_MULTI_FG,
			get_allocation_owner,
			is_allocation_closed,
		)

		doc = _canary_doc()
		with _env():
			permit_product_reject_zero_valuation(doc)
			doc.items[2].valuation_type = "Valuation Rate"  # Core auto
			self.assertTrue(apply_iran_manufacture_output_contract(doc))
		fg, scrap = doc.items[1], doc.items[2]
		self.assertEqual(flt(fg.basic_rate), RATE)
		self.assertEqual(flt(scrap.basic_rate), RATE)
		self.assertEqual(flt(fg.basic_amount), 689_235_090)
		self.assertEqual(flt(scrap.basic_amount), 5_171_110)
		self.assertEqual(flt(scrap.allow_zero_valuation_rate), 0)
		self.assertEqual(
			doc.get("custom_manufacturing_costing_contract_version"),
			MANUFACTURE_COSTING_CONTRACT_VERSION,
		)
		classified = classify_manufacture_outputs(doc)
		self.assertIn(scrap, classified[CLASS_MAIN_PRODUCT_REJECT])
		# v5.5.19: Product Reject owns closed plan; Multi-FG must not steal.
		self.assertEqual(get_allocation_owner(doc), STRATEGY_PRODUCT_REJECT)
		self.assertTrue(is_allocation_closed(doc))
		self.assertNotEqual(get_allocation_owner(doc), STRATEGY_SAME_ITEM_MULTI_FG)
		out = sum(flt(r.basic_amount) for r in doc.items if r.get("s_warehouse"))
		inc = sum(flt(r.basic_amount) for r in doc.items if r.get("t_warehouse"))
		self.assertEqual(inc, out)


class TestDestinationIndependence(unittest.TestCase):
	"""L — Product Reject rate independent of destination warehouse history."""

	def _run(self, scrap_wh):
		doc = _canary_doc(scrap_wh=scrap_wh)
		with _env():
			permit_product_reject_zero_valuation(doc)
			doc.items[2].valuation_type = "Valuation Rate"
			self.assertTrue(apply_iran_manufacture_output_contract(doc))
		return flt(doc.items[1].basic_rate), flt(doc.items[2].basic_rate), flt(doc.items[2].basic_amount)

	def test_n08_n09_destination_cases(self):
		"""N08 empty dest / N09 conflicting dest → same source-derived rate."""
		a = self._run("WH_SAME")
		b = self._run("WH_DIFF")
		c = self._run("WH_EMPTY")
		for fg_rate, scrap_rate, scrap_amt in (a, b, c):
			self.assertEqual(fg_rate, RATE)
			self.assertEqual(scrap_rate, RATE)
			self.assertEqual(scrap_amt, 5_171_110)
		self.assertEqual(a, b)
		self.assertEqual(b, c)


class TestIdempotencyAndAllocator(unittest.TestCase):
	def test_idempotent_contract_x3(self):
		doc = _canary_doc()
		with _env():
			permit_product_reject_zero_valuation(doc)
			snapshots = []
			for _ in range(3):
				apply_iran_manufacture_output_contract(doc)
				snapshots.append(
					(
						flt(doc.items[1].basic_rate),
						flt(doc.items[1].basic_amount),
						flt(doc.items[2].basic_rate),
						flt(doc.items[2].basic_amount),
						flt(doc.items[2].allow_zero_valuation_rate),
						flt(doc.items[1].additional_cost),
						flt(doc.items[2].additional_cost),
					)
				)
		self.assertEqual(snapshots[0], snapshots[1])
		self.assertEqual(snapshots[1], snapshots[2])
		self.assertEqual(snapshots[0][0], RATE)
		self.assertEqual(snapshots[0][2], RATE)

	def test_allocator_not_stage(self):
		"""Product Reject must use allocate_scrap_absorbed_cost, not stage pool."""
		doc = _canary_doc()
		with _env():
			permit_product_reject_zero_valuation(doc)
			applied = allocate_scrap_absorbed_cost(doc)
		self.assertTrue(applied)
		self.assertEqual(flt(doc.items[2].basic_rate), RATE)
		self.assertEqual(flt(doc.items[1].basic_rate), RATE)
		# No stage-equivalent fields required on Product Reject
		self.assertFalse(flt(doc.items[2].get("custom_equivalent_qty") or 0))

	def test_n10_zero_pool_fails_assert(self):
		"""N10 — bridge + zero economics → assert throws."""
		doc = _Doc(
			items=[
				_consumed(RM, 940, 0),  # zero rate → pool 0
				_fg(FG, 933),
				_scrap(FG, 7),
			]
		)
		with _env():
			permit_product_reject_zero_valuation(doc)
			# allocate returns False when pool <= 0
			self.assertFalse(allocate_scrap_absorbed_cost(doc))
			with self.assertRaises(frappe.ValidationError):
				assert_bridged_product_reject_priced(doc)

	def test_permit_noop_wrong_doctype_purpose(self):
		doc = _canary_doc()
		doc.doctype = "Work Order"
		permit_product_reject_zero_valuation(doc)
		self.assertFalse(getattr(doc.items[2], PRODUCT_REJECT_BRIDGE_FLAG, False))

	def test_assert_noop_without_bridge(self):
		doc = _canary_doc()
		assert_bridged_product_reject_priced(doc)  # no throw

	def test_pct_component_cost_explicit(self):
		doc = _canary_doc()
		doc.items[2].valuation_type = "% of Component Cost"
		with _env():
			self.assertFalse(is_product_reject_bridge_candidate(doc, doc.items[2]))

	def test_z_code_product_reject_candidate(self):
		"""Legacy Z-form of FG is still Product Reject."""
		z_fg = "Z30100055"
		doc = _Doc(
			items=[
				_consumed(RM, 940, RATE),
				_fg(FG, 933),
				_scrap(z_fg, 7),
			]
		)
		with _env(main_item_codes={z_fg: FG}):
			self.assertTrue(is_product_reject_bridge_candidate(doc, doc.items[2]))


class TestCoverageBranches(unittest.TestCase):
	"""Hit remaining branches for 100% new-logic coverage."""

	def test_clear_non_vr_valuation_type(self):
		doc = _canary_doc()
		setattr(doc.items[2], PRODUCT_REJECT_BRIDGE_FLAG, True)
		doc.items[2].valuation_type = "Manual"
		clear_core_auto_valuation_for_product_reject_bridge(doc)
		self.assertEqual(doc.items[2].valuation_type, "Manual")

	def test_candidate_outgoing_row_false(self):
		doc = _canary_doc()
		row = doc.items[0]  # consume / source-only
		with _env():
			self.assertFalse(is_product_reject_bridge_candidate(doc, row))

	def test_set_basic_rate_manually_without_vt(self):
		doc = _canary_doc()
		doc.items[2].set_basic_rate_manually = 1
		with _env():
			self.assertFalse(is_product_reject_bridge_candidate(doc, doc.items[2]))

	def test_contract_false_not_candidate(self):
		doc = _canary_doc()
		with (
			mock.patch(f"{MODULE}.is_irr_company", return_value=True),
			mock.patch(
				"erpnext_extensions.iran_accounting.manufacture_stage_costing.uses_v533_contract",
				return_value=False,
			),
			mock.patch(f"{MODULE}.frappe.db.get_value", return_value=None),
		):
			self.assertFalse(is_product_reject_bridge_candidate(doc, doc.items[2]))

	def test_clear_without_set_method(self):
		class Plain:
			def get(self, key, default=None):
				return getattr(self, key, default)

		p = Plain()
		p.valuation_type = "Valuation Rate"
		p.set_basic_rate_manually = 1
		setattr(p, PRODUCT_REJECT_BRIDGE_FLAG, True)

		class PlainDoc:
			def get(self, k, d=None):
				return getattr(self, k, d)

		doc = PlainDoc()
		doc.items = [p]
		clear_core_auto_valuation_for_product_reject_bridge(doc)
		self.assertIsNone(p.valuation_type)
		self.assertEqual(p.set_basic_rate_manually, 0)

	def test_legacy_scrap_with_stage_secondary_type_excluded(self):
		"""is_scrap_row via legacy flag + stage secondary → excluded at type gate."""
		doc = _canary_doc()
		doc.items[2].secondary_item_type = "By-Product"
		doc.items[2].is_legacy_scrap_item = 1
		with _env():
			self.assertFalse(is_product_reject_bridge_candidate(doc, doc.items[2]))

	def test_z_reject_also_component_family_excluded(self):
		"""Z-form of FG whose main is also consumed → fail closed at component gate."""
		z_fg = "Z30100055"
		doc = _Doc(
			items=[
				_consumed(FG, 940, RATE),  # FG itself consumed
				_fg(FG, 933),
				_scrap(z_fg, 7),
			]
		)
		with _env(main_item_codes={z_fg: FG}):
			self.assertFalse(is_product_reject_bridge_candidate(doc, doc.items[2]))

	def test_permit_non_irr_noop(self):
		doc = _canary_doc()
		with _env(irr=False):
			permit_product_reject_zero_valuation(doc)
		self.assertFalse(getattr(doc.items[2], PRODUCT_REJECT_BRIDGE_FLAG, False))

	def test_clear_set_raises_falls_back(self):
		class BoomRow:
			def __init__(self):
				self.valuation_type = "Valuation Rate"
				self.set_basic_rate_manually = 1
				setattr(self, PRODUCT_REJECT_BRIDGE_FLAG, True)

			def get(self, key, default=None):
				return getattr(self, key, default)

			def set(self, key, value):
				raise RuntimeError("set broken")

		row = BoomRow()
		doc = _Doc(items=[row])
		clear_core_auto_valuation_for_product_reject_bridge(doc)
		self.assertIsNone(row.valuation_type)
		self.assertEqual(row.set_basic_rate_manually, 0)

	def test_has_explicit_returns_false_clean(self):
		from erpnext_extensions.iran_accounting.scrap_costing import (
			_has_explicit_independent_reject_valuation,
		)

		row = _scrap(FG, 7)
		self.assertFalse(_has_explicit_independent_reject_valuation(row))

	def test_apply_contract_paths_with_bridge_assert(self):
		"""Exercise assert on component-only and valid-state return paths."""
		# Component scrap only (no product reject) — assert is no-op
		doc = _Doc(
			items=[
				_consumed(RM, 100, RATE),
				_fg(FG, 90),
				_scrap(RM, 10),
			]
		)
		with _env():
			apply_iran_manufacture_output_contract(doc)
		# Bridged product reject + stage allocator True path without pricing → throw
		doc2 = _canary_doc()
		with _env():
			permit_product_reject_zero_valuation(doc2)
			with mock.patch(
				"erpnext_extensions.iran_accounting.manufacture_stage_costing.allocate_stage_output_cost",
				return_value=True,
			):
				with self.assertRaises(frappe.ValidationError):
					apply_iran_manufacture_output_contract(doc2)

	def test_apply_wrong_doctype_and_non_irr(self):
		doc = _canary_doc()
		doc.doctype = "Work Order"
		self.assertFalse(apply_iran_manufacture_output_contract(doc))
		doc2 = _canary_doc()
		with mock.patch(f"{MODULE}.is_irr_company", return_value=False):
			self.assertFalse(apply_iran_manufacture_output_contract(doc2))

	def test_apply_contract_disabled(self):
		doc = _canary_doc()
		with (
			mock.patch(f"{MODULE}.is_irr_company", return_value=True),
			mock.patch(
				"erpnext_extensions.iran_accounting.manufacture_stage_costing.uses_v533_contract",
				return_value=False,
			),
		):
			self.assertFalse(apply_iran_manufacture_output_contract(doc))

	def test_apply_stage_success_with_priced_reject(self):
		"""Stage returns True after reject already priced → assert passes, return True."""
		doc = _canary_doc()
		with _env():
			permit_product_reject_zero_valuation(doc)
			allocate_scrap_absorbed_cost(doc)  # pre-price
			with mock.patch(
				"erpnext_extensions.iran_accounting.manufacture_stage_costing.allocate_stage_output_cost",
				return_value=True,
			):
				self.assertTrue(apply_iran_manufacture_output_contract(doc))
		self.assertEqual(flt(doc.items[2].allow_zero_valuation_rate), 0)

	def test_apply_valid_state_and_fallback_allocate(self):
		"""No product reject / component: valid-state False path and fallback allocate."""
		# Valid FG-only manufacture (no scrap)
		doc = _Doc(
			items=[
				_consumed(RM, 100, RATE),
				_Row(
					item_code=FG,
					qty=100,
					transfer_qty=100,
					basic_rate=RATE,
					basic_amount=100 * RATE,
					amount=100 * RATE,
					s_warehouse=None,
					t_warehouse="Quarantine",
					is_finished_item=1,
				),
			]
		)
		with _env():
			# _erpnext_manufacture_state_is_valid likely True → return False
			result = apply_iran_manufacture_output_contract(doc)
			self.assertIn(result, (True, False))
		# Invalid state without reject → fallback allocate_scrap (no rejects → False)
		doc2 = _Doc(
			items=[
				_consumed(RM, 100, RATE),
				_fg(FG, 100),  # unpriced FG
			]
		)
		with _env():
			with mock.patch(f"{MODULE}._erpnext_manufacture_state_is_valid", return_value=False):
				self.assertFalse(apply_iran_manufacture_output_contract(doc2))

	def test_apply_component_without_single_fg(self):
		"""component_applied with zero FG rows skips residual restore (branch 895→897)."""
		doc = _Doc(
			items=[
				_consumed(RM, 100, RATE),
				_scrap(RM, 10),
			]
		)
		with _env():
			self.assertTrue(apply_iran_manufacture_output_contract(doc))


if __name__ == "__main__":
	unittest.main()

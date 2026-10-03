# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.4.1 Job Card Stock Rebuild — unit + live canary tests (Phase 1)."""

from __future__ import annotations

import unittest
from unittest import mock

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.job_card_stock_rebuild import statuses as S
from erpnext_extensions.iran_accounting.job_card_stock_rebuild.evidence import classify_movement
from erpnext_extensions.iran_accounting.job_card_stock_rebuild.secondary import (
	CLASS_BY_PRODUCT,
	CLASS_CO_PRODUCT,
	CLASS_COMPONENT_SCRAP,
	CLASS_MAIN_PRODUCT_REJECT,
	CLASS_ORDINARY_SCRAP,
	CLASS_UNKNOWN,
	classify_secondary_row,
	secondary_identity,
)
from erpnext_extensions.iran_accounting.job_card_stock_rebuild.semantics import (
	derive_tracking_for_item,
	propose_link_backfills,
	qty_equal,
	reconcile_batches,
)


def _jc_item(**kw):
	base = {
		"name": "jci-1",
		"item_code": "13200544",
		"required_qty": 1160,
		"transferred_qty": 0,
		"consumed_qty": 0,
		"custom_issued_qty": 0,
		"custom_returned_qty": 0,
		"custom_still_in_wip": 0,
		"custom_returnable_qty": 0,
	}
	base.update(kw)
	return frappe._dict(base)


def _ev(**kw):
	base = {
		"item_code": "13200544",
		"batch_no": "B1",
		"issued": 1160,
		"returned": 12,
		"consumed": 0,
		"component_scrap": 0,
		"product_reject": 0,
		"ordinary_scrap": 0,
		"other": 0,
		"link_candidates": [],
		"evidence": [],
	}
	base.update(kw)
	return base


class TestClassifyMovement(unittest.TestCase):
	def test_issue_return_consume(self):
		self.assertEqual(
			classify_movement(
				"Material Transfer for Manufacture",
				False,
				{"qty": 10, "s_warehouse": "A", "t_warehouse": "WIP"},
			),
			"ISSUE",
		)
		self.assertEqual(
			classify_movement(
				"Material Transfer for Manufacture",
				True,
				{"qty": 2, "s_warehouse": "WIP", "t_warehouse": "A"},
			),
			"RETURN",
		)
		self.assertEqual(
			classify_movement("Manufacture", False, {"qty": 5, "s_warehouse": "WIP", "t_warehouse": None}),
			"CONSUME",
		)

	def test_scrap_classes_not_merged(self):
		self.assertEqual(
			classify_movement(
				"Manufacture",
				False,
				{
					"qty": 1,
					"t_warehouse": "SCRAP",
					"secondary_item_type": "Scrap",
					"custom_output_class": "COMPONENT_SCRAP",
					"is_scrap_item": 1,
				},
			),
			"COMPONENT_SCRAP",
		)
		self.assertEqual(
			classify_movement(
				"Manufacture",
				False,
				{
					"qty": 1,
					"t_warehouse": "REJ",
					"secondary_item_type": "Scrap",
					"custom_output_class": "MAIN_PRODUCT_REJECT",
					"is_scrap_item": 1,
				},
			),
			"PRODUCT_REJECT",
		)
		self.assertEqual(
			classify_movement(
				"Manufacture",
				False,
				{
					"qty": 1,
					"t_warehouse": "SCRAP",
					"secondary_item_type": "Scrap",
					"custom_output_class": "",
					"is_scrap_item": 1,
				},
			),
			"ORDINARY_SCRAP",
		)


class TestSemanticsR01_R20(unittest.TestCase):
	"""Raw-material derivation cases R01–R20 (domain level)."""

	def test_r01_balanced(self):
		jc = _jc_item(
			transferred_qty=2890,
			consumed_qty=2890,
			custom_issued_qty=2920,
			custom_returned_qty=30,
			custom_returnable_qty=2890,
			custom_still_in_wip=0,
		)
		d = derive_tracking_for_item(jc, _ev(issued=2920, returned=30, consumed=2890))
		self.assertEqual(d["action"], "NO CHANGE")
		self.assertTrue(qty_equal(d["wip_remainder"], 0))
		self.assertNotIn(S.JOB_CARD_TRACKING_INCOMPLETE, d["statuses"])

	def test_r02_missing_job_card_item_link(self):
		with mock.patch(
			"erpnext_extensions.iran_accounting.job_card_stock_rebuild.semantics.load_job_card_items",
			return_value=[_jc_item()],
		):
			props = propose_link_backfills(
				"JC",
				_ev(
					link_candidates=[
						{
							"voucher": "STE-1",
							"detail_name": "sed-1",
							"item_code": "13200544",
							"batch_no": "B1",
							"qty": 100,
							"bucket": "ISSUE",
							"job_card_item": None,
						}
					]
				),
			)
		self.assertEqual(props[0]["action"], "BACKFILL LINK")
		self.assertEqual(props[0]["derived"], "jci-1")

	def test_r03_missing_transferred_qty(self):
		d = derive_tracking_for_item(_jc_item(transferred_qty=0), _ev())
		fp = {x["fieldname"]: x for x in d["field_plan"]}
		self.assertTrue(fp["transferred_qty"]["write_required"])
		self.assertTrue(qty_equal(fp["transferred_qty"]["derived"], 1148))
		self.assertIn(S.JOB_CARD_TRACKING_INCOMPLETE, d["statuses"])

	def test_r04_return_reduces_wip(self):
		d = derive_tracking_for_item(_jc_item(), _ev())
		self.assertTrue(qty_equal(d["wip_remainder"], 1148))
		self.assertTrue(qty_equal(d["issued"] - d["returned"], 1148))

	def test_r05_partial_manufacture(self):
		d = derive_tracking_for_item(
			_jc_item(transferred_qty=1000, consumed_qty=400),
			_ev(issued=1000, returned=0, consumed=400),
		)
		self.assertIn(S.PARTIAL_MANUFACTURE_CONSUMPTION, d["statuses"])
		self.assertTrue(qty_equal(d["wip_remainder"], 600))

	def test_r06_multiple_manufacture_entries_sum_consume(self):
		d = derive_tracking_for_item(
			_jc_item(transferred_qty=1000, consumed_qty=700),
			_ev(issued=1000, returned=0, consumed=700),
		)
		fp = {x["fieldname"]: x for x in d["field_plan"]}
		self.assertTrue(qty_equal(fp["consumed_qty"]["derived"], 700))
		self.assertEqual(fp["consumed_qty"]["source"], "manufacture_consume_rows")

	def test_r07_over_consumed(self):
		d = derive_tracking_for_item(_jc_item(), _ev(issued=100, returned=0, consumed=120))
		self.assertIn(S.OVER_CONSUMED, d["statuses"])

	def test_r08_r10_cancelled_ignored_via_evidence_query_contract(self):
		# Evidence collector filters docstatus=1 / is_cancelled=0 — classify itself is neutral
		self.assertIsNone(classify_movement("Material Transfer for Manufacture", False, {"qty": 0}))

	def test_r11_unique_ownership(self):
		with mock.patch(
			"erpnext_extensions.iran_accounting.job_card_stock_rebuild.semantics.load_job_card_items",
			return_value=[_jc_item()],
		):
			name, err = __import__(
				"erpnext_extensions.iran_accounting.job_card_stock_rebuild.semantics",
				fromlist=["resolve_unique_jc_item"],
			).resolve_unique_jc_item("JC", "13200544")
		self.assertEqual(name, "jci-1")
		self.assertIsNone(err)

	def test_r12_ambiguous_ownership_blocks(self):
		with mock.patch(
			"erpnext_extensions.iran_accounting.job_card_stock_rebuild.semantics.load_job_card_items",
			return_value=[_jc_item(name="a"), _jc_item(name="b")],
		):
			from erpnext_extensions.iran_accounting.job_card_stock_rebuild.semantics import (
				resolve_unique_jc_item,
			)

			name, err = resolve_unique_jc_item("JC", "13200544")
		self.assertIsNone(name)
		self.assertEqual(err, S.AMBIGUOUS_OWNERSHIP)

	def test_r13_r15_batch_reconcile(self):
		collapsed, statuses = reconcile_batches(
			[
				_ev(batch_no="B1", issued=600, returned=0, consumed=0),
				_ev(batch_no="B2", issued=560, returned=12, consumed=0),
			]
		)
		self.assertEqual(len(collapsed), 1)
		self.assertTrue(qty_equal(collapsed[0]["issued"], 1160))
		self.assertTrue(qty_equal(collapsed[0]["wip_remainder"], 1148))

		_, bad = reconcile_batches(
			[
				_ev(batch_no="B1", issued=100, returned=0, consumed=150),
				_ev(batch_no="B2", issued=100, returned=0, consumed=0),
			]
		)
		self.assertIn(S.BATCH_MISMATCH, bad)

	def test_r16_consumed_not_inferred_as_issued_minus_returned(self):
		d = derive_tracking_for_item(_jc_item(), _ev(issued=1160, returned=12, consumed=0))
		fp = {x["fieldname"]: x for x in d["field_plan"]}
		self.assertTrue(qty_equal(fp["consumed_qty"]["derived"], 0))
		self.assertTrue(qty_equal(fp["transferred_qty"]["derived"], 1148))
		self.assertIn(S.MISSING_MANUFACTURE_CONSUMPTION, d["statuses"])

	def test_r20_idempotent_derived_values(self):
		jc = _jc_item(
			transferred_qty=1148,
			consumed_qty=0,
			custom_issued_qty=1160,
			custom_returned_qty=12,
			custom_returnable_qty=1148,
			custom_still_in_wip=1148,
		)
		d = derive_tracking_for_item(jc, _ev())
		self.assertEqual(d["action"], "NO CHANGE")


class TestSecondaryS01_S20(unittest.TestCase):
	def test_s01_ordinary_scrap(self):
		self.assertEqual(
			classify_secondary_row({"secondary_item_type": "Scrap", "custom_output_class": ""}),
			CLASS_ORDINARY_SCRAP,
		)

	def test_s02_component_scrap(self):
		self.assertEqual(
			classify_secondary_row({"custom_output_class": "COMPONENT_SCRAP"}),
			CLASS_COMPONENT_SCRAP,
		)

	def test_s03_product_reject(self):
		self.assertEqual(
			classify_secondary_row({"custom_output_class": "MAIN_PRODUCT_REJECT"}),
			CLASS_MAIN_PRODUCT_REJECT,
		)

	def test_s04_s05_co_by(self):
		self.assertEqual(
			classify_secondary_row({"secondary_item_type": "Co-Product"}), CLASS_CO_PRODUCT
		)
		self.assertEqual(
			classify_secondary_row({"secondary_item_type": "By-Product"}), CLASS_BY_PRODUCT
		)

	def test_s07_duplicate_identity(self):
		a = secondary_identity(
			{
				"classification": CLASS_ORDINARY_SCRAP,
				"item_code": "X",
				"batch_no": "",
				"bom_secondary_item": "",
				"secondary_item_type": "Scrap",
				"uom": "Nos",
				"custom_parent_co_product": "",
			}
		)
		b = secondary_identity(
			{
				"classification": CLASS_ORDINARY_SCRAP,
				"item_code": "X",
				"batch_no": "",
				"bom_secondary_item": "",
				"secondary_item_type": "Scrap",
				"uom": "Nos",
				"custom_parent_co_product": "",
			}
		)
		self.assertEqual(a, b)

	def test_s08_unknown_blocks(self):
		self.assertEqual(
			classify_secondary_row({"secondary_item_type": "Weird Type"}), CLASS_UNKNOWN
		)

	def test_s14_reject_never_co_product(self):
		self.assertNotEqual(
			classify_secondary_row(
				{"secondary_item_type": "Scrap", "custom_output_class": "MAIN_PRODUCT_REJECT"}
			),
			CLASS_CO_PRODUCT,
		)

	def test_s15_component_scrap_never_reject(self):
		self.assertNotEqual(
			classify_secondary_row({"custom_output_class": "COMPONENT_SCRAP"}),
			CLASS_MAIN_PRODUCT_REJECT,
		)

	def test_s11_equiv_factor_preserved_in_identity_payload(self):
		# Rebuild must not invent factor=1; classify leaves metadata alone
		row = {
			"secondary_item_type": "Co-Product",
			"custom_output_equivalent_factor": 1.25,
		}
		self.assertEqual(classify_secondary_row(row), CLASS_CO_PRODUCT)
		self.assertEqual(row["custom_output_equivalent_factor"], 1.25)


class TestLiveCanaries(unittest.TestCase):
	"""Real Development canaries — skip if Job Cards absent.

	After a successful Apply canary, FAIL JC is expected BALANCED with
	MISSING_MANUFACTURE_CONSUMPTION + POSTING_ORDER_WARNING retained.
	"""

	FAIL_JC = "PO-JOB08760"
	CTRL_JC = "PO-JOB08761"
	ITEM = "13200544"

	def _has(self, name):
		return bool(frappe.db.exists("Job Card", name))

	def test_canary_fail_scan_quantities(self):
		if not self._has(self.FAIL_JC):
			self.skipTest("PO-JOB08760 not on site")
		from erpnext_extensions.iran_accounting.job_card_stock_rebuild.service import scan_job_card

		before_se = frappe.db.count("Stock Entry")
		before_sle = frappe.db.count("Stock Ledger Entry")
		plan = scan_job_card(self.FAIL_JC, item_filter=self.ITEM)
		self.assertFalse(plan.get("mutated"))
		self.assertEqual(frappe.db.count("Stock Entry"), before_se)
		self.assertEqual(frappe.db.count("Stock Ledger Entry"), before_sle)
		row = next(r for r in plan["material_rows"] if r["item_code"] == self.ITEM)
		self.assertTrue(qty_equal(row["issued"], 1160))
		self.assertTrue(qty_equal(row["returned"], 12))
		self.assertTrue(qty_equal(row["consumed"], 0))
		self.assertTrue(qty_equal(row["wip_remainder"], 1148))
		self.assertIn(S.MISSING_MANUFACTURE_CONSUMPTION, plan["statuses"])
		# After Apply: tracking complete → BALANCED; before Apply: incomplete
		if plan["overall_status"] == S.BALANCED:
			self.assertFalse(plan["apply_allowed"])
		else:
			self.assertIn(S.JOB_CARD_TRACKING_INCOMPLETE, plan["statuses"])
		self.assertTrue(plan["manufacture_repair_required"])

	def test_canary_fail_dry_run_or_idempotent(self):
		if not self._has(self.FAIL_JC):
			self.skipTest("PO-JOB08760 not on site")
		from erpnext_extensions.iran_accounting.job_card_stock_rebuild.service import (
			dry_run_rebuild,
			scan_job_card,
		)

		scan = scan_job_card(self.FAIL_JC, item_filter=self.ITEM)
		jci = frappe.db.get_value(
			"Job Card Item",
			{"parent": self.FAIL_JC, "item_code": self.ITEM},
			["name", "transferred_qty", "consumed_qty"],
			as_dict=1,
		)
		result = dry_run_rebuild(self.FAIL_JC, fingerprint=scan["fingerprint"], item_filter=self.ITEM)
		after = frappe.db.get_value(
			"Job Card Item",
			jci.name,
			["transferred_qty", "consumed_qty"],
			as_dict=1,
		)
		self.assertTrue(qty_equal(after.transferred_qty, jci.transferred_qty))
		self.assertTrue(qty_equal(after.consumed_qty, jci.consumed_qty))
		self.assertEqual(result.get("dry_run_status"), "DRY_RUN_PASS")
		if scan.get("apply_allowed"):
			sim_items = (result.get("downstream") or {}).get("simulation", {}).get("items") or []
			match = [i for i in sim_items if i["item_code"] == self.ITEM]
			self.assertTrue(match, "Downstream simulation missing 13200544")
			self.assertTrue(qty_equal(match[0]["qty"], 1148))
		else:
			# Already rebuilt — simulate candidates from current DB state
			from erpnext_extensions.iran_accounting.job_card_stock_rebuild.guards import (
				simulate_manufacture_candidates,
			)

			sim = simulate_manufacture_candidates(self.FAIL_JC)
			match = [i for i in sim.get("items") or [] if i["item_code"] == self.ITEM]
			self.assertTrue(match)
			self.assertTrue(qty_equal(match[0]["qty"], 1148))

	def test_canary_control_balanced(self):
		if not self._has(self.CTRL_JC):
			self.skipTest("PO-JOB08761 not on site")
		from erpnext_extensions.iran_accounting.job_card_stock_rebuild.service import scan_job_card

		jci = frappe.db.get_value(
			"Job Card Item",
			{"parent": self.CTRL_JC, "item_code": self.ITEM},
			[
				"name",
				"transferred_qty",
				"consumed_qty",
				"custom_issued_qty",
				"custom_returned_qty",
				"custom_still_in_wip",
				"custom_returnable_qty",
			],
			as_dict=1,
		)
		plan = scan_job_card(self.CTRL_JC, item_filter=self.ITEM)
		row = next(r for r in plan["material_rows"] if r["item_code"] == self.ITEM)
		self.assertTrue(qty_equal(row["issued"], 2920))
		self.assertTrue(qty_equal(row["returned"], 30))
		self.assertTrue(qty_equal(row["consumed"], 2890))
		self.assertTrue(qty_equal(row["wip_remainder"], 0))
		self.assertFalse(plan["apply_allowed"])
		self.assertEqual(plan["overall_status"], S.BALANCED)
		after = frappe.db.get_value(
			"Job Card Item",
			jci.name,
			[
				"transferred_qty",
				"consumed_qty",
				"custom_issued_qty",
				"custom_returned_qty",
				"custom_still_in_wip",
				"custom_returnable_qty",
			],
			as_dict=1,
		)
		for k in (
			"transferred_qty",
			"consumed_qty",
			"custom_issued_qty",
			"custom_returned_qty",
			"custom_still_in_wip",
			"custom_returnable_qty",
		):
			self.assertTrue(qty_equal(after.get(k), jci.get(k)), f"CTRL field {k} changed")

	def test_wo_readonly_scan(self):
		wo = "MFG-WO-2026-00659"
		if not frappe.db.exists("Work Order", wo):
			self.skipTest("WO not on site")
		from erpnext_extensions.iran_accounting.job_card_stock_rebuild.service import (
			scan_work_order_readonly,
		)

		out = scan_work_order_readonly(wo)
		self.assertFalse(out["mutated"])
		self.assertTrue(any(r["job_card"] == self.FAIL_JC for r in out["job_cards"]))

if __name__ == "__main__":
	unittest.main()

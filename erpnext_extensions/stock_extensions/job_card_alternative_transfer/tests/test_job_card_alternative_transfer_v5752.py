# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.7.52 Job Card original + substitute material fulfillment.

Run:
  bench --site development.localhost run-tests \\
    --app erpnext_extensions \\
    --module erpnext_extensions.stock_extensions.job_card_alternative_transfer.tests.\\
    test_job_card_alternative_transfer_v5752

Or:
  bench --site development.localhost execute \\
    erpnext_extensions.stock_extensions.job_card_alternative_transfer.tests.\\
    test_job_card_alternative_transfer_v5752.run_all
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.integration.bootstrap import apply as apply_iran
from erpnext_extensions.stock_extensions.job_card_alternative_transfer.attribution import (
	aggregate_transfer_rows,
	allocate_effective_qty_to_demand_rows,
	get_attributed_transferred_qty_by_item,
	get_material_transfer_status,
	get_shortfall_rows,
	materials_fully_transferred,
)
from erpnext_extensions.stock_extensions.job_card_alternative_transfer.patch import apply_patch

CANARY_JC = "PO-JOB11513"
CANARY_ORIG = "18200041"
CANARY_SUB = "18200058"
CANARY_ORIG_REQ = 200.04
CANARY_SUB_XFER = 200.06


def _row(**kwargs):
	base = {
		"item_code": "ORIG",
		"original_item": None,
		"transfer_qty": 0,
		"is_return": 0,
	}
	base.update(kwargs)
	return base


def _demand(idx, item_code, required_qty, name=None, **kwargs):
	data = {
		"idx": idx,
		"name": name or f"row-{idx}",
		"item_code": item_code,
		"required_qty": required_qty,
		"transferred_qty": 0,
		"source_warehouse": "WH-SRC",
		"stock_uom": "Nos",
		"uom": "Nos",
	}
	data.update(kwargs)
	return frappe._dict(data)


def _jc(name="JC-TEST", items=None):
	return frappe._dict(name=name, items=items or [])


class TestAggregateTransferRows(unittest.TestCase):
	def test_original_only(self):
		attr = aggregate_transfer_rows([_row(item_code="ORIG", transfer_qty=100)])
		self.assertAlmostEqual(attr["ORIG"], 100)

	def test_substitute_only(self):
		attr = aggregate_transfer_rows(
			[_row(item_code="SUB-A", original_item="ORIG", transfer_qty=100)]
		)
		self.assertAlmostEqual(attr["ORIG"], 100)
		self.assertNotIn("SUB-A", attr)

	def test_mixed_transfer(self):
		attr = aggregate_transfer_rows(
			[
				_row(item_code="ORIG", transfer_qty=40),
				_row(item_code="SUB-A", original_item="ORIG", transfer_qty=60),
			]
		)
		self.assertAlmostEqual(attr["ORIG"], 100)

	def test_mixed_partial(self):
		attr = aggregate_transfer_rows(
			[
				_row(item_code="ORIG", transfer_qty=30),
				_row(item_code="SUB-A", original_item="ORIG", transfer_qty=40),
			]
		)
		self.assertAlmostEqual(attr["ORIG"], 70)

	def test_multiple_substitutes(self):
		attr = aggregate_transfer_rows(
			[
				_row(item_code="ORIG", transfer_qty=20),
				_row(item_code="SUB-A", original_item="ORIG", transfer_qty=30),
				_row(item_code="SUB-B", original_item="ORIG", transfer_qty=50),
			]
		)
		self.assertAlmostEqual(attr["ORIG"], 100)

	def test_original_over_transfer(self):
		attr = aggregate_transfer_rows([_row(item_code="ORIG", transfer_qty=110)])
		self.assertAlmostEqual(attr["ORIG"], 110)

	def test_mixed_over_transfer(self):
		attr = aggregate_transfer_rows(
			[
				_row(item_code="ORIG", transfer_qty=60),
				_row(item_code="SUB-A", original_item="ORIG", transfer_qty=60),
			]
		)
		self.assertAlmostEqual(attr["ORIG"], 120)

	def test_no_transfer(self):
		attr = aggregate_transfer_rows([])
		self.assertEqual(attr, {})

	def test_cancelled_or_draft_not_in_input(self):
		# Query filters docstatus=1; aggregator must not invent credit from empty input.
		self.assertEqual(aggregate_transfer_rows([]), {})

	def test_return_nets_out(self):
		attr = aggregate_transfer_rows(
			[
				_row(item_code="SUB-A", original_item="ORIG", transfer_qty=100),
				_row(item_code="SUB-A", original_item="ORIG", transfer_qty=25, is_return=1),
			]
		)
		self.assertAlmostEqual(attr["ORIG"], 75)

	def test_over_return_clamped(self):
		attr = aggregate_transfer_rows(
			[
				_row(item_code="ORIG", transfer_qty=10),
				_row(item_code="ORIG", transfer_qty=40, is_return=1),
			]
		)
		self.assertAlmostEqual(attr["ORIG"], 0)

	def test_unlinked_substitute_does_not_credit_original(self):
		"""Invalid / unregistered substitute: no original_item → credits its own code only."""
		attr = aggregate_transfer_rows([_row(item_code="SUB-A", transfer_qty=100)])
		self.assertAlmostEqual(attr["SUB-A"], 100)
		self.assertNotIn("ORIG", attr)

	def test_uom_uses_transfer_qty_stock_uom(self):
		# transfer_qty is already stock-UOM; qty in other UOM must not be used.
		attr = aggregate_transfer_rows(
			[_row(item_code="ORIG", transfer_qty=12.5, qty=100)]  # stray qty ignored
		)
		self.assertAlmostEqual(attr["ORIG"], 12.5)

	def test_duplicate_line_summed_once_each(self):
		attr = aggregate_transfer_rows(
			[
				_row(item_code="SUB-A", original_item="ORIG", transfer_qty=40),
				_row(item_code="SUB-A", original_item="ORIG", transfer_qty=60),
			]
		)
		self.assertAlmostEqual(attr["ORIG"], 100)


class TestAllocateDemandRows(unittest.TestCase):
	def _shortfall(self, required, attributed):
		rows = allocate_effective_qty_to_demand_rows(
			[_demand(1, "ORIG", required)],
			attributed,
		)
		return rows[0]

	def test_scenario_table(self):
		cases = [
			("original_only", 100, {"ORIG": 100}, 0),
			("substitute_only", 100, {"ORIG": 100}, 0),
			("mixed", 100, {"ORIG": 100}, 0),
			("mixed_partial", 100, {"ORIG": 70}, 30),
			("multi_sub", 100, {"ORIG": 100}, 0),
			("over_original", 100, {"ORIG": 110}, 0),
			("over_mixed", 100, {"ORIG": 120}, 0),
			("none", 100, {}, 100),
		]
		for name, required, attributed, expected_short in cases:
			with self.subTest(name):
				row = self._shortfall(required, attributed)
				self.assertAlmostEqual(row.shortfall_qty, expected_short, msg=name)
				self.assertTrue(
					(row.effective_transferred_qty >= required) == (expected_short == 0),
					msg=name,
				)

	def test_fifo_sibling_demand_no_double_count(self):
		rows = allocate_effective_qty_to_demand_rows(
			[_demand(1, "ORIG", 40, name="a"), _demand(2, "ORIG", 60, name="b")],
			{"ORIG": 70},
		)
		by_name = {r.name: r for r in rows}
		self.assertAlmostEqual(by_name["a"].shortfall_qty, 0)
		self.assertAlmostEqual(by_name["b"].shortfall_qty, 30)

	def test_supply_row_required_zero_ignored(self):
		rows = allocate_effective_qty_to_demand_rows(
			[
				_demand(1, "ORIG", 100, name="demand"),
				_demand(2, "SUB-A", 0, name="supply", transferred_qty=100),
			],
			{"ORIG": 100},
		)
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0].name, "demand")
		self.assertAlmostEqual(rows[0].shortfall_qty, 0)

	def test_excess_does_not_offset_other_item(self):
		rows = allocate_effective_qty_to_demand_rows(
			[_demand(1, "ORIG", 50), _demand(2, "OTHER", 50)],
			{"ORIG": 80},
		)
		by_item = {r.item_code: r for r in rows}
		self.assertAlmostEqual(by_item["ORIG"].shortfall_qty, 0)
		self.assertAlmostEqual(by_item["OTHER"].shortfall_qty, 50)


class TestPatchedJobCardMethods(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		apply_iran()
		apply_patch()

	def test_get_rows_to_move_credits_substitute(self):
		from erpnext.manufacturing.doctype.job_card.job_card import JobCard

		jc = JobCard(
			{
				"doctype": "Job Card",
				"name": "JC-MOCK",
				"items": [
					{
						"name": "d1",
						"idx": 1,
						"item_code": "ORIG",
						"required_qty": 100,
						"transferred_qty": 0,
						"source_warehouse": "WH",
					},
					{
						"name": "s1",
						"idx": 2,
						"item_code": "SUB-A",
						"required_qty": 0,
						"transferred_qty": 100,
						"source_warehouse": "WH",
					},
				],
			}
		)
		with patch(
			"erpnext_extensions.stock_extensions.job_card_alternative_transfer.patch.get_shortfall_rows",
			return_value=[],
		):
			self.assertEqual(jc.get_rows_to_move(), [])

		short = [
			frappe._dict(name="d1", shortfall_qty=40, item_code="ORIG", required_qty=100, effective_transferred_qty=60)
		]
		with patch(
			"erpnext_extensions.stock_extensions.job_card_alternative_transfer.patch.get_shortfall_rows",
			return_value=short,
		):
			moved = jc.get_rows_to_move()
			self.assertEqual([r.name for r in moved], ["d1"])

	def test_has_short_uses_attribution(self):
		from erpnext.manufacturing.doctype.job_card.job_card import JobCard

		jc = JobCard(
			{
				"doctype": "Job Card",
				"name": "JC-MOCK-2",
				"work_order": "WO-X",
				"for_quantity": 10,
				"operation": "OP",
				"operation_row_id": 1,
				"items": [],
			}
		)
		jc.get_work_order_qty_details = lambda: frappe._dict(operation_qty=10)
		jc.is_own_material = lambda row: True

		with (
			patch("frappe.get_all", return_value=[frappe._dict(item_code="ORIG", required_qty=100, operation="OP", operation_row_id=1)]),
			patch(
				"erpnext_extensions.stock_extensions.job_card_alternative_transfer.patch.get_attributed_transferred_qty_by_item",
				return_value={"ORIG": 100},
			),
		):
			self.assertFalse(jc.has_short_transferred_material())

		with (
			patch("frappe.get_all", return_value=[frappe._dict(item_code="ORIG", required_qty=100, operation="OP", operation_row_id=1)]),
			patch(
				"erpnext_extensions.stock_extensions.job_card_alternative_transfer.patch.get_attributed_transferred_qty_by_item",
				return_value={"ORIG": 40},
			),
		):
			self.assertTrue(jc.has_short_transferred_material())

	def test_patch_idempotent(self):
		from erpnext.manufacturing.doctype.job_card.job_card import JobCard

		apply_patch()
		apply_patch()
		self.assertTrue(getattr(JobCard, "_ee_jc_alt_transfer_patched", False))


class TestCrossJobCardIsolation(unittest.TestCase):
	def test_attribution_scoped_by_job_card_name(self):
		# Ensure SQL filter uses the given job card — mock db.sql.
		captured = {}

		def fake_sql(query, values=None, as_dict=False):
			captured["values"] = values
			return [
				{
					"item_code": "SUB-A",
					"original_item": "ORIG",
					"transfer_qty": 50,
					"is_return": 0,
				}
			]

		with patch("frappe.db.sql", side_effect=fake_sql):
			attr = get_attributed_transferred_qty_by_item("JC-A")
		self.assertEqual(captured["values"][0], "JC-A")
		self.assertAlmostEqual(attr["ORIG"], 50)


class TestCanaryPOJOB11513(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		if not frappe.db.exists("Job Card", CANARY_JC):
			raise unittest.SkipTest(f"{CANARY_JC} not on site")
		apply_iran()
		apply_patch()
		frappe.set_user("Administrator")

	def test_rows_intact(self):
		jc = frappe.get_doc("Job Card", CANARY_JC)
		row1 = next(r for r in jc.items if r.idx == 1)
		row10 = next(r for r in jc.items if r.idx == 10)
		self.assertEqual(row1.item_code, CANARY_ORIG)
		self.assertAlmostEqual(flt(row1.required_qty), CANARY_ORIG_REQ)
		self.assertEqual(row10.item_code, CANARY_SUB)
		self.assertAlmostEqual(flt(row10.required_qty), 0)
		self.assertAlmostEqual(flt(row10.transferred_qty), CANARY_SUB_XFER)

	def test_attribution_credits_original(self):
		attr = get_attributed_transferred_qty_by_item(CANARY_JC)
		self.assertAlmostEqual(flt(attr.get(CANARY_ORIG)), CANARY_SUB_XFER)
		self.assertNotIn(CANARY_SUB, attr)

	def test_materials_fully_transferred(self):
		jc = frappe.get_doc("Job Card", CANARY_JC)
		self.assertTrue(materials_fully_transferred(jc))
		self.assertEqual(get_shortfall_rows(jc), [])
		self.assertEqual(jc.get_rows_to_move(), [])
		status = get_material_transfer_status(jc)
		self.assertTrue(status["fully_transferred"])

	def test_has_short_false_when_substitute_covers(self):
		jc = frappe.get_doc("Job Card", CANARY_JC)
		# Header transfer already complete; shortfall path must not flag original.
		self.assertFalse(jc.has_short_transferred_material())

	def test_get_material_shortages_whitelisted_and_empty(self):
		"""Move Materials RPC: patched method must be in frappe.whitelisted."""
		from erpnext.manufacturing.doctype.job_card.job_card import JobCard
		from erpnext_extensions.stock_extensions.job_card_alternative_transfer import patch as alt_patch

		apply_patch()
		fn = JobCard.get_material_shortages
		self.assertIn(fn, frappe.whitelisted)
		self.assertIs(fn, alt_patch.get_material_shortages)

		jc = frappe.get_doc("Job Card", CANARY_JC)
		shortages = jc.get_material_shortages()
		self.assertEqual(shortages, [])

	def test_module_shortage_api_authorized(self):
		from erpnext_extensions.stock_extensions.job_card_alternative_transfer.api import (
			get_material_shortages_for_job_card,
		)

		shortages = get_material_shortages_for_job_card(CANARY_JC)
		self.assertEqual(shortages, [])


class TestShortageApiSecurity(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		apply_iran()
		apply_patch()

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.local.flags = getattr(frappe.local, "flags", None) or frappe._dict()

	def test_missing_job_card_rejected(self):
		from erpnext_extensions.stock_extensions.job_card_alternative_transfer.api import (
			get_material_shortages_for_job_card,
		)

		with self.assertRaises(frappe.ValidationError):
			get_material_shortages_for_job_card("")
		with self.assertRaises(frappe.ValidationError):
			get_material_shortages_for_job_card(None)

	def test_invalid_job_card_rejected(self):
		from erpnext_extensions.stock_extensions.job_card_alternative_transfer.api import (
			get_material_shortages_for_job_card,
		)

		with self.assertRaises(frappe.DoesNotExistError):
			get_material_shortages_for_job_card("PO-JOB-DOES-NOT-EXIST-5752")

	def test_unauthorized_user_denied(self):
		from erpnext_extensions.stock_extensions.job_card_alternative_transfer.api import (
			get_material_shortages_for_job_card,
		)

		if not frappe.db.exists("Job Card", CANARY_JC):
			raise unittest.SkipTest(f"{CANARY_JC} not on site")

		# Guest has no Job Card read permission.
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			get_material_shortages_for_job_card(CANARY_JC)

	def test_read_only_does_not_mutate(self):
		from erpnext_extensions.stock_extensions.job_card_alternative_transfer.api import (
			get_material_shortages_for_job_card,
		)

		if not frappe.db.exists("Job Card", CANARY_JC):
			raise unittest.SkipTest(f"{CANARY_JC} not on site")

		frappe.set_user("Administrator")
		before = frappe.db.get_value(
			"Job Card Item",
			{"parent": CANARY_JC, "idx": 1},
			["item_code", "required_qty", "transferred_qty", "modified"],
			as_dict=True,
		)
		get_material_shortages_for_job_card(CANARY_JC)
		after = frappe.db.get_value(
			"Job Card Item",
			{"parent": CANARY_JC, "idx": 1},
			["item_code", "required_qty", "transferred_qty", "modified"],
			as_dict=True,
		)
		self.assertEqual(before, after)

	def test_cross_job_card_isolation_in_api(self):
		from erpnext_extensions.stock_extensions.job_card_alternative_transfer.api import (
			get_material_transfer_status_for_job_card,
		)

		if not frappe.db.exists("Job Card", CANARY_JC):
			raise unittest.SkipTest(f"{CANARY_JC} not on site")

		status = get_material_transfer_status_for_job_card(CANARY_JC)
		# Attribution keys come only from this JC's Stock Entries.
		self.assertIn(CANARY_ORIG, status["attributed_by_item"])
		self.assertAlmostEqual(flt(status["attributed_by_item"][CANARY_ORIG]), CANARY_SUB_XFER)

	def test_is_whitelisted_accepts_patched_method(self):
		from erpnext.manufacturing.doctype.job_card.job_card import JobCard

		apply_patch()
		# Must not raise PermissionError / Method Not Allowed.
		frappe.is_whitelisted(JobCard.get_material_shortages)


def run_all():
	suite = unittest.TestLoader().loadTestsFromModule(
		frappe.get_module(
			"erpnext_extensions.stock_extensions.job_card_alternative_transfer.tests."
			"test_job_card_alternative_transfer_v5752"
		)
	)
	result = unittest.TextTestRunner(verbosity=2).run(suite)
	return {
		"tests": result.testsRun,
		"failures": len(result.failures),
		"errors": len(result.errors),
		"skipped": len(result.skipped),
		"ok": result.wasSuccessful(),
	}

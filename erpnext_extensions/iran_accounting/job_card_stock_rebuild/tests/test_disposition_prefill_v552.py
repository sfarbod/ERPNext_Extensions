# Copyright (c) 2026, ERPNext Extensions contributors
"""PF01–PF13 — Scan suggestion → editable disposition prefill (v5.5.2)."""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt

from erpnext_extensions.iran_accounting.job_card_stock_rebuild.disposition_prefill import (
	DecisionState,
	prefill_from_suggestion,
)
from erpnext_extensions.iran_accounting.job_card_stock_rebuild.golden_rule import (
	scan_golden_rule,
	validate_dispositions,
)
from erpnext_extensions.iran_accounting.scrap_costing import MANUFACTURE_COSTING_CONTRACT_VERSION


class TestDispositionPrefillV552(FrappeTestCase):
	def test_pf01_consumed_prefills_consumed(self):
		d = prefill_from_suggestion(
			{"suggested_action": "CONSUMED", "suggested_qty": 1148, "confidence": "HIGH"}
		)
		self.assertEqual(flt(d["consumed"]), 1148)
		self.assertEqual(flt(d["scrap"]), 0)
		self.assertEqual(flt(d["return"]), 0)
		self.assertEqual(flt(d["still"]), 0)

	def test_pf02_component_scrap_prefills_scrap(self):
		d = prefill_from_suggestion(
			{"suggested_action": "COMPONENT_SCRAP", "suggested_qty": 5, "confidence": "HIGH"}
		)
		self.assertEqual(flt(d["scrap"]), 5)
		self.assertEqual(flt(d["consumed"]), 0)

		d2 = prefill_from_suggestion({"suggested_action": "SCRAP", "suggested_qty": 5})
		self.assertEqual(flt(d2["scrap"]), 5)

	def test_pf03_return_prefills_return(self):
		d = prefill_from_suggestion({"suggested_action": "RETURN", "suggested_qty": 20})
		self.assertEqual(flt(d["return"]), 20)
		self.assertEqual(flt(d["consumed"]), 0)

	def test_pf04_still_in_wip_prefills_still(self):
		d = prefill_from_suggestion({"suggested_action": "STILL IN WIP", "suggested_qty": 48})
		self.assertEqual(flt(d["still"]), 48)
		d2 = prefill_from_suggestion({"suggested_action": "STILL_IN_WIP", "suggested_qty": 48})
		self.assertEqual(flt(d2["still"]), 48)

	def test_pf05_low_confidence_still_prefills(self):
		d = prefill_from_suggestion(
			{
				"suggested_action": "CONSUMED",
				"suggested_qty": 2522,
				"confidence": "LOW",
			}
		)
		self.assertEqual(flt(d["consumed"]), 2522)
		self.assertEqual(flt(d["scrap"]), 0)

	def test_pf06_no_defensible_suggestion_leaves_zero(self):
		for action in ("MANUAL REVIEW", "AMBIGUOUS", "BLOCKED", "", None):
			d = prefill_from_suggestion(
				{"suggested_action": action, "suggested_qty": 2522, "confidence": "LOW"}
			)
			self.assertEqual(flt(d["consumed"]), 0, action)
			self.assertEqual(flt(d["scrap"]), 0, action)
			self.assertEqual(flt(d["return"]), 0, action)
			self.assertEqual(flt(d["still"]), 0, action)

	def test_pf07_user_can_edit_prefilled_value(self):
		st = DecisionState()
		st.init_from_scan(
			[{"item_code": "13200544", "batch_no": "", "suggested_action": "CONSUMED", "suggested_qty": 1148}]
		)
		self.assertEqual(flt(st.get("13200544")["consumed"]), 1148)
		st.set_user_edit("13200544", consumed=1100)
		self.assertEqual(flt(st.get("13200544")["consumed"]), 1100)

	def test_pf08_user_can_split_disposition(self):
		st = DecisionState()
		st.init_from_scan(
			[{"item_code": "13200544", "batch_no": "", "suggested_action": "CONSUMED", "suggested_qty": 1148}]
		)
		st.set_user_edit("13200544", consumed=1100, still=48)
		d = st.get("13200544")
		self.assertEqual(flt(d["consumed"]), 1100)
		self.assertEqual(flt(d["still"]), 48)
		self.assertEqual(flt(d["consumed"]) + flt(d["still"]), 1148)

	def test_pf09_invalid_manual_total_rejected(self):
		rows = [
			{
				"item_code": "13200544",
				"batch_no": "",
				"remaining_wip": 1148,
				"issued": 1160,
				"returned": 12,
				"consumed": 0,
				"scrap": 0,
			}
		]
		ok, errors, _ = validate_dispositions(
			rows,
			[
				{
					"item_code": "13200544",
					"batch_no": "",
					"proposed_consumed": 1000,
					"proposed_scrap": 0,
					"proposed_return": 0,
					"proposed_still_in_wip": 0,
				}
			],
		)
		self.assertFalse(ok)
		self.assertTrue(any("!=" in e or "remaining" in e for e in errors))

	def test_pf10_user_edits_survive_rerender(self):
		st = DecisionState()
		row = {
			"item_code": "13200544",
			"batch_no": "",
			"suggested_action": "CONSUMED",
			"suggested_qty": 1148,
		}
		st.init_from_scan([row])
		st.set_user_edit("13200544", consumed=1100, still=48)
		# Simulate unrelated UI re-render using the same scan row (suggestion unchanged).
		rendered = st.values_for_render(row)
		self.assertEqual(flt(rendered["consumed"]), 1100)
		self.assertEqual(flt(rendered["still"]), 48)

	def test_pf11_explicit_new_scan_refreshes_suggestions(self):
		st = DecisionState()
		st.init_from_scan(
			[{"item_code": "13200544", "batch_no": "", "suggested_action": "CONSUMED", "suggested_qty": 1148}]
		)
		st.set_user_edit("13200544", consumed=1100, still=48)
		# Explicit new Scan with updated backend suggestion.
		st.init_from_scan(
			[{"item_code": "13200544", "batch_no": "", "suggested_action": "RETURN", "suggested_qty": 20}]
		)
		d = st.get("13200544")
		self.assertEqual(flt(d["return"]), 20)
		self.assertEqual(flt(d["consumed"]), 0)
		self.assertEqual(flt(d["still"]), 0)

	def test_pf12_dry_run_payload_uses_final_user_values(self):
		st = DecisionState()
		st.init_from_scan(
			[{"item_code": "13200544", "batch_no": "", "suggested_action": "CONSUMED", "suggested_qty": 1148}]
		)
		st.set_user_edit("13200544", consumed=1100, still=48)
		payload = st.dry_run_payload_row("13200544")
		self.assertEqual(flt(payload["proposed_consumed"]), 1100)
		self.assertEqual(flt(payload["proposed_still_in_wip"]), 48)
		self.assertNotEqual(flt(payload["proposed_consumed"]), 1148)

	def test_pf13_accepting_suggestion_unchanged_matches_backend_proposed(self):
		"""No accounting policy change — accepted suggestion == scan proposed_*."""
		self.assertEqual(MANUFACTURE_COSTING_CONTRACT_VERSION, "5.3.43")
		if not frappe.db.exists("Job Card", "PO-JOB08760"):
			self.skipTest("PO-JOB08760 not on site")
		scan = scan_golden_rule("PO-JOB08760")
		row = next(r for r in scan["rows"] if r["item_code"] == "13200544")
		self.assertEqual(row["suggested_action"], "CONSUMED")
		self.assertEqual(flt(row["suggested_qty"]), 1148)
		self.assertEqual(flt(row["proposed_consumed"]), 1148)
		d = prefill_from_suggestion(row)
		self.assertEqual(flt(d["consumed"]), 1148)
		ok, errors, norm = validate_dispositions(
			[row],
			[
				{
					"item_code": "13200544",
					"batch_no": row.get("batch_no") or "",
					"proposed_consumed": d["consumed"],
					"proposed_scrap": d["scrap"],
					"proposed_return": d["return"],
					"proposed_still_in_wip": d["still"],
				}
			],
		)
		self.assertTrue(ok, errors)
		self.assertEqual(flt(norm[0]["proposed_consumed"]), 1148)

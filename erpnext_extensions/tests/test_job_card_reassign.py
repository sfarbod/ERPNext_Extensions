# Copyright (c) 2026, ERPNext Extensions contributors
"""Unit tests for Job Card Work Order reassignment (no operator writes)."""

from __future__ import annotations

import unittest
from unittest import mock

from erpnext_extensions.stock_extensions.job_card_reassign.classify import (
	CLASS_BLOCK,
	CLASS_KEEP,
	CLASS_MOVE,
	classify_stock_entry,
)
from erpnext_extensions.stock_extensions.job_card_reassign.engine import fingerprint
from erpnext_extensions.stock_extensions.job_card_reassign.mapping import map_job_card_operation


def _se(**kw):
	row = {
		"name": "STE-1",
		"purpose": "Manufacture",
		"docstatus": 1,
		"work_order": "WO-A",
		"job_card": "JC-1",
		"is_return": 0,
		"job_card_item_parents": ["JC-1"],
		"pick_list": None,
		"source_stock_entry": None,
	}
	row.update(kw)
	return row


class TestClassify(unittest.TestCase):
	def test_class_a_exclusive_job_card(self):
		with mock.patch(
			"erpnext_extensions.stock_extensions.job_card_reassign.classify._has_landed_cost",
			return_value=False,
		), mock.patch(
			"erpnext_extensions.stock_extensions.job_card_reassign.classify._has_consignment_flag",
			return_value=False,
		):
			out = classify_stock_entry(_se(), {"JC-1"}, "WO-A")
		self.assertEqual(out["classification"], CLASS_MOVE)

	def test_class_a_return(self):
		with mock.patch(
			"erpnext_extensions.stock_extensions.job_card_reassign.classify._has_landed_cost",
			return_value=False,
		), mock.patch(
			"erpnext_extensions.stock_extensions.job_card_reassign.classify._has_consignment_flag",
			return_value=False,
		):
			out = classify_stock_entry(
				_se(purpose="Material Transfer for Manufacture", is_return=1),
				{"JC-1"},
				"WO-A",
			)
		self.assertEqual(out["classification"], CLASS_MOVE)

	def test_class_b_work_order_only(self):
		out = classify_stock_entry(
			_se(job_card="", job_card_item_parents=[], purpose="Material Transfer for Manufacture"),
			{"JC-1"},
			"WO-A",
		)
		self.assertEqual(out["classification"], CLASS_KEEP)

	def test_class_c_shared_items(self):
		out = classify_stock_entry(
			_se(job_card_item_parents=["JC-1", "JC-2"]),
			{"JC-1"},
			"WO-A",
		)
		self.assertEqual(out["classification"], CLASS_BLOCK)

	def test_class_c_job_card_item_without_parent_link(self):
		out = classify_stock_entry(
			_se(job_card="", job_card_item_parents=["JC-1"]),
			{"JC-1"},
			"WO-A",
		)
		self.assertEqual(out["classification"], CLASS_BLOCK)

	def test_unselected_job_card_kept(self):
		out = classify_stock_entry(_se(job_card="JC-9", job_card_item_parents=["JC-9"]), {"JC-1"}, "WO-A")
		self.assertEqual(out["classification"], CLASS_KEEP)

	def test_unexpected_purpose_blocked(self):
		out = classify_stock_entry(_se(purpose="Material Issue"), {"JC-1"}, "WO-A")
		self.assertEqual(out["classification"], CLASS_BLOCK)


class TestMapping(unittest.TestCase):
	def test_unique_by_finished_good_reuses(self):
		ops = [
			{"name": "op-a", "idx": 1, "operation": "Pack", "finished_good": "FG-1", "sequence_id": 1},
			{"name": "op-b", "idx": 2, "operation": "Pack", "finished_good": "FG-2", "sequence_id": 2},
		]
		mapped = map_job_card_operation(
			{"name": "JC-1", "operation": "Pack", "finished_good": "FG-2", "sequence_id": 2, "operation_id": "old"},
			ops,
		)
		self.assertTrue(mapped["ok"])
		self.assertEqual(mapped["action"], "REUSE")
		self.assertEqual(mapped["target_operation_id"], "op-b")

	def test_same_operation_name_different_fg_creates(self):
		"""Do not reuse solely because Operation master name matches."""
		ops = [
			{"name": "op-a", "idx": 1, "operation": "Mix", "finished_good": "A", "sequence_id": 1},
		]
		mapped = map_job_card_operation(
			{
				"name": "JC-1",
				"operation": "Mix",
				"finished_good": "B",
				"operation_id": "src-op",
				"workstation": "WS-1",
			},
			ops,
			{"src-op": {"name": "src-op", "operation": "Mix", "finished_good": "B", "workstation": "WS-1"}},
		)
		self.assertTrue(mapped["ok"])
		self.assertEqual(mapped["action"], "CREATE")
		self.assertEqual(mapped["create_definition"]["finished_good"], "B")
		self.assertTrue(str(mapped["target_operation_id"]).startswith("CREATE:"))

	def test_missing_operation_creates(self):
		mapped = map_job_card_operation(
			{
				"name": "JC-1",
				"operation": "Label",
				"finished_good": "X",
				"operation_id": "src-label",
			},
			[{"name": "op-a", "idx": 1, "operation": "Pack", "finished_good": "X", "sequence_id": 1}],
			{"src-label": {"name": "src-label", "operation": "Label", "finished_good": "X", "sequence_id": 3}},
		)
		self.assertTrue(mapped["ok"])
		self.assertEqual(mapped["action"], "CREATE")
		self.assertEqual(mapped["create_definition"]["operation"], "Label")

	def test_ambiguous_exact_match_creates(self):
		ops = [
			{"name": "op-a", "idx": 1, "operation": "Mix", "finished_good": "A", "sequence_id": 1},
			{"name": "op-b", "idx": 2, "operation": "Mix", "finished_good": "A", "sequence_id": 2},
		]
		mapped = map_job_card_operation(
			{"name": "JC-1", "operation": "Mix", "finished_good": "A", "operation_id": "src"},
			ops,
			{"src": {"operation": "Mix", "finished_good": "A"}},
		)
		self.assertTrue(mapped["ok"])
		self.assertEqual(mapped["action"], "CREATE")


class TestFingerprint(unittest.TestCase):
	def _payload(self, **kw):
		base = {
			"source_work_order": "WO-A",
			"target_work_order": "WO-B",
			"job_cards": [
				{
					"name": "JC-1",
					"modified": "2026-01-01",
					"work_order": "WO-A",
					"operation_id": "op1",
					"docstatus": 1,
					"status": "Completed",
				}
			],
			"stock_entries": [
				{
					"name": "STE-1",
					"modified": "2026-01-01",
					"work_order": "WO-A",
					"job_card": "JC-1",
					"docstatus": 1,
					"classification": "MOVE",
				}
			],
			"source": {"modified": "2026-01-01", "status": "In Process"},
			"target": {"modified": "2026-01-01", "status": "In Process"},
		}
		base.update(kw)
		return base

	def test_stable_when_unchanged(self):
		self.assertEqual(fingerprint(self._payload()), fingerprint(self._payload()))

	def test_changes_when_jc_modified(self):
		a = self._payload()
		b = self._payload()
		b["job_cards"][0]["modified"] = "2026-01-02"
		self.assertNotEqual(fingerprint(a), fingerprint(b))


class TestApiGuard(unittest.TestCase):
	def test_get_job_cards_requires_system_manager(self):
		from erpnext_extensions.stock_extensions.job_card_reassign import api

		with mock.patch.object(api.frappe, "only_for", side_effect=PermissionError("no")):
			with self.assertRaises(PermissionError):
				api.get_job_cards("WO-A")


class TestCompatibility(unittest.TestCase):
	def _wo(self, name, **kw):
		row = {
			"name": name,
			"company": "C1",
			"docstatus": 1,
			"status": "In Process",
			"production_item": "FG",
			"bom_no": "BOM-1",
			"qty": 100,
			"track_semi_finished_goods": 1,
			"transfer_material_against": "Job Card",
			"skip_transfer": 0,
			"project": None,
			"operations": [{"name": "op-t", "completed_qty": 0, "process_loss_qty": 0}],
		}
		row.update(kw)
		return row

	def test_same_work_order_is_blocked(self):
		from erpnext_extensions.stock_extensions.job_card_reassign.validate import evaluate_compatibility

		source = self._wo("WO-A")
		dummy = mock.Mock()
		dummy.db.get_single_value.return_value = 0
		dummy.db.exists.return_value = False
		dummy.get_all.return_value = []
		with mock.patch(
			"erpnext_extensions.stock_extensions.job_card_reassign.validate.collect_sfg_consumption_blockers",
			return_value=[],
		), mock.patch(
			"erpnext_extensions.stock_extensions.job_card_reassign.validate.frappe",
			dummy,
		):
			blockers, _warnings = evaluate_compatibility(
				source=source,
				target=source,
				job_cards=[
					{
						"name": "JC-1",
						"work_order": "WO-A",
						"docstatus": 1,
						"status": "Completed",
						"for_quantity": 10,
						"pending_qty": 0,
					}
				],
				selected=["JC-1"],
				classified={"block": [], "rows": []},
				mappings={"JC-1": {"ok": True, "target_operation_id": "op-t"}},
				reason="fix split",
			)
		self.assertTrue(any(b["code"] == "same_work_order" for b in blockers))

	def test_evaluate_compatibility_uses_class_move_constant(self):
		"""Regression: validate.py must import CLASS_MOVE (not raise NameError)."""
		from erpnext_extensions.stock_extensions.job_card_reassign.validate import evaluate_compatibility

		source = self._wo("WO-A")
		target = self._wo("WO-B")
		dummy = mock.Mock()
		dummy.db.get_single_value.return_value = 0
		dummy.db.exists.return_value = False
		dummy.get_all.return_value = []
		with mock.patch(
			"erpnext_extensions.stock_extensions.job_card_reassign.validate.collect_sfg_consumption_blockers",
			return_value=[],
		), mock.patch(
			"erpnext_extensions.stock_extensions.job_card_reassign.validate.frappe",
			dummy,
		):
			_blockers, warnings = evaluate_compatibility(
				source=source,
				target=target,
				job_cards=[
					{
						"name": "JC-1",
						"work_order": "WO-A",
						"docstatus": 1,
						"status": "Completed",
						"for_quantity": 10,
						"pending_qty": 0,
						"total_completed_qty": 10,
						"process_loss_qty": 0,
					}
				],
				selected=["JC-1"],
				classified={
					"block": [],
					"rows": [
						{
							"name": "STE-1",
							"classification": CLASS_MOVE,
							"docstatus": 1,
						}
					],
				},
				mappings={"JC-1": {"ok": True, "target_operation_id": "op-t"}},
				reason="fix split",
			)
		self.assertTrue(any(w["code"] == "submitted_stock_entries" for w in warnings))

	def test_capacity_is_not_a_blocker(self):
		from erpnext_extensions.stock_extensions.job_card_reassign.validate import evaluate_compatibility

		source = self._wo("WO-A")
		target = self._wo("WO-B", qty=10)
		dummy = mock.Mock()
		dummy.db.get_single_value.return_value = 0
		dummy.db.exists.return_value = False
		dummy.get_all.return_value = []
		with mock.patch(
			"erpnext_extensions.stock_extensions.job_card_reassign.validate.collect_sfg_consumption_blockers",
			return_value=[],
		), mock.patch(
			"erpnext_extensions.stock_extensions.job_card_reassign.validate.frappe",
			dummy,
		):
			blockers, warnings = evaluate_compatibility(
				source=source,
				target=target,
				job_cards=[
					{
						"name": "JC-1",
						"work_order": "WO-A",
						"docstatus": 1,
						"status": "Completed",
						"for_quantity": 50,
						"pending_qty": 0,
						"total_completed_qty": 50,
						"process_loss_qty": 0,
						"operation": "Pack",
						"finished_good": "FG",
					}
				],
				selected=["JC-1"],
				classified={"block": [], "rows": []},
				mappings={
					"JC-1": {
						"ok": True,
						"action": "REUSE",
						"target_operation_id": "op-t",
						"source_operation": "Pack",
						"source_finished_good": "FG",
					}
				},
				reason="fix split",
			)
		self.assertFalse(any(b["code"] in {"target_capacity", "target_completed_capacity"} for b in blockers))
		self.assertFalse(any(b["code"] == "operation_unmapped" for b in blockers))

	def test_preview_reassignment_does_not_raise_name_error(self):
		"""Regression: preview_reassignment must not crash on missing CLASS_MOVE."""
		from erpnext_extensions.stock_extensions.job_card_reassign import engine

		source = {
			"name": "WO-A",
			"company": "C1",
			"docstatus": 1,
			"status": "In Process",
			"production_item": "FG",
			"bom_no": "BOM-1",
			"qty": 100,
			"track_semi_finished_goods": 1,
			"transfer_material_against": "Job Card",
			"skip_transfer": 0,
			"project": None,
			"modified": "2026-01-01",
			"operations": [
				{
					"name": "op-t",
					"idx": 1,
					"operation": "Pack",
					"finished_good": "FG",
					"sequence_id": 1,
					"completed_qty": 0,
					"process_loss_qty": 0,
					"workstation": None,
					"workstation_type": None,
				}
			],
			"required_items": [],
			"produced_qty": 0,
			"process_loss_qty": 0,
			"material_transferred_for_manufacturing": 0,
		}
		target = dict(source, name="WO-B", modified="2026-01-02")
		jc = {
			"name": "JC-1",
			"work_order": "WO-A",
			"operation": "Pack",
			"operation_id": "op-s",
			"finished_good": "FG",
			"sequence_id": 1,
			"docstatus": 1,
			"status": "Completed",
			"for_quantity": 10,
			"pending_qty": 0,
			"total_completed_qty": 10,
			"manufactured_qty": 10,
			"transferred_qty": 10,
			"process_loss_qty": 0,
			"modified": "2026-01-01",
			"is_paused": 0,
			"is_corrective_job_card": 0,
			"is_subcontracted": 0,
			"track_semi_finished_goods": 1,
			"workstation": None,
			"workstation_type": None,
		}
		classified = {
			"rows": [
				{
					"name": "STE-1",
					"classification": CLASS_MOVE,
					"docstatus": 1,
					"modified": "2026-01-01",
					"work_order": "WO-A",
					"job_card": "JC-1",
					"purpose": "Manufacture",
					"process_loss_qty": 0,
				}
			],
			"move": ["STE-1"],
			"keep": [],
			"block": [],
		}

		with mock.patch.object(engine, "_require_system_manager"), mock.patch.object(
			engine, "load_work_order", side_effect=lambda name: source if name == "WO-A" else target
		), mock.patch.object(engine, "load_job_cards", return_value=[jc]), mock.patch(
			"erpnext_extensions.stock_extensions.job_card_reassign.engine.classify_scope",
			return_value=classified,
		), mock.patch.object(engine, "remaining_job_card_count", return_value=0), mock.patch.object(
			engine,
			"store_preview",
			side_effect=lambda preview: {**preview, "preview_token": "test-token"},
		), mock.patch(
			"erpnext_extensions.stock_extensions.job_card_reassign.engine.analyze_historical_qty",
			return_value={
				"planned_qty": 100,
				"planned_qty_unchanged": True,
				"qty_change_required": False,
				"historical_exceeds_planned_qty": False,
				"final_fg_for_quantity_sum": 10,
				"max_operation_completed_plus_loss": 10,
				"message": "Planned Qty remains unchanged: 100.",
			},
		), mock.patch(
			"erpnext_extensions.stock_extensions.job_card_reassign.validate.collect_sfg_consumption_blockers",
			return_value=[],
		), mock.patch(
			"erpnext_extensions.stock_extensions.job_card_reassign.validate.frappe.db.get_single_value",
			return_value=0,
		), mock.patch(
			"erpnext_extensions.stock_extensions.job_card_reassign.validate.frappe.db.exists",
			return_value=False,
		), mock.patch(
			"erpnext_extensions.stock_extensions.job_card_reassign.validate.frappe.get_all",
			return_value=[],
		):
			preview = engine.preview_reassignment("WO-A", "WO-B", ["JC-1"], "fix split")

		self.assertIn("can_execute", preview)
		self.assertIsInstance(preview.get("blockers"), list)
		self.assertTrue(preview.get("can_execute"))
		self.assertEqual(preview["operation_mapping"]["JC-1"]["action"], "REUSE")
		self.assertFalse(preview["historical_qty"].get("qty_change_required"))
		self.assertEqual(preview["counters_after_estimate"]["target"]["qty"], 100)
		self.assertTrue(any(w.get("code") == "submitted_stock_entries" for w in preview.get("warnings") or []))
		self.assertFalse(any(w.get("code") == "reconcile_target_qty" for w in preview.get("warnings") or []))


class TestPlannedQtyImmutable(unittest.TestCase):
	def test_analyze_historical_qty_never_proposes_update(self):
		from erpnext_extensions.stock_extensions.job_card_reassign.qty import analyze_historical_qty

		target = {
			"name": "WO-T",
			"qty": 6430,
			"production_item": "FG",
			"operations": [
				{"name": "op-1", "completed_qty": 1000, "process_loss_qty": 0},
			],
		}
		moved = [
			{
				"name": "JC-1",
				"docstatus": 1,
				"finished_good": "FG",
				"for_quantity": 10000,
				"manufactured_qty": 9000,
				"total_completed_qty": 9000,
				"process_loss_qty": 0,
			}
		]
		mappings = {
			"JC-1": {"ok": True, "action": "REUSE", "target_operation_id": "op-1"},
		}
		with mock.patch(
			"erpnext_extensions.stock_extensions.job_card_reassign.qty.frappe.get_all",
			return_value=[],
		):
			info = analyze_historical_qty(target=target, moved_job_cards=moved, mappings=mappings)
		self.assertEqual(info["planned_qty"], 6430)
		self.assertTrue(info["planned_qty_unchanged"])
		self.assertFalse(info["qty_change_required"])
		self.assertTrue(info["historical_exceeds_planned_qty"])
		self.assertNotIn("proposed_qty", info)

	def test_apply_reassignment_never_changes_work_order_qty(self):
		from erpnext_extensions.stock_extensions.job_card_reassign import engine

		preview = {
			"source_work_order": "WO-A",
			"target_work_order": "WO-B",
			"reason": "fix",
			"source": {"name": "WO-A", "qty": 6430, "bom_no": "BOM-1"},
			"target": {"name": "WO-B", "qty": 6430, "bom_no": "BOM-1", "track_semi_finished_goods": 1},
			"job_cards": [
				{
					"name": "JC-1",
					"work_order": "WO-A",
					"docstatus": 1,
					"for_quantity": 10000,
					"total_completed_qty": 9000,
					"manufactured_qty": 9000,
				}
			],
			"operation_mapping": {
				"JC-1": {
					"ok": True,
					"action": "REUSE",
					"target_operation_id": "op-t",
					"target_operation_row_id": 1,
					"target_sequence_id": 1,
					"target_operation": "Pack",
					"target_semi_fg_bom": "BOM-1",
				}
			},
			"move_stock_entries": ["STE-1"],
			"stock_entries": [
				{"name": "STE-1", "job_card": "JC-1", "classification": "MOVE", "docstatus": 1}
			],
			"counters_before": {
				"source": {"name": "WO-A", "qty": 6430},
				"target": {"name": "WO-B", "qty": 6430},
			},
			"historical_qty": {"planned_qty": 6430, "qty_change_required": False},
			"preview_token": "tok",
		}
		qty_values = {"WO-A": 6430.0, "WO-B": 6430.0}

		def fake_get_value(doctype, name, field=None, **kwargs):
			if doctype == "Work Order" and field == "qty":
				return qty_values[name]
			if doctype == "Work Order" and isinstance(field, (list, tuple)):
				return None
			return None

		def fake_set_value(doctype, name, field, value=None, **kwargs):
			if doctype == "Work Order":
				if isinstance(field, dict) and "qty" in field:
					qty_values[name] = field["qty"]
				elif field == "qty":
					qty_values[name] = value

		source_after = {"name": "WO-A", "qty": 6430, "operations": [], "required_items": []}
		target_after = {"name": "WO-B", "qty": 6430, "operations": [], "required_items": []}

		with mock.patch.object(engine, "create_target_operations", return_value={}), mock.patch.object(
			engine, "resolve_create_mappings", side_effect=lambda m, c: m
		), mock.patch.object(engine, "_transfer_additional_items"), mock.patch.object(
			engine, "_apply_job_card"
		), mock.patch.object(engine, "_apply_stock_entry"), mock.patch.object(
			engine, "_move_material_requests", return_value=[]
		), mock.patch.object(engine, "refresh_job_card"), mock.patch.object(
			engine, "recalculate_work_order"
		), mock.patch.object(
			engine, "load_work_order", side_effect=lambda n: source_after if n == "WO-A" else target_after
		), mock.patch.object(engine, "remaining_job_card_count", return_value=0), mock.patch.object(
			engine, "write_audit", return_value="LOG-1"
		), mock.patch.object(engine, "add_comments"), mock.patch.object(
			engine.frappe.db, "get_value", side_effect=fake_get_value
		), mock.patch.object(engine.frappe.db, "set_value", side_effect=fake_set_value):
			result = engine.apply_reassignment(preview)

		self.assertEqual(qty_values["WO-A"], 6430.0)
		self.assertEqual(qty_values["WO-B"], 6430.0)
		self.assertEqual(result["source_qty"], 6430.0)
		self.assertEqual(result["target_qty"], 6430.0)

	def test_operation_status_allows_excess_without_mutating_qty(self):
		from erpnext_extensions.stock_extensions.job_card_reassign.recalc import (
			update_operation_status_allow_historical_excess,
		)

		class Op:
			def __init__(self, completed, loss=0):
				self.completed_qty = completed
				self.process_loss_qty = loss
				self.status = "Pending"

			def precision(self, _field):
				return 3

		class WO:
			def __init__(self):
				self.qty = 6430
				self.operations = [Op(9000), Op(1000, 500)]

			def get(self, key):
				return self.operations if key == "operations" else None

		wo = WO()
		update_operation_status_allow_historical_excess(wo)
		self.assertEqual(wo.qty, 6430)
		self.assertEqual(wo.operations[0].status, "Completed")
		self.assertEqual(wo.operations[1].status, "Work in Progress")

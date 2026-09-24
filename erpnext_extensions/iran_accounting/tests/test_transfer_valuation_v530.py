# Copyright (c) 2026, ERPNext Extensions contributors
"""Phase 5B — transfer valuation reconstruction unit tests."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from erpnext_extensions.iran_accounting.historical_stock.transfer_valuation import (
	EXACT,
	RECONSTRUCTABLE,
	WAITING_UPSTREAM,
	apply_transfer_reconstruction_to_row,
	collapse_transfer_roots,
	reconstruct_transfer_valuation,
)


class TestTransferValuationV530(unittest.TestCase):
	def test_outgoing_svd_is_authoritative_exact(self):
		row = {
			"purpose": "Material Transfer",
			"parent": "STE-T1",
			"item_code": "ITEM-A",
			"s_warehouse": "WH-S",
			"t_warehouse": "WH-T",
			"qty": 10,
			"basic_rate": 50,
			"posting_date": "2026-01-01",
			"posting_time": "10:00:00",
		}
		out_sle = type(
			"S",
			(),
			{
				"name": "SLE-OUT",
				"warehouse": "WH-S",
				"actual_qty": -10,
				"outgoing_rate": 100,
				"stock_value_difference": -1000,
				"qty_after_transaction": 0,
				"stock_value": 0,
			},
		)()

		with (
			patch(
				"erpnext_extensions.iran_accounting.historical_stock.transfer_valuation._outgoing_sle",
				return_value=out_sle,
			),
			patch(
				"erpnext_extensions.iran_accounting.historical_stock.transfer_valuation._incoming_sle",
				return_value=None,
			),
			patch(
				"erpnext_extensions.iran_accounting.historical_stock.transfer_valuation._source_upstream_health",
				return_value={"status": "healthy", "dependencies": [], "root_voucher": None},
			),
		):
			ev = reconstruct_transfer_valuation(row)
		self.assertEqual(ev["classification"], EXACT)
		self.assertEqual(ev["expected_rate"], 100.0)
		self.assertEqual(ev["authoritative_source"], "outgoing_sle_svd")
		self.assertIn("evidence", ev)

	def test_poisoned_source_is_waiting_upstream(self):
		row = {
			"purpose": "Material Transfer",
			"parent": "STE-T2",
			"item_code": "ITEM-A",
			"s_warehouse": "WH-S",
			"t_warehouse": "WH-T",
			"qty": 5,
			"basic_rate": 0,
			"posting_date": "2026-01-01",
			"posting_time": "10:00:00",
		}
		out_sle = type(
			"S",
			(),
			{
				"name": "SLE-OUT",
				"warehouse": "WH-S",
				"actual_qty": -5,
				"outgoing_rate": 0,
				"stock_value_difference": -500,
				"qty_after_transaction": 0,
				"stock_value": 0,
			},
		)()
		with (
			patch(
				"erpnext_extensions.iran_accounting.historical_stock.transfer_valuation._outgoing_sle",
				return_value=out_sle,
			),
			patch(
				"erpnext_extensions.iran_accounting.historical_stock.transfer_valuation._incoming_sle",
				return_value=None,
			),
			patch(
				"erpnext_extensions.iran_accounting.historical_stock.transfer_valuation._source_upstream_health",
				return_value={
					"status": "poisoned",
					"reason": "zero_rate_with_value_movement",
					"root_voucher": "STE-ROOT",
					"dependencies": ["STE-ROOT"],
				},
			),
		):
			ev = reconstruct_transfer_valuation(row)
		self.assertEqual(ev["classification"], WAITING_UPSTREAM)
		self.assertEqual(ev["root_voucher"], "STE-ROOT")
		upgraded = apply_transfer_reconstruction_to_row(dict(row), cache={})
		# apply calls reconstruct again — patch still needed
		with (
			patch(
				"erpnext_extensions.iran_accounting.historical_stock.transfer_valuation.reconstruct_transfer_valuation",
				return_value=ev,
			),
		):
			upgraded = apply_transfer_reconstruction_to_row(dict(row), cache={})
		self.assertEqual(upgraded["status"], "DEPENDENCY_REPAIR_REQUIRED")
		self.assertFalse(upgraded["eligible"])

	def test_collapse_roots(self):
		rows = [
			{"purpose": "Material Transfer", "voucher": "A", "item": "I", "warehouse": "W", "posting_date": "2026-01-01",
			 "transfer_reconstruction": {"classification": EXACT, "root_voucher": "ROOT1"}},
			{"purpose": "Material Transfer", "voucher": "B", "item": "I", "warehouse": "W", "posting_date": "2026-01-02",
			 "transfer_reconstruction": {"classification": RECONSTRUCTABLE, "root_voucher": "ROOT1"}},
			{"purpose": "Material Transfer", "voucher": "C", "item": "I2", "warehouse": "W2", "posting_date": "2026-01-03",
			 "transfer_reconstruction": {"classification": WAITING_UPSTREAM, "root_voucher": "ROOT2"}},
		]
		c = collapse_transfer_roots(rows)
		self.assertEqual(c["root_chains"], 2)
		self.assertEqual(c["findings"], 3)

	def test_material_issue_zero_refuses_sibling_outgoing_svd(self):
		"""33496 regression: empty SLE.batch_no must not steal sibling 223282 as EXACT."""
		row = {
			"purpose": "Material Issue",
			"parent": "MAT-STE-2026-33496",
			"voucher": "MAT-STE-2026-33496",
			"voucher_detail": "tr8caomlb9",
			"item_code": "13100023",
			"s_warehouse": "WH-Q",
			"t_warehouse": None,
			"qty": 100,
			"basic_rate": 0,
			"batch_no": "5922-13100023-260319",
			"posting_date": "2026-07-27",
			"posting_time": "18:00:25",
		}
		# THIS detail's SLE is zero-valued (the defect).
		own_zero = type(
			"S",
			(),
			{
				"name": "SLE-ZERO",
				"warehouse": "WH-Q",
				"actual_qty": -100,
				"outgoing_rate": 0,
				"stock_value_difference": 0,
				"qty_after_transaction": 122700,
				"stock_value": 133505838800,
				"voucher_detail_no": "tr8caomlb9",
			},
		)()
		with (
			patch(
				"erpnext_extensions.iran_accounting.historical_stock.transfer_valuation._outgoing_sle",
				return_value=own_zero,
			),
			patch(
				"erpnext_extensions.iran_accounting.historical_stock.transfer_valuation._incoming_sle",
				return_value=None,
			),
			patch(
				"erpnext_extensions.iran_accounting.historical_stock.transfer_valuation._source_upstream_health",
				return_value={"status": "healthy", "dependencies": [], "root_voucher": None},
			),
		):
			ev = reconstruct_transfer_valuation(row)
		self.assertEqual(ev["classification"], WAITING_UPSTREAM)
		self.assertEqual(ev["expected_rate"], 0.0)
		self.assertIsNone(ev["authoritative_source"])
		with patch(
			"erpnext_extensions.iran_accounting.historical_stock.transfer_valuation.reconstruct_transfer_valuation",
			return_value=ev,
		):
			upgraded = apply_transfer_reconstruction_to_row(dict(row), cache={})
		self.assertFalse(upgraded.get("eligible"))
		self.assertNotEqual(upgraded.get("confidence"), "EXACT")

	def test_outgoing_sle_prefers_voucher_detail(self):
		from erpnext_extensions.iran_accounting.historical_stock import transfer_valuation as tv

		calls = {}

		def fake_sql(query, args=None, as_dict=False):
			calls["query"] = query
			calls["args"] = list(args or [])
			return []

		with patch.object(tv.frappe.db, "sql", side_effect=fake_sql):
			tv._outgoing_sle("V", "I", "W", batch="B1", voucher_detail="DET-ZERO")
		self.assertIn("voucher_detail_no=%s", calls["query"])
		self.assertEqual(calls["args"][-1], "DET-ZERO")
		# Must not use empty-batch OR wildcard.
		self.assertNotIn("IFNULL(batch_no", calls["query"])


if __name__ == "__main__":
	unittest.main()

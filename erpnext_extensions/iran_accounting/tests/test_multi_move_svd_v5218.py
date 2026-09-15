# Copyright (c) 2026, ERPNext Extensions contributors
"""Unit tests — multi-move PO + SVD residue."""

from __future__ import annotations

import unittest
from unittest.mock import patch


class TestMultiMoveOptimizer(unittest.TestCase):
	def test_collect_and_propose_two_outs(self):
		from erpnext_extensions.iran_accounting.stock_posting_order.multi_move import (
			collect_negative_outbounds,
			propose_multi_move_after_inbound,
			STATUS_MULTI_MOVE_REPAIRABLE,
		)

		def sle(name, vn, qty, dt, vt="Stock Entry"):
			return {
				"name": name,
				"voucher_no": vn,
				"voucher_type": vt,
				"actual_qty": qty,
				"posting_datetime": dt,
				"creation": dt,
			}

		out1 = sle("1", "OUT-1", -6, "2026-04-06 10:00:00")
		out2 = sle("2", "OUT-2", -5, "2026-04-06 11:00:00")
		inn = sle("3", "PR-1", 20, "2026-04-07 09:00:00", vt="Purchase Receipt")
		series = [out1, out2, inn]
		outs = collect_negative_outbounds(series, inn, out1)
		self.assertEqual({o["voucher_no"] for o in outs}, {"OUT-1", "OUT-2"})
		prop = propose_multi_move_after_inbound({"outbound": out1, "inbound": inn}, series)
		self.assertTrue(prop["ok"], prop)
		self.assertEqual(prop["status"], STATUS_MULTI_MOVE_REPAIRABLE)
		self.assertEqual(len(prop["moves"]), 2)
		self.assertEqual(str(prop["proposed"]["min_qty"]), "0")


class TestSvdResidue(unittest.TestCase):
	def test_dry_run_detects_drift(self):
		from erpnext_extensions.iran_accounting.historical_stock.wrong_rate_engine.svd_residue import (
			apply_svd_residue,
		)

		row = {"voucher": "V1", "item": "I1", "expected": 100.0}

		class Sle:
			def __init__(self):
				self.name = "SLE1"
				self.actual_qty = -2
				self.incoming_rate = 0
				self.valuation_rate = 100
				self.stock_value_difference = -150  # should be -200
				self.warehouse = "WH"

		with patch(
			"erpnext_extensions.iran_accounting.historical_stock.wrong_rate_engine.svd_residue.frappe.db.sql",
			return_value=[Sle()],
		):
			# frappe imported inside function — patch module attr after import path
			import erpnext_extensions.iran_accounting.historical_stock.wrong_rate_engine.svd_residue as mod

			with patch.object(mod, "frappe") as frappe_mod:
				frappe_mod.db.sql.return_value = [Sle()]
				out = apply_svd_residue(row, dry_run=True)
		self.assertTrue(out["ok"])
		self.assertEqual(out["changed"], 1)
		self.assertEqual(out["writes"][0]["new"], -200.0)


if __name__ == "__main__":
	unittest.main()

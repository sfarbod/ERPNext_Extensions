# Copyright (c) 2026, ERPNext Extensions contributors
"""Unit tests — I1 apply path is fail-closed (v5.2.22).

v5.2.20/5.2.21 could commit a Stock-Entry-only half-repair: the identity replay refused and
returned ``ok=False`` without writing, the apply ignored the flag, and the post-write check
tested ``incoming_rate`` while the poison sat in ``valuation_rate``. These tests pin the
behaviour that prevents that.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from erpnext_extensions.iran_accounting.historical_stock import I1_READY, I1_WAITING
from erpnext_extensions.iran_accounting.historical_stock.i1_repair import (
	HARD_REPLAY_POISONS,
	_replay_blockers,
	_verify_repair,
	apply_i1_voucher,
)

MOD = "erpnext_extensions.iran_accounting.historical_stock.i1_repair"


def _sle(**kw):
	base = dict(
		name="SLE-1",
		voucher_no="STE-1",
		item_code="FG-1",
		actual_qty=10.0,
		qty_after_transaction=10.0,
		incoming_rate=100.0,
		valuation_rate=100.0,
		stock_value=1000.0,
		stock_value_difference=1000.0,
	)
	base.update(kw)
	return SimpleNamespace(**base)


class TestReplayBlockers(unittest.TestCase):
	def test_missing_identity_is_blocked(self):
		self.assertTrue(_replay_blockers(None, "WH", "2026-01-01 00:00:00"))
		self.assertTrue(_replay_blockers("ITEM", None, "2026-01-01 00:00:00"))
		self.assertTrue(_replay_blockers("ITEM", "WH", None))

	def test_hard_poison_downstream_blocks_replay(self):
		with patch(f"{MOD}._fetch_previous", return_value=None), patch(
			f"{MOD}._fetch_sles", return_value=[_sle(voucher_no="STE-BAD")]
		), patch(f"{MOD}.sle_poison_reason", return_value="exploded_rate"):
			blockers = _replay_blockers("FG-1", "WH", "2026-01-01 00:00:00")
		self.assertEqual(len(blockers), 1)
		self.assertIn("STE-BAD", blockers[0])
		self.assertIn("exploded_rate", blockers[0])

	def test_healthy_chain_has_no_blockers(self):
		with patch(f"{MOD}._fetch_previous", return_value=None), patch(
			f"{MOD}._fetch_sles", return_value=[_sle()]
		), patch(f"{MOD}.sle_poison_reason", return_value=None):
			self.assertEqual(_replay_blockers("FG-1", "WH", "2026-01-01 00:00:00"), [])

	def test_negative_incoming_rate_is_a_hard_poison(self):
		self.assertIn("negative_incoming_rate", HARD_REPLAY_POISONS)
		self.assertIn("exploded_rate", HARD_REPLAY_POISONS)


class TestApplyRefusesWhenNotReady(unittest.TestCase):
	def test_apply_refuses_a_waiting_voucher(self):
		with patch(
			f"{MOD}.classify_i1_voucher",
			return_value={"i1_status": I1_WAITING, "message": "blocked upstream"},
		):
			with self.assertRaises(Exception) as ctx:
				apply_i1_voucher("STE-1")
		self.assertIn("WAITING_I1", str(ctx.exception))

	def test_apply_refuses_when_preflight_finds_a_blocker(self):
		"""READY at scan time, poisoned by the time we write — must not write anything."""
		plan = {
			"i1_status": I1_READY,
			"item": "FG-1",
			"warehouse": "WH-FG",
			"posting_datetime": "2026-01-01 00:00:00",
			"secondary_rows": [{"item": "RM-1", "warehouse": "WH-Store"}],
		}
		with patch(f"{MOD}.classify_i1_voucher", return_value=plan), patch(
			f"{MOD}._replay_blockers", return_value=["STE-BAD is exploded_rate"]
		), patch(f"{MOD}.frappe.db.set_value") as set_value:
			with self.assertRaises(Exception) as ctx:
				apply_i1_voucher("STE-1")
		self.assertIn("replay is blocked", str(ctx.exception))
		set_value.assert_not_called()


def _verify_row(**kw):
	"""A row as ``frappe.db.sql(as_dict=True)`` yields it — attribute access, not a plain dict."""
	base = dict(
		name="SLE-1",
		item_code="FG-1",
		actual_qty=10.0,
		incoming_rate=5.0,
		valuation_rate=5.0,
		stock_value_difference=50.0,
	)
	base.update(kw)
	return SimpleNamespace(**base)


def _gl(debit, credit):
	return [SimpleNamespace(d=debit, c=credit)]


class TestVerifyRepair(unittest.TestCase):
	def test_negative_valuation_rate_is_caught_even_when_incoming_rate_is_positive(self):
		"""The exact false pass: incoming_rate positive, valuation_rate still poisoned."""
		poisoned = _verify_row(
			name="SLE-FG",
			item_code="20100067",
			actual_qty=356.0,
			incoming_rate=5824757088.90,
			valuation_rate=-5824757089.0,
			stock_value_difference=2073613523650.0,
		)
		with patch(f"{MOD}.frappe.db.sql", return_value=[poisoned]):
			out = _verify_repair("STE-1", [])
		self.assertFalse(out["ok"])
		self.assertIn("valuation_rate", out["message"])

	def test_negative_incoming_rate_is_caught(self):
		with patch(f"{MOD}.frappe.db.sql", return_value=[_verify_row(incoming_rate=-5.0)]):
			out = _verify_repair("STE-1", [])
		self.assertFalse(out["ok"])
		self.assertIn("incoming_rate", out["message"])

	def test_negative_inbound_svd_is_caught(self):
		with patch(f"{MOD}.frappe.db.sql", return_value=[_verify_row(stock_value_difference=-50.0)]):
			out = _verify_repair("STE-1", [])
		self.assertFalse(out["ok"])
		self.assertIn("SVD", out["message"])

	def test_unbalanced_gl_is_caught(self):
		# First sql call returns SLE rows, second returns the GL totals.
		with patch(f"{MOD}.frappe.db.sql", side_effect=[[_verify_row()], _gl(100.0, 90.0)]):
			out = _verify_repair("STE-1", [])
		self.assertFalse(out["ok"])
		self.assertIn("unbalanced", out["message"])

	def test_clean_voucher_passes(self):
		with patch(f"{MOD}.frappe.db.sql", side_effect=[[_verify_row()], _gl(100.0, 100.0)]):
			out = _verify_repair("STE-1", [])
		self.assertTrue(out["ok"])


if __name__ == "__main__":
	unittest.main()

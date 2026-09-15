# Copyright (c) 2026, ERPNext Extensions contributors
"""Unit tests — GL root classifier Phase 2 (v5.2.18)."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from erpnext_extensions.iran_accounting.historical_stock import (
	G0_HEALTHY,
	G1_ECONOMICALLY_WRONG,
	G2_MISSING,
	G3_UNBALANCED,
	G4_POISONED_SLE,
	GL_ROOT,
	GL_WAITING_SLE,
)
from erpnext_extensions.iran_accounting.historical_stock.gl_integrity import classify_stock_entry_gl


class TestGLClassifier(unittest.TestCase):
	def _se(self, **kw):
		base = dict(name="STE-1", docstatus=1, company="C", posting_date="2026-01-01", total_outgoing_value=100, total_incoming_value=100)
		base.update(kw)
		return MagicMock(**base) if False else type("SE", (), base)()

	@patch("erpnext_extensions.iran_accounting.historical_stock.gl_integrity._voucher_has_poison_sle", return_value=False)
	@patch("erpnext_extensions.iran_accounting.historical_stock.gl_integrity.frappe")
	def test_g2_missing(self, frappe, _p):
		frappe.db.get_value.return_value = self._se()
		frappe.db.sql.return_value = []
		row = classify_stock_entry_gl("STE-1")
		self.assertEqual(row["gl_class"], G2_MISSING)
		self.assertEqual(row["gl_role"], GL_ROOT)
		self.assertTrue(row["eligible"])

	@patch("erpnext_extensions.iran_accounting.historical_stock.gl_integrity._voucher_has_poison_sle", return_value=False)
	@patch("erpnext_extensions.iran_accounting.historical_stock.gl_integrity.frappe")
	def test_g3_unbalanced(self, frappe, _p):
		frappe.db.get_value.return_value = self._se()
		frappe.db.sql.return_value = [
			type("R", (), dict(account="A", debit=100, credit=0, cost_center="CC", project=None, against=None, party=None, party_type=None))(),
			type("R", (), dict(account="B", debit=0, credit=50, cost_center="CC", project=None, against=None, party=None, party_type=None))(),
		]
		row = classify_stock_entry_gl("STE-1")
		self.assertEqual(row["gl_class"], G3_UNBALANCED)
		self.assertEqual(row["gl_role"], GL_ROOT)

	@patch("erpnext_extensions.iran_accounting.historical_stock.gl_integrity._voucher_has_poison_sle", return_value=False)
	@patch("erpnext_extensions.iran_accounting.historical_stock.gl_integrity.frappe")
	def test_g1_economically_wrong(self, frappe, _p):
		frappe.db.get_value.return_value = self._se(total_outgoing_value=500000, total_incoming_value=500000)
		frappe.db.sql.return_value = [
			type("R", (), dict(account="Inv", debit=100, credit=0, cost_center="CC", project="P1", against=None, party=None, party_type=None))(),
			type("R", (), dict(account="Exp", debit=0, credit=100, cost_center="CC", project="P1", against=None, party=None, party_type=None))(),
		]
		row = classify_stock_entry_gl("STE-1")
		self.assertEqual(row["gl_class"], G1_ECONOMICALLY_WRONG)
		self.assertEqual(row["gl_role"], GL_ROOT)
		self.assertFalse(row["has_stock_adjustment"])
		self.assertFalse(row["has_round_off"])

	@patch("erpnext_extensions.iran_accounting.historical_stock.gl_integrity._voucher_has_poison_sle", return_value=True)
	@patch("erpnext_extensions.iran_accounting.historical_stock.gl_integrity.frappe")
	def test_g4_poisoned(self, frappe, _p):
		frappe.db.get_value.return_value = self._se()
		frappe.db.sql.return_value = [
			type("R", (), dict(account="A", debit=100, credit=0, cost_center="CC", project=None, against=None, party=None, party_type=None))(),
			type("R", (), dict(account="B", debit=0, credit=100, cost_center="CC", project=None, against=None, party=None, party_type=None))(),
		]
		row = classify_stock_entry_gl("STE-1")
		self.assertEqual(row["gl_class"], G4_POISONED_SLE)
		self.assertEqual(row["gl_role"], GL_WAITING_SLE)
		self.assertFalse(row["eligible"])

	@patch("erpnext_extensions.iran_accounting.historical_stock.gl_integrity._voucher_has_poison_sle", return_value=False)
	@patch("erpnext_extensions.iran_accounting.historical_stock.gl_integrity.frappe")
	def test_g0_healthy(self, frappe, _p):
		frappe.db.get_value.return_value = self._se(total_outgoing_value=100, total_incoming_value=100)
		frappe.db.sql.return_value = [
			type("R", (), dict(account="A", debit=100, credit=0, cost_center="CC", project=None, against=None, party=None, party_type=None))(),
			type("R", (), dict(account="B", debit=0, credit=100, cost_center="CC", project=None, against=None, party=None, party_type=None))(),
		]
		row = classify_stock_entry_gl("STE-1")
		self.assertEqual(row["gl_class"], G0_HEALTHY)


if __name__ == "__main__":
	unittest.main()

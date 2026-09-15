# Copyright (c) 2026, ERPNext Extensions contributors
"""Unit tests — Failed RIV Phase 2 classifier (v5.2.18)."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from erpnext_extensions.iran_accounting.historical_stock import (
	RIV_NEGATIVE_STOCK,
	RIV_RAW_MATERIAL_COST,
	RIV_SAFE_TO_RETRY,
	RIV_VALUATION_INTEGRITY,
	RIV_WAITING_GL,
	RIV_WAITING_RATE,
	RIV_WAITING_SLE,
	SLE_HEALTHY,
	SLE_POISONED_CHAIN,
)
from erpnext_extensions.iran_accounting.historical_stock.failed_riv import classify_failed_riv


def _doc(**kw):
	base = dict(
		name="RIV-1",
		item_code="ITEM",
		warehouse="WH",
		voucher_no="STE-1",
		posting_date="2026-01-01",
		error_log="",
	)
	base.update(kw)
	return SimpleNamespace(**base)


class TestFailedRIVClassifier(unittest.TestCase):
	@patch("erpnext_extensions.iran_accounting.historical_stock.failed_riv._has_wrong_rate_dependency", return_value=False)
	@patch("erpnext_extensions.iran_accounting.historical_stock.failed_riv._has_zero_rate_dependency", return_value=False)
	@patch("erpnext_extensions.iran_accounting.historical_stock.failed_riv.classify_stock_entry_gl")
	@patch("erpnext_extensions.iran_accounting.historical_stock.failed_riv.classify_identity", return_value=SLE_HEALTHY)
	def test_safe_deadlock(self, _id, gl, _z, _w):
		gl.return_value = {"gl_class": "G0_HEALTHY"}
		row = classify_failed_riv(_doc(error_log="Deadlock found when trying to get lock"))
		self.assertEqual(row["riv_status"], RIV_SAFE_TO_RETRY)
		self.assertTrue(row["eligible"])

	@patch("erpnext_extensions.iran_accounting.historical_stock.failed_riv._has_wrong_rate_dependency", return_value=True)
	@patch("erpnext_extensions.iran_accounting.historical_stock.failed_riv._has_zero_rate_dependency", return_value=False)
	@patch("erpnext_extensions.iran_accounting.historical_stock.failed_riv.classify_stock_entry_gl")
	@patch("erpnext_extensions.iran_accounting.historical_stock.failed_riv.classify_identity", return_value=SLE_HEALTHY)
	def test_waiting_rate(self, _id, gl, _z, _w):
		gl.return_value = {"gl_class": "G0_HEALTHY"}
		row = classify_failed_riv(_doc(error_log="Deadlock found"))
		self.assertEqual(row["riv_status"], RIV_WAITING_RATE)
		self.assertFalse(row["eligible"])

	@patch("erpnext_extensions.iran_accounting.historical_stock.failed_riv._has_wrong_rate_dependency", return_value=False)
	@patch("erpnext_extensions.iran_accounting.historical_stock.failed_riv._has_zero_rate_dependency", return_value=False)
	@patch("erpnext_extensions.iran_accounting.historical_stock.failed_riv.classify_stock_entry_gl")
	@patch("erpnext_extensions.iran_accounting.historical_stock.failed_riv.classify_identity", return_value=SLE_POISONED_CHAIN)
	def test_waiting_sle(self, _id, gl, _z, _w):
		gl.return_value = {"gl_class": "G0_HEALTHY"}
		row = classify_failed_riv(_doc(error_log="x"))
		self.assertEqual(row["riv_status"], RIV_WAITING_SLE)

	@patch("erpnext_extensions.iran_accounting.historical_stock.failed_riv._has_wrong_rate_dependency", return_value=False)
	@patch("erpnext_extensions.iran_accounting.historical_stock.failed_riv._has_zero_rate_dependency", return_value=False)
	@patch("erpnext_extensions.iran_accounting.historical_stock.failed_riv.classify_stock_entry_gl")
	@patch("erpnext_extensions.iran_accounting.historical_stock.failed_riv.classify_identity", return_value=SLE_HEALTHY)
	def test_waiting_gl(self, _id, gl, _z, _w):
		gl.return_value = {"gl_class": "G3_UNBALANCED"}
		row = classify_failed_riv(_doc(error_log="Debit and Credit not equal"))
		self.assertEqual(row["riv_status"], RIV_WAITING_GL)

	@patch("erpnext_extensions.iran_accounting.historical_stock.failed_riv._has_wrong_rate_dependency", return_value=False)
	@patch("erpnext_extensions.iran_accounting.historical_stock.failed_riv._has_zero_rate_dependency", return_value=False)
	@patch("erpnext_extensions.iran_accounting.historical_stock.failed_riv.classify_stock_entry_gl")
	@patch("erpnext_extensions.iran_accounting.historical_stock.failed_riv.classify_identity", return_value=SLE_HEALTHY)
	def test_negative_stock(self, _id, gl, _z, _w):
		gl.return_value = {"gl_class": "G0_HEALTHY"}
		row = classify_failed_riv(_doc(error_log="The stock for the item is negative"))
		self.assertEqual(row["riv_status"], RIV_NEGATIVE_STOCK)

	@patch("erpnext_extensions.iran_accounting.historical_stock.failed_riv._has_wrong_rate_dependency", return_value=False)
	@patch("erpnext_extensions.iran_accounting.historical_stock.failed_riv._has_zero_rate_dependency", return_value=False)
	@patch("erpnext_extensions.iran_accounting.historical_stock.failed_riv.classify_stock_entry_gl")
	@patch("erpnext_extensions.iran_accounting.historical_stock.failed_riv.classify_identity", return_value=SLE_HEALTHY)
	def test_valuation_poison(self, _id, gl, _z, _w):
		gl.return_value = {"gl_class": "G0_HEALTHY"}
		row = classify_failed_riv(_doc(error_log="Stock valuation integrity I4 leftover"))
		self.assertEqual(row["riv_status"], RIV_VALUATION_INTEGRITY)

	@patch("erpnext_extensions.iran_accounting.historical_stock.failed_riv._has_wrong_rate_dependency", return_value=False)
	@patch("erpnext_extensions.iran_accounting.historical_stock.failed_riv._has_zero_rate_dependency", return_value=False)
	@patch("erpnext_extensions.iran_accounting.historical_stock.failed_riv.classify_stock_entry_gl")
	@patch("erpnext_extensions.iran_accounting.historical_stock.failed_riv.classify_identity", return_value=SLE_HEALTHY)
	def test_raw_material(self, _id, gl, _z, _w):
		gl.return_value = {"gl_class": "G0_HEALTHY"}
		row = classify_failed_riv(_doc(error_log="Get Raw Materials Cost from Consumption Entry"))
		self.assertEqual(row["riv_status"], RIV_RAW_MATERIAL_COST)


if __name__ == "__main__":
	unittest.main()

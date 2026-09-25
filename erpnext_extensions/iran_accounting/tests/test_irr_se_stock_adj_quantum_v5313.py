# Copyright (c) 2026, ERPNext Extensions contributors
"""IRR Stock Entry: one-quantum residual → Stock Adjustment, never Round Off."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from frappe.utils import flt


def _net_diff(gl_map, precision):
	d = sum(flt(getattr(e, "debit", 0)) - flt(getattr(e, "credit", 0)) for e in gl_map)
	d = flt(d, precision)
	return d, d


class TestIRRStockEntryStockAdjustmentQuantum(unittest.TestCase):
	ADJ = "621301 - تعدیلات موجودی کالا - E"
	ROA = "622515 - کسر و اضافات ریالی - E"
	COMPANY = "اسپاد فارمد دارو"

	def _gl_entry(self, account, debit=0.0, credit=0.0, voucher_type="Stock Entry"):
		data = {
			"account": account,
			"debit": debit,
			"credit": credit,
			"debit_in_transaction_currency": debit,
			"credit_in_transaction_currency": credit,
			"debit_in_account_currency": debit,
			"credit_in_account_currency": credit,
			"company": self.COMPANY,
			"voucher_type": voucher_type,
			"voucher_no": "MAT-STE-2026-30470",
			"posting_date": "2026-06-18",
			"remarks": "test",
			"is_opening": "No",
			"cost_center": "3000 - اداری و ستاد - E",
			"project": None,
		}
		ns = SimpleNamespace(**data)

		def _get(k, d=None, _data=data):
			return _data.get(k, d)

		def _set(k, v, _data=data, _ns=ns):
			_data[k] = v
			setattr(_ns, k, v)

		ns.get = _get
		ns.set = _set
		return ns

	def _install_process(self):
		import erpnext.accounts.general_ledger as gl
		from erpnext_extensions.iran_accounting.integration.monkey_patches import (
			_install_iran_process_debit_credit_difference,
		)

		_install_iran_process_debit_credit_difference(gl)
		return gl

	def test_stock_entry_one_quantum_goes_to_stock_adjustment_not_round_off(self):
		gl = self._install_process()
		process = gl.process_debit_credit_difference

		gl_map = [
			self._gl_entry("111605 - Inv", debit=100.0),
			self._gl_entry("111701 - WIP", credit=99.0),
		]
		calls = {"round_off": 0, "throw": 0}

		def fake_round_off(*a, **k):
			calls["round_off"] += 1
			raise AssertionError("Stock Entry must not call make_round_off_gle")

		def fake_throw(*a, **k):
			calls["throw"] += 1
			raise AssertionError("must not throw for one-quantum residual")

		def _cached(dt, name, field=None, *a, **k):
			if dt == "Company" and field == "stock_adjustment_account":
				return self.ADJ
			if dt == "Company" and field == "cost_center":
				return "3000 - اداری و ستاد - E"
			return None

		with patch.object(gl, "get_debit_credit_allowance", return_value=0.5), patch.object(
			gl, "get_debit_credit_difference", side_effect=_net_diff
		), patch(
			"erpnext_extensions.iran_accounting.domain.currency.is_irr_company",
			return_value=True,
		), patch(
			"erpnext_extensions.iran_accounting.domain.currency.get_currency_precision",
			return_value=0,
		), patch.object(gl, "make_round_off_gle", side_effect=fake_round_off), patch.object(
			gl, "raise_debit_credit_not_equal_error", side_effect=fake_throw
		), patch("erpnext.get_company_currency", return_value="IRR"), patch(
			"erpnext_extensions.iran_accounting.domain.irr_gl_precision_align.frappe.get_cached_value",
			side_effect=_cached,
		), patch("frappe.get_cached_value", side_effect=_cached):
			process(gl_map)

		self.assertEqual(calls["throw"], 0)
		self.assertEqual(calls["round_off"], 0)
		accounts = [getattr(e, "account", None) or e.get("account") for e in gl_map]
		self.assertIn(self.ADJ, accounts)
		self.assertNotIn(self.ROA, accounts)
		net = sum(
			flt(getattr(e, "debit", 0) if hasattr(e, "debit") else e.get("debit") or 0)
			- flt(getattr(e, "credit", 0) if hasattr(e, "credit") else e.get("credit") or 0)
			for e in gl_map
		)
		self.assertEqual(flt(net, 6), 0.0)

	def test_stock_entry_large_imbalance_still_throws(self):
		gl = self._install_process()
		process = gl.process_debit_credit_difference
		gl_map = [
			self._gl_entry("111605 - Inv", debit=1100.0),
			self._gl_entry("111701 - WIP", credit=100.0),
		]
		with patch.object(gl, "get_debit_credit_allowance", return_value=0.5), patch.object(
			gl, "get_debit_credit_difference", side_effect=_net_diff
		), patch(
			"erpnext_extensions.iran_accounting.domain.currency.is_irr_company",
			return_value=True,
		), patch(
			"erpnext_extensions.iran_accounting.domain.currency.get_currency_precision",
			return_value=0,
		), patch("erpnext.get_company_currency", return_value="IRR"), patch.object(
			gl,
			"raise_debit_credit_not_equal_error",
			side_effect=Exception("THROW_AS_EXPECTED"),
		), patch.object(gl, "make_round_off_gle", side_effect=AssertionError("no round off")):
			with self.assertRaisesRegex(Exception, "THROW_AS_EXPECTED"):
				process(gl_map)

	def test_journal_entry_still_uses_round_off(self):
		"""Ordinary non-stock accounting rounding keeps Round Off destination."""
		gl = self._install_process()
		process = gl.process_debit_credit_difference
		gl_map = [
			self._gl_entry("111605 - Inv", debit=100.0, voucher_type="Journal Entry"),
			self._gl_entry("111701 - WIP", credit=99.0, voucher_type="Journal Entry"),
		]
		for e in gl_map:
			e.voucher_no = "JV-TEST-1"

		calls = {"round_off": 0}

		def fake_round_off(m, diff, trx, precision):
			calls["round_off"] += 1
			m.append(self._gl_entry(self.ROA, credit=flt(diff, precision), voucher_type="Journal Entry"))

		with patch.object(gl, "get_debit_credit_allowance", return_value=1.0), patch.object(
			gl, "get_debit_credit_difference", side_effect=_net_diff
		), patch(
			"erpnext_extensions.iran_accounting.domain.currency.is_irr_company",
			return_value=True,
		), patch(
			"erpnext_extensions.iran_accounting.domain.currency.get_currency_precision",
			return_value=0,
		), patch.object(gl, "make_round_off_gle", side_effect=fake_round_off), patch(
			"erpnext.get_company_currency", return_value="IRR"
		), patch("frappe.get_cached_value", return_value=None):
			process(gl_map)

		self.assertEqual(calls["round_off"], 1)
		accounts = [getattr(e, "account", None) or e.get("account") for e in gl_map]
		self.assertIn(self.ROA, accounts)
		self.assertNotIn(self.ADJ, accounts)


if __name__ == "__main__":
	unittest.main()

# Copyright (c) 2026, ERPNext Extensions contributors
"""OBSOLETE Round-Off destination for Stock Entry.

Stock-valuation residuals must use Company.stock_adjustment_account.
See test_irr_se_stock_adj_quantum_v5313.py. This file is a regression guard
that Stock Entry must NOT call make_round_off_gle for a one-quantum IRR residual.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from frappe.utils import flt


def _net_diff(gl_map, precision):
	"""Independent of frappe.rounded (needs System Settings / bound local)."""
	d = sum(flt(getattr(e, "debit", 0)) - flt(getattr(e, "credit", 0)) for e in gl_map)
	if precision is not None:
		d = float(round(d, int(precision)))
	return d, d


class TestIRRStockEntryMustNotUseRoundOff(unittest.TestCase):
	ADJ = "621301 - تعدیلات موجودی کالا - E"


	def setUp(self):
		"""Bind minimal frappe.local + rounding so flt(., precision) works off-site."""
		import frappe
		from frappe import _dict
		from unittest.mock import patch
		try:
			_ = frappe.flags
		except RuntimeError:
			frappe.local.flags = _dict()
			frappe.local.conf = _dict()
		self._sys_settings_patch = patch(
			"frappe.get_system_settings",
			return_value="Banker's Rounding (legacy)",
		)
		self._sys_settings_patch.start()

	def tearDown(self):
		if getattr(self, "_sys_settings_patch", None):
			self._sys_settings_patch.stop()


	def _gl_entry(self, account, debit=0.0, credit=0.0):
		data = {
			"account": account,
			"debit": debit,
			"credit": credit,
			"debit_in_transaction_currency": debit,
			"credit_in_transaction_currency": credit,
			"debit_in_account_currency": debit,
			"credit_in_account_currency": credit,
			"company": "اسپاد فارمد دارو",
			"voucher_type": "Stock Entry",
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

	def test_precision0_stock_entry_one_quantum_does_not_call_round_off(self):
		import erpnext.accounts.general_ledger as gl
		from erpnext_extensions.iran_accounting.integration.monkey_patches import (
			_install_iran_process_debit_credit_difference,
		)

		_install_iran_process_debit_credit_difference(gl)
		process = gl.process_debit_credit_difference

		gl_map = [
			self._gl_entry("111605 - Inv", debit=100.0),
			self._gl_entry("111701 - WIP", credit=99.0),
		]
		calls = {"round_off": 0}

		def fake_round_off(*a, **k):
			calls["round_off"] += 1
			raise AssertionError("Stock Entry must not call make_round_off_gle")

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
			gl, "raise_debit_credit_not_equal_error", side_effect=AssertionError("no throw")
		), patch("erpnext.get_company_currency", return_value="IRR"), patch(
			"erpnext_extensions.iran_accounting.domain.irr_gl_precision_align.frappe.get_cached_value",
			side_effect=_cached,
		), patch("frappe.get_cached_value", side_effect=_cached):
			process(gl_map)

		self.assertEqual(calls["round_off"], 0)
		accounts = [getattr(e, "account", None) or e.get("account") for e in gl_map]
		self.assertIn(self.ADJ, accounts)
		net = sum(
			flt(getattr(e, "debit", 0)) - flt(getattr(e, "credit", 0)) for e in gl_map
		)
		self.assertEqual(flt(net, 6), 0.0)


if __name__ == "__main__":
	unittest.main()

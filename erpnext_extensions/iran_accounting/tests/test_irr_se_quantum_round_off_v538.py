# Copyright (c) 2026, ERPNext Extensions contributors
"""IRR Stock Entry: one-quantum debit/credit residual must reach make_round_off_gle.

ERPNext hardcodes Stock Entry allowance at 0.5. For currency precision 0 (IRR) that
creates a dead zone: |diff|==1 throws before make_round_off_gle, while the round-off
branch requires |diff|>=1 and |diff|<=allowance — impossible.

Iran monkey_patches raise the non-JE/PE allowance to one quantum when precision==0.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from frappe.utils import flt


class TestIRRStockEntryQuantumRoundOff(unittest.TestCase):
	def _gl_entry(self, account, debit=0.0, credit=0.0):
		return SimpleNamespace(
			account=account,
			debit=debit,
			credit=credit,
			debit_in_transaction_currency=debit,
			credit_in_transaction_currency=credit,
			company="اسپاد فارمد دارو",
			voucher_type="Stock Entry",
			voucher_no="MAT-STE-2026-30470",
			posting_date="2026-06-18",
			remarks="test",
			is_opening="No",
			get=lambda k, d=None, _a=account, _d=debit, _c=credit: {
				"account": _a,
				"debit": _d,
				"credit": _c,
			}.get(k, d),
		)

	def test_precision0_stock_entry_one_quantum_goes_to_round_off(self):
		"""|diff|==1 must call make_round_off_gle, not raise."""
		import erpnext.accounts.general_ledger as gl
		from erpnext_extensions.iran_accounting.integration import monkey_patches

		# Ensure patch is applied against a fresh module binding.
		monkey_patches.apply_monkey_patches()
		process = gl.process_debit_credit_difference

		gl_map = [
			self._gl_entry("111605 - Inv", debit=100.0),
			self._gl_entry("111701 - WIP", credit=99.0),
		]
		# After flt(., 0): debit-credit = 1

		calls = {"round_off": 0, "throw": 0}

		def fake_round_off(m, diff, trx, precision):
			calls["round_off"] += 1
			# Balance the map the way make_round_off_gle would.
			m.append(
				self._gl_entry("622515 - Round Off", credit=flt(diff, precision))
			)

		def fake_throw(*a, **k):
			calls["throw"] += 1
			raise AssertionError("must not throw for one-quantum residual")

		with patch.object(gl, "get_debit_credit_allowance", return_value=0.5), patch(
			"erpnext_extensions.iran_accounting.domain.currency.is_irr_company",
			return_value=True,
		), patch(
			"erpnext_extensions.iran_accounting.domain.currency.get_currency_precision",
			return_value=0,
		), patch.object(gl, "make_round_off_gle", side_effect=fake_round_off), patch.object(
			gl, "raise_debit_credit_not_equal_error", side_effect=fake_throw
		), patch(
			"erpnext.get_company_currency", return_value="IRR"
		):
			process(gl_map)

		self.assertEqual(calls["throw"], 0)
		self.assertEqual(calls["round_off"], 1)
		net = sum(flt(e.debit) - flt(e.credit) for e in gl_map)
		self.assertEqual(flt(net, 6), 0.0)

	def test_precision0_stock_entry_large_imbalance_still_throws(self):
		import erpnext.accounts.general_ledger as gl
		from erpnext_extensions.iran_accounting.integration import monkey_patches

		monkey_patches.apply_monkey_patches()
		process = gl.process_debit_credit_difference

		gl_map = [
			self._gl_entry("111605 - Inv", debit=1100.0),
			self._gl_entry("111701 - WIP", credit=100.0),
		]

		with patch.object(gl, "get_debit_credit_allowance", return_value=0.5), patch(
			"erpnext_extensions.iran_accounting.domain.currency.is_irr_company",
			return_value=True,
		), patch(
			"erpnext_extensions.iran_accounting.domain.currency.get_currency_precision",
			return_value=0,
		), patch(
			"erpnext.get_company_currency", return_value="IRR"
		), patch.object(
			gl,
			"raise_debit_credit_not_equal_error",
			side_effect=Exception("THROW_AS_EXPECTED"),
		):
			with self.assertRaisesRegex(Exception, "THROW_AS_EXPECTED"):
				process(gl_map)


if __name__ == "__main__":
	unittest.main()

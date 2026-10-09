# Copyright (c) 2026, ERPNext Extensions contributors
"""5.5.27 — RIV ±1 IRR GL residual ownership and bootstrap-safe executors.

Pilot-1 root cause (LOCAL 1000-voucher campaign):
  Direct ``_execute_reposting_entry`` without Iran bootstrap used vanilla
  ``update_rate_on_stock_entry`` (basic_rate = |SVD|/qty float) and vanilla
  ``process_debit_credit_difference`` (Stock Entry allowance 0.5). Per-account
  ROUND_HALF_UP of fractional SLE→GL legs produced Debit/Credit Difference ±1
  and threw — 203 Failed RIVs / 89 Stock Entries.

Economic owner of the proven one-quantum stock-valuation residual:
  Company.stock_adjustment_account (existing TYPE-C / precision-residual contract).
  Not Round Off. Not a new tolerance. Not silence.
"""

from __future__ import annotations

import inspect
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from frappe.utils import flt

from erpnext_extensions.iran_accounting.domain.irr_gl_precision_align import (
	IRR_STOCK_VALUATION_RESIDUAL_REMARK,
	absorb_stock_valuation_precision_residual,
	align_irr_gl_map_to_currency_precision,
)
from erpnext_extensions.iran_accounting.domain.riv_rate_guard import (
	should_accept_riv_propagated_outgoing_rate,
)


def _entry(account, debit=0.0, credit=0.0, **extra):
	row = {
		"account": account,
		"debit": debit,
		"credit": credit,
		"debit_in_account_currency": debit,
		"credit_in_account_currency": credit,
		"debit_in_transaction_currency": debit,
		"credit_in_transaction_currency": credit,
		"cost_center": "CC",
		"company": "اسپاد فارمد دارو",
		"posting_date": "2026-04-01",
		"voucher_type": "Stock Entry",
		"voucher_no": "MAT-STE-TEST-PM1",
		"remarks": "test",
		"is_opening": "No",
	}
	row.update(extra)
	return row


def _flt_precision_safe(value, precision=None):
	"""Site-free flt: frappe.flt(x, 0) can collapse negatives without System Settings."""
	num = float(value or 0)
	if precision is None:
		return num
	return float(round(num, int(precision)))


class TestRivPm1GlResidualV5527(unittest.TestCase):
	def test_plus_one_quantum_absorbed_to_stock_adjustment(self):
		"""+1 IRR excess debit → credit Stock Adjustment (not Round Off)."""
		gl_map = [
			_entry("111701 - WIP", credit=100.0),
			_entry("111625 - FG", debit=101.0),
		]
		with patch(
			"erpnext_extensions.iran_accounting.domain.irr_gl_precision_align.frappe.get_cached_value",
			side_effect=lambda *a, **k: "621301 - SA"
			if a and a[-1] == "stock_adjustment_account"
			else "CC",
		), patch(
			"erpnext_extensions.iran_accounting.domain.irr_gl_precision_align.get_company_currency",
			return_value="IRR",
		), patch(
			"erpnext_extensions.iran_accounting.domain.irr_gl_precision_align.round_currency",
			side_effect=lambda v, *_a, **_k: float(round(float(v or 0))),
		), patch(
			"erpnext_extensions.iran_accounting.domain.irr_gl_precision_align.flt",
			side_effect=_flt_precision_safe,
		):
			ok = absorb_stock_valuation_precision_residual(
				gl_map, 1.0, 1.0, 0, company="اسپاد فارمد دارو"
			)
		self.assertTrue(ok)
		net = sum(float(e["debit"]) - float(e["credit"]) for e in gl_map)
		self.assertEqual(net, 0.0)
		sa = next(e for e in gl_map if e["account"] == "621301 - SA")
		self.assertEqual(float(sa["credit"]), 1.0)
		self.assertEqual(float(sa["debit"]), 0.0)
		self.assertIn(IRR_STOCK_VALUATION_RESIDUAL_REMARK, sa.get("remarks") or "")

	def test_minus_one_quantum_absorbed_to_stock_adjustment(self):
		"""−1 IRR excess credit → debit Stock Adjustment."""
		gl_map = [
			_entry("111701 - WIP", credit=101.0),
			_entry("111625 - FG", debit=100.0),
		]
		with patch(
			"erpnext_extensions.iran_accounting.domain.irr_gl_precision_align.frappe.get_cached_value",
			side_effect=lambda *a, **k: "621301 - SA"
			if a and a[-1] == "stock_adjustment_account"
			else "CC",
		), patch(
			"erpnext_extensions.iran_accounting.domain.irr_gl_precision_align.get_company_currency",
			return_value="IRR",
		), patch(
			"erpnext_extensions.iran_accounting.domain.irr_gl_precision_align.round_currency",
			side_effect=lambda v, *_a, **_k: float(round(float(v or 0))),
		), patch(
			"erpnext_extensions.iran_accounting.domain.irr_gl_precision_align.flt",
			side_effect=_flt_precision_safe,
		):
			ok = absorb_stock_valuation_precision_residual(
				gl_map, -1.0, -1.0, 0, company="اسپاد فارمد دارو"
			)
		self.assertTrue(ok)
		net = sum(float(e["debit"]) - float(e["credit"]) for e in gl_map)
		self.assertEqual(net, 0.0)
		sa = next(e for e in gl_map if e["account"] == "621301 - SA")
		self.assertEqual(float(sa["debit"]), 1.0)

	def test_align_idempotent_second_apply(self):
		doc = SimpleNamespace(
			doctype="Stock Entry",
			name="MAT-STE-TEST-PM1",
			company="اسپاد فارمد دارو",
			purpose="Manufacture",
			posting_date="2026-04-01",
		)
		gl_map = [
			_entry("111701 - WIP", credit=50_671_372.03),
			_entry("621301 - SA", credit=0.48),
			_entry("111605 - Semi", debit=50_671_372.51),
		]
		with patch(
			"erpnext_extensions.iran_accounting.domain.irr_gl_precision_align.is_irr_company",
			return_value=True,
		), patch(
			"erpnext_extensions.iran_accounting.domain.irr_gl_precision_align.get_company_currency",
			return_value="IRR",
		), patch(
			"erpnext_extensions.iran_accounting.domain.irr_gl_precision_align.get_currency_precision",
			return_value=0,
		), patch(
			"erpnext_extensions.iran_accounting.domain.irr_gl_precision_align.frappe.get_cached_value",
			side_effect=lambda *a, **k: "621301 - SA"
			if a and a[-1] == "stock_adjustment_account"
			else "CC",
		), patch(
			"erpnext_extensions.iran_accounting.domain.irr_gl_precision_align.round_currency",
			side_effect=lambda v, *_a, **_k: float(round(flt(v))),
		):
			align_irr_gl_map_to_currency_precision(doc, gl_map)
			snapshot = [(e["account"], flt(e["debit"]), flt(e["credit"])) for e in gl_map]
			align_irr_gl_map_to_currency_precision(doc, gl_map)
			again = [(e["account"], flt(e["debit"]), flt(e["credit"])) for e in gl_map]
		self.assertEqual(snapshot, again)
		self.assertEqual(sum(d - c for _, d, c in again), 0.0)

	def test_material_issue_not_in_riv_propagated_rate_purposes(self):
		"""MI stays rate-first: vanilla |SVD|/qty must not rewrite basic_rate during RIV."""
		self.assertFalse(
			should_accept_riv_propagated_outgoing_rate(
				purpose="Material Issue",
				actual_qty=-10,
				basic_rate=14528.0,
				outgoing_rate=14529.97300005,
				through_riv=True,
			)
		)

	def test_mtfm_accepts_material_riv_propagated_integer_divergence(self):
		self.assertTrue(
			should_accept_riv_propagated_outgoing_rate(
				purpose="Material Transfer for Manufacture",
				actual_qty=-10,
				basic_rate=1000.0,
				outgoing_rate=1500.4,
				through_riv=True,
			)
		)

	def test_ensure_iran_runtime_helper_is_importable_and_idempotent_symbol(self):
		"""Pilot gap closure: private/public RIV executors call bootstrap before work."""
		from erpnext_extensions.iran_accounting.integration import riv_execute_guard as guard

		self.assertTrue(callable(guard._ensure_iran_runtime_for_riv))
		self.assertTrue(callable(guard._wrapped_private_execute_reposting_entry))
		self.assertTrue(callable(guard.execute_reposting_entry))
		# install_execute_reposting_entry_guard must bind the private wrapper.
		src = inspect.getsource(guard.install_execute_reposting_entry_guard)
		self.assertIn("_wrapped_private_execute_reposting_entry", src)
		self.assertIn("_ensure_iran_runtime_for_riv", inspect.getsource(guard.execute_reposting_entry))



if __name__ == "__main__":
	unittest.main()

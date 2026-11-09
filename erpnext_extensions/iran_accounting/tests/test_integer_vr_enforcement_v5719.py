# Copyright (c) 2026, ERPNext Extensions contributors
"""5.7.18 — Mandatory integer valuation_rate; Class A residual for PI-discount floats.

Suite filename retains ``v5719`` historically. Replaces the earlier ULP-skip draft:
fractional VR must never persist under IRR.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from frappe.utils import flt

from erpnext_extensions.iran_accounting.domain.currency import (
	integer_valuation_rate_from_amount,
	round_row_amount,
)
from erpnext_extensions.iran_accounting.domain.irr_residual_classification import (
	classify_amount_rate_residual,
	is_legacy_uvr_float_valuation_rate,
)
from erpnext_extensions.iran_accounting.domain.qty_rate_amount import (
	_align_purchase_valuation_rate_amount_preserving,
)


def _pr_row(**kwargs):
	base = dict(
		name="row1",
		idx=1,
		item_code="ITEM-1",
		qty=46,
		rate=1_737_715,
		amount=79_934_890,
		base_rate=1_737_715,
		base_amount=79_934_890,
		base_net_amount=79_934_890,
		item_tax_amount=0,
		landed_cost_voucher_amount=0,
		amount_difference_with_purchase_invoice=-15,
		valuation_rate=1_737_714.673913044,
		conversion_factor=1,
		stock_qty=46,
		rejected_qty=0,
		sales_incoming_rate=None,
		is_fixed_asset=0,
		cost_center=None,
	)
	base.update(kwargs)
	row = SimpleNamespace(**base)
	row.get = lambda k, d=None: getattr(row, k, d)
	row.precision = lambda field: 6 if field == "stock_qty" else 2
	return row


class TestIntegerVrAlignV5719(unittest.TestCase):
	def test_pi_discount_row_integerizes(self):
		row = _pr_row()
		doc = SimpleNamespace(
			doctype="Purchase Receipt",
			company="Co",
			currency="IRR",
			conversion_rate=1,
			is_old_subcontracting_flow=0,
		)
		doc.get = lambda k, d=None: getattr(doc, k, d)
		with patch(
			"erpnext_extensions.iran_accounting.domain.currency.is_irr_company",
			return_value=True,
		), patch(
			"erpnext_extensions.iran_accounting.domain.currency.get_company_currency",
			return_value="IRR",
		):
			_align_purchase_valuation_rate_amount_preserving(row, doc)
		# auth = 79934890 + (-15) = 79934875 → integer VR 1737715
		self.assertEqual(flt(row.valuation_rate), 1_737_715)
		self.assertEqual(flt(row.valuation_rate), flt(int(row.valuation_rate)))

	def test_lcv_row_integerizes_not_fractional(self):
		row = _pr_row(
			qty=800,
			stock_qty=800,
			base_net_amount=800_000_000,
			amount=800_000_000,
			amount_difference_with_purchase_invoice=0,
			landed_cost_voucher_amount=1500,
			valuation_rate=0,
		)
		doc = SimpleNamespace(
			doctype="Purchase Receipt",
			company="Co",
			currency="IRR",
			conversion_rate=1,
			is_old_subcontracting_flow=0,
		)
		doc.get = lambda k, d=None: getattr(doc, k, d)
		with patch(
			"erpnext_extensions.iran_accounting.domain.currency.is_irr_company",
			return_value=True,
		), patch(
			"erpnext_extensions.iran_accounting.domain.currency.get_company_currency",
			return_value="IRR",
		):
			_align_purchase_valuation_rate_amount_preserving(row, doc)
		self.assertEqual(flt(row.valuation_rate), flt(int(row.valuation_rate)))
		self.assertEqual(flt(row.valuation_rate), 1_000_002)  # ROUND_HALF_UP(800001500/800)


class TestIntegerVrClassifyV5719(unittest.TestCase):
	def test_hrc00014_pi_discount_becomes_class_a_integer(self):
		auth = 79_934_875.0
		qty = 46.0
		vr = 1_737_714.673913044
		out = classify_amount_rate_residual(
			qty=qty,
			authoritative_amount=auth,
			valuation_rate=vr,
			currency="IRR",
			item_code="15010547",
			idx=2,
		)
		self.assertEqual(out["class"], "A")
		self.assertIn(out["reason"], {
			"amount_authoritative_legacy_uvr_float_rate",
			"amount_authoritative_integer_valuation_rate",
		})
		ivr = integer_valuation_rate_from_amount(auth, qty, "IRR")
		self.assertEqual(flt(out["expected_valuation_rate"]), flt(ivr))
		self.assertEqual(flt(ivr), 1_737_715)
		# residual = auth − qty×integer_rate = 79934875 − 79934890 = −15
		self.assertEqual(flt(out["residual"]), -15)

	def test_second_ulp_case_integerizes_class_a(self):
		out = classify_amount_rate_residual(
			qty=3.0,
			authoritative_amount=9_544_090.0,
			valuation_rate=3_181_363.333333334,
			currency="IRR",
			item_code="15010666",
		)
		self.assertEqual(out["class"], "A")
		self.assertEqual(flt(out["expected_valuation_rate"]), flt(int(out["expected_valuation_rate"])))

	def test_true_mismatch_still_class_b(self):
		out = classify_amount_rate_residual(
			qty=15.0,
			authoritative_amount=510_000_000.0,
			valuation_rate=32_584_473.066666666,
			currency="IRR",
		)
		self.assertEqual(out["class"], "B")
		self.assertEqual(out["reason"], "non_integer_rate_under_irr_contract")

	def test_arbitrary_near_int_not_coerced(self):
		# |100 − 3×33.1| = 0.7 > 0.51 and not exact UVR → Class B
		self.assertFalse(is_legacy_uvr_float_valuation_rate(100, 3, 33.1, "IRR"))
		out = classify_amount_rate_residual(
			qty=3.0,
			authoritative_amount=100.0,
			valuation_rate=33.1,
			currency="IRR",
		)
		self.assertEqual(out["class"], "B")

	def test_multi_row_company_shape(self):
		"""Two PI-discount rows → both Class A with integer expected rates."""
		rows = [
			(46.0, 79_934_875.0, 1_737_714.673913044),
			(570.0, 990_497_362.0, 1_737_714.670175439),
		]
		for qty, auth, vr in rows:
			out = classify_amount_rate_residual(
				qty=qty,
				authoritative_amount=auth,
				valuation_rate=vr,
				currency="IRR",
			)
			self.assertEqual(out["class"], "A")
			self.assertEqual(
				flt(out["expected_valuation_rate"]),
				flt(integer_valuation_rate_from_amount(auth, qty, "IRR")),
			)
			self.assertEqual(
				flt(out["expected_amount"]),
				flt(
					round_row_amount(
						qty,
						integer_valuation_rate_from_amount(auth, qty, "IRR"),
						"IRR",
					)
				),
			)


if __name__ == "__main__":
	unittest.main()

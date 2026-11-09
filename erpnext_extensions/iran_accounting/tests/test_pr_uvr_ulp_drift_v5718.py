# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.7.18 — UVR float ±1e-9 persistence drift must not become Class B.

HRC-00014 failed RIV on MAT-PRE-2026-02045 / item 15010547 when stored
valuation_rate was one DECIMAL(30,9) quantum above flt(auth/qty) while
qty × VR still reconstructed acquisition auth.
"""

from __future__ import annotations

import unittest

from erpnext_extensions.iran_accounting.domain.irr_residual_classification import (
	classify_amount_rate_residual,
	is_legacy_uvr_float_valuation_rate,
)


class TestUvrUlpDriftV5718(unittest.TestCase):
	def test_hrc00014_pr02045_row_is_legacy(self):
		# auth = base_net + pi_diff = 79934890 - 15
		self.assertTrue(
			is_legacy_uvr_float_valuation_rate(
				79934875.0, 46.0, 1737714.673913044, "IRR"
			)
		)

	def test_hrc00014_pr02045_classifies_amount_preserving_skip(self):
		out = classify_amount_rate_residual(
			qty=46.0,
			authoritative_amount=79934875.0,
			valuation_rate=1737714.673913044,
			currency="IRR",
			item_code="15010547",
			idx=2,
		)
		self.assertEqual(out["class"], "skip")
		self.assertEqual(out["reason"], "amount_preserving_fractional_vr_zero_residual")
		self.assertEqual(out["residual"], 0)

	def test_second_company_case_pr02094(self):
		# MAT-PRE-2026-02094 / 15010666 qty=3, auth=9544090
		self.assertTrue(
			is_legacy_uvr_float_valuation_rate(
				9544090.0, 3.0, 3181363.333333334, "IRR"
			)
		)
		out = classify_amount_rate_residual(
			qty=3.0,
			authoritative_amount=9544090.0,
			valuation_rate=3181363.333333334,
			currency="IRR",
			item_code="15010666",
			idx=1,
		)
		self.assertEqual(out["class"], "skip")
		self.assertEqual(out["residual"], 0)

	def test_exact_ieee_still_legacy(self):
		self.assertTrue(
			is_legacy_uvr_float_valuation_rate(
				488767096.0, 15.0, 32584473.066666666, "IRR"
			)
		)

	def test_true_mismatch_still_class_b(self):
		out = classify_amount_rate_residual(
			qty=15.0,
			authoritative_amount=510000000.0,
			valuation_rate=32584473.066666666,
			currency="IRR",
		)
		self.assertEqual(out["class"], "B")
		self.assertEqual(out["reason"], "non_integer_rate_under_irr_contract")

	def test_arbitrary_nearby_int_rate_not_legacy(self):
		# ROUND(33.1)=33=ROUND(100/3) but 33.1 is not auth/qty reconstruction
		self.assertFalse(is_legacy_uvr_float_valuation_rate(100, 3, 33.1, "IRR"))
		out = classify_amount_rate_residual(
			qty=3.0,
			authoritative_amount=100.0,
			valuation_rate=33.1,
			currency="IRR",
		)
		self.assertEqual(out["class"], "B")


if __name__ == "__main__":
	unittest.main()

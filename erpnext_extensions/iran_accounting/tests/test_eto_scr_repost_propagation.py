# Copyright (c) 2026, ERPNext Extensions contributors
"""ETO Subcontracting Receipt cost must propagate on repost (open investigation).

See docs/investigations/ETO_SCR_REPOST_PROPAGATION.md.

These tests describe the required behaviour and are skipped until the fix lands.
Remove the skip decorator when implementing the ETO-scoped propagation.
"""

from __future__ import annotations

import unittest

ETO_SERVICE_ITEM = "23000001"
ETO_FG_ITEM = "30600001"


@unittest.skip("Open investigation: ETO SCR cost does not propagate on repost (see docs/investigations)")
class TestEtoScrRepostPropagation(unittest.TestCase):
	def test_transfer_outward_uses_batchwise_rate_after_scr_correction(self):
		"""Transfer outward leg of an ETO SCR batch takes the corrected SABE batch rate.

		Given an SCR inward of 61 × 2,216,580 (batch created by SCR for 23000001),
		a Material Transfer for Manufacture of the same 61 must post
		stock_value_difference -135,211,380 after RIV, and its SED row must align.
		"""
		raise NotImplementedError

	def test_manufacture_fg_recalculated_from_corrected_consume(self):
		"""Manufacture consuming the ETO batch recalculates FG from the corrected consume value."""
		raise NotImplementedError

	def test_non_eto_items_unchanged(self):
		"""Items whose batches are not from an ETO SCR keep the current document-authority behaviour."""
		raise NotImplementedError

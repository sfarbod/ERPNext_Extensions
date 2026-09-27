# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.3.19 — Purchase Receipt provenance beats previous_healthy for lost transfers.

MAT-STE-2026-33937 class: outgoing SVD/rate lost; SABB.posting_date NULL on a
later Purchase Receipt must not poison unanimous inbound; PR valuation_rate
before the transfer is EXACT document authority.
"""

from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import patch

from erpnext_extensions.iran_accounting.historical_stock.transfer_valuation import (
	EXACT,
	_batch_inward_sabb_rate,
	_batch_purchase_receipt_rate,
	_filter_rows_on_or_before,
	reconstruct_transfer_valuation,
)


class TestPrTransferProvenanceV5319(unittest.TestCase):
	def test_filter_resolves_null_sabb_via_voucher_date(self):
		rows = [
			type(
				"R",
				(),
				{
					"posting_date": None,
					"voucher_type": "Purchase Receipt",
					"voucher_no": "PR-EARLY",
					"incoming_rate": 600000,
				},
			)(),
			type(
				"R",
				(),
				{
					"posting_date": None,
					"voucher_type": "Purchase Receipt",
					"voucher_no": "PR-LATE",
					"incoming_rate": 660000,
				},
			)(),
		]

		def _vd(vt, vn):
			return {"PR-EARLY": date(2026, 7, 26), "PR-LATE": date(2026, 8, 4)}.get(vn)

		with patch(
			"erpnext_extensions.iran_accounting.historical_stock.transfer_valuation._voucher_posting_date",
			side_effect=_vd,
		):
			kept = _filter_rows_on_or_before(rows, "2026-08-01")
		self.assertEqual(len(kept), 1)
		self.assertEqual(kept[0].voucher_no, "PR-EARLY")

	def test_lost_outgoing_uses_purchase_receipt_exact(self):
		row = {
			"purpose": "Material Transfer",
			"parent": "MAT-STE-2026-33937",
			"voucher": "MAT-STE-2026-33937",
			"item_code": "13100023",
			"batch_no": "5922-13100023-260319",
			"s_warehouse": "WH-Q",
			"t_warehouse": "WH-A",
			"qty": 1500,
			"basic_rate": 0,
			"posting_date": "2026-08-01",
			"posting_time": "18:01:20",
		}
		out_sle = type(
			"S",
			(),
			{
				"name": "SLE-OUT",
				"warehouse": "WH-Q",
				"actual_qty": -1500,
				"outgoing_rate": 0,
				"stock_value_difference": 0,
				"qty_after_transaction": 67500,
				"stock_value": 1,
				"voucher_detail_no": "d1",
			},
		)()
		pr = {
			"rate": 600000.0,
			"receipt_count": 1,
			"vouchers": ["MAT-PRE-2026-01654"],
			"first_voucher": "MAT-PRE-2026-01654",
			"last_voucher": "MAT-PRE-2026-01654",
			"warehouse": "WH-Q",
		}

		with (
			patch(
				"erpnext_extensions.iran_accounting.historical_stock.transfer_valuation._outgoing_sle",
				return_value=out_sle,
			),
			patch(
				"erpnext_extensions.iran_accounting.historical_stock.transfer_valuation._incoming_sle",
				return_value=None,
			),
			patch(
				"erpnext_extensions.iran_accounting.historical_stock.transfer_valuation._batch_stock_reco_rate",
				return_value=None,
			),
			patch(
				"erpnext_extensions.iran_accounting.historical_stock.transfer_valuation._batch_purchase_receipt_rate",
				return_value=pr,
			),
			patch(
				"erpnext_extensions.iran_accounting.historical_stock.transfer_valuation._previous_healthy",
				return_value={"name": "SLE-TIP", "voucher_no": "STE-OTHER", "rate": 440596.0},
			),
			patch(
				"erpnext_extensions.iran_accounting.historical_stock.transfer_valuation._source_upstream_health",
				return_value={"status": "healthy", "dependencies": [], "root_voucher": None},
			),
		):
			ev = reconstruct_transfer_valuation(row)

		self.assertEqual(ev["classification"], EXACT)
		self.assertEqual(ev["expected_rate"], 600000.0)
		self.assertEqual(ev["authoritative_source"], "batch_purchase_receipt_rate")
		self.assertNotEqual(ev["expected_rate"], 440596.0)

	def test_purchase_receipt_helper_signature(self):
		# Import-time smoke: helpers remain public for reconstruction.
		self.assertTrue(callable(_batch_purchase_receipt_rate))
		self.assertTrue(callable(_batch_inward_sabb_rate))


if __name__ == "__main__":
	unittest.main()

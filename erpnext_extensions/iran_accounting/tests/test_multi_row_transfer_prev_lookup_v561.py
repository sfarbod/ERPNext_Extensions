# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.6.1: multi-row Material Transfer must not absorb against pre-voucher balance."""

from __future__ import annotations

import unittest
from unittest import mock

from frappe.utils import flt


class TestMultiRowTransferPrevLookup(unittest.TestCase):
	def test_lookup_includes_same_voucher_prior_sle(self):
		from erpnext_extensions.iran_accounting.domain import stock_entry_sync as ses

		sle = {
			"item_code": "13100028",
			"warehouse": "WH-APPROVED",
			"name": "SLE-ROW-2",
			"voucher_no": "MAT-STE-2026-25926",
			"posting_datetime": "2026-04-12 18:00:40",
			"creation": "2026-09-11 19:25:13.862140",
		}
		captured = {}

		def fake_sql(query, params=None):
			captured["query"] = " ".join(query.split())
			captured["params"] = params
			# Same-voucher prior row stock_value (correct previous layer).
			return [(323931648.0,)]

		with mock.patch("frappe.db.sql", side_effect=fake_sql):
			prev = ses._lookup_previous_stock_value(sle)

		self.assertEqual(flt(prev), 323931648.0)
		self.assertNotIn("voucher_no !=", captured["query"])
		self.assertIn("name != %s", captured["query"])
		# Params must not push voucher_no as an exclusion filter.
		self.assertNotIn("MAT-STE-2026-25926", (captured["params"] or ())[2:])

	def test_lookup_prefers_chronological_prior_not_pre_voucher(self):
		"""Document the 25926 regression shape: pre-voucher 458903168 vs prior-row 323931648."""
		from erpnext_extensions.iran_accounting.domain import stock_entry_sync as ses

		sle = mock.Mock()
		sle.get = mock.Mock(
			side_effect=lambda k, d=None: {
				"item_code": "13100028",
				"warehouse": "WH",
				"name": "SLE-2",
				"voucher_no": "V1",
				"posting_datetime": "2026-04-12 18:00:40",
				"creation": "2026-09-11 19:25:13.862140",
			}.get(k, d)
		)

		with mock.patch(
			"frappe.db.sql",
			return_value=[(323931648.0,)],
		) as sql:
			prev = ses._lookup_previous_stock_value(sle)

		self.assertEqual(flt(prev), 323931648.0)
		q = " ".join(sql.call_args[0][0].split())
		self.assertNotIn("voucher_no !=", q)


if __name__ == "__main__":
	unittest.main()

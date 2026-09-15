# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.2.15 campaign framework unit tests (no live DB writes required for pure helpers)."""

from __future__ import annotations

import unittest

from erpnext_extensions.iran_accounting.historical_stock.campaign_clusters import (
	SAFE_GROUP,
	SAFE_SEQUENTIAL_GROUP,
	UNSAFE_GROUP,
	classify_group,
	cluster_independent_roots,
)
from erpnext_extensions.iran_accounting.historical_stock.campaign_wizard import campaign_comparison


class TestCampaignClusters(unittest.TestCase):
	def test_safe_group_distinct_identities(self):
		roots = [
			{"voucher": "A", "item": "I1", "warehouse": "W1", "confidence": "EXACT", "eligible": True, "patient_zero": {"voucher_no": "PZ1"}},
			{"voucher": "B", "item": "I2", "warehouse": "W1", "confidence": "EXACT", "eligible": True, "patient_zero": {"voucher_no": "PZ2"}},
		]
		self.assertEqual(classify_group(roots), SAFE_GROUP)
		cl = cluster_independent_roots(roots, max_cluster=10)
		self.assertGreaterEqual(cl["independent_root_count"], 2)
		self.assertTrue(cl["safe_groups"])

	def test_unsafe_shared_patient_zero(self):
		roots = [
			{"voucher": "A", "item": "I1", "warehouse": "W1", "confidence": "EXACT", "eligible": True, "patient_zero": {"voucher_no": "PZ1"}},
			{"voucher": "B", "item": "I2", "warehouse": "W2", "confidence": "EXACT", "eligible": True, "patient_zero": {"voucher_no": "PZ1"}},
		]
		cl = cluster_independent_roots(roots, max_cluster=10)
		self.assertTrue(cl["unsafe_groups"])
		self.assertEqual(cl["unsafe_groups"][0]["group_class"], UNSAFE_GROUP)

	def test_sequential_same_identity(self):
		roots = [
			{"voucher": "A", "item": "I1", "warehouse": "W1", "confidence": "EXACT", "eligible": True, "patient_zero": {"voucher_no": "PZ1"}, "posting_date": "2026-01-01"},
			{"voucher": "B", "item": "I1", "warehouse": "W1", "confidence": "EXACT", "eligible": True, "patient_zero": {"voucher_no": "PZ1"}, "posting_date": "2026-02-01"},
		]
		self.assertEqual(classify_group(roots), SAFE_SEQUENTIAL_GROUP)

	def test_shared_pz_promotes_pz_root_only(self):
		roots = [
			{"voucher": "PZ1", "item": "I1", "warehouse": "W1", "confidence": "EXACT", "eligible": True, "patient_zero": {"voucher_no": "PZ1"}},
			{"voucher": "D1", "item": "I2", "warehouse": "W2", "confidence": "EXACT", "eligible": True, "patient_zero": {"voucher_no": "PZ1"}},
		]
		cl = cluster_independent_roots(roots, max_cluster=10)
		self.assertGreaterEqual(cl["independent_root_count"], 1)
		self.assertTrue(cl["safe_groups"] or cl["recommended_first_group"])
		rec = cl["recommended_first_group"]
		self.assertEqual(rec["repair_order"], ["PZ1"])
		self.assertTrue(cl["unsafe_groups"])


if __name__ == "__main__":
	unittest.main()

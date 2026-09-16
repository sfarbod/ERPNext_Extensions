# Copyright (c) 2026, ERPNext Extensions contributors
"""Release gate — Historical Repair UI routing / topic isolation (v5.2.18)."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

JS = (
	Path(__file__).resolve().parents[2]
	/ "erpnext_extensions"
	/ "page"
	/ "historical_repair"
	/ "historical_repair.js"
)


def _js() -> str:
	return JS.read_text(encoding="utf-8")


class TestHistoricalRepairRoutingV5218(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.src = _js()
		assert JS.is_file(), f"missing {JS}"

	def test_wrong_and_zero_scan_apis_are_distinct(self):
		# Scan map must route wrong → scan_wrong_rates_api and zero → scan_zero_rates
		m = re.search(r"scan\(opts\)\s*\{.*?const map = \{(?P<body>.*?)\};", self.src, re.S)
		self.assertIsNotNone(m)
		body = m.group("body")
		self.assertIn("wrong:", body)
		self.assertIn("scan_wrong_rates_api", body)
		self.assertIn("zero:", body)
		self.assertIn("scan_zero_rates", body)
		# Wrong must not call zero scan
		wrong_line = [ln for ln in body.splitlines() if "wrong:" in ln][0]
		self.assertNotIn("scan_zero_rates", wrong_line)

	def test_repair_apis_are_topic_isolated(self):
		self.assertIn('this.topic === "wrong"', self.src)
		self.assertIn("repair_wrong_rates_selected", self.src)
		self.assertIn('this.topic === "zero"', self.src)
		self.assertIn("repair_zero_rates_selected", self.src)
		# Zero repair must not call wrong repair
		# Extract _execute_repair zero branch
		m = re.search(
			r'else if \(this\.topic === "zero"\) \{\s*method = `\$\{this\.api\}\.([^`]+)`',
			self.src,
		)
		self.assertIsNotNone(m)
		self.assertEqual(m.group(1), "repair_zero_rates_selected")
		m2 = re.search(
			r'else if \(this\.topic === "wrong"\) \{\s*method = `\$\{this\.api\}\.([^`]+)`',
			self.src,
		)
		self.assertIsNotNone(m2)
		self.assertEqual(m2.group(1), "repair_wrong_rates_selected")

	def test_kpi_topic_map_wrong_not_zero(self):
		m = re.search(r"const KPI_TOPIC = \{(?P<body>.*?)\};", self.src, re.S)
		self.assertIsNotNone(m)
		body = m.group("body")
		self.assertIn('"Wrong Rate": "wrong"', body)
		self.assertIn('"Zero Rate": "zero"', body)
		self.assertNotRegex(body, r'"Wrong Rate":\s*"zero"')

	def test_warehouse_plan_handler_exists_and_calls_api(self):
		self.assertIn("show_warehouse_plan()", self.src)
		self.assertIn("warehouse_plan_api", self.src)
		self.assertIn("warehouse_dry_run_api", self.src)

	def test_posting_only_tools_are_gated(self):
		self.assertIn('this.topic !== "posting"', self.src)
		self.assertIn("Replay Downstream is a Posting Order identity tool", self.src)
		self.assertIn("Rebuild Affected Documents is a Posting Order identity tool", self.src)

	def test_cluster_explorer_is_zero_scoped(self):
		self.assertIn("classify_zero_clusters_api", self.src)
		self.assertIn("Cluster Explorer classifies Zero Rate", self.src)

	def test_unload_contract_message_present(self):
		self.assertIn("Rows are not loaded yet.", self.src)
		self.assertIn("Click Scan to load this topic.", self.src)
		self.assertIn("data-role=\"rows-not-loaded\"", self.src)
		# Must not use silent empty copy alone for unload
		self.assertIn("hr-empty-unload", self.src)

	def test_topics_include_wrong_rate_tab(self):
		m = re.search(r"const TOPICS = \[(?P<body>.*?)\];", self.src, re.S)
		self.assertIsNotNone(m)
		body = m.group("body")
		self.assertIn('id: "wrong"', body)
		self.assertIn('id: "zero"', body)
		self.assertIn('id: "posting"', body)

	def test_scan_all_invalidates_topic_cache(self):
		self.assertIn("_invalidate_topic_rows", self.src)
		self.assertIn("_on_company_change", self.src)
		self.assertIn("topic_expectations", self.src)
		self.assertIn("_last_company_value", self.src)
		self.assertIn("Ignore control bootstrap", self.src)


class TestTopicScanRepairContractV5218(unittest.TestCase):
	"""Backend topic APIs stay isolated."""

	def test_wrong_and_zero_modules_distinct(self):
		from erpnext_extensions.iran_accounting.historical_stock import api

		self.assertTrue(callable(api.scan_wrong_rates_api))
		self.assertTrue(callable(api.scan_zero_rates))
		self.assertTrue(callable(api.repair_wrong_rates_selected))
		self.assertTrue(callable(api.repair_zero_rates_selected))
		self.assertNotEqual(api.scan_wrong_rates_api.__name__, api.scan_zero_rates.__name__)
		self.assertNotEqual(
			api.repair_wrong_rates_selected.__name__, api.repair_zero_rates_selected.__name__
		)

	def test_warehouse_plan_apis_whitelisted(self):
		from erpnext_extensions.iran_accounting.historical_stock import api

		for name in (
			"warehouse_plan_api",
			"warehouse_dry_run_api",
			"warehouse_apply_api",
			"warehouse_dependency_api",
		):
			self.assertTrue(hasattr(api, name), name)


if __name__ == "__main__":
	unittest.main()

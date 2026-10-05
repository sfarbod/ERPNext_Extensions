"""Run core Job Card Stock Rebuild regressions without full app discovery."""

from __future__ import annotations

import unittest


MODULES = [
	"erpnext_extensions.iran_accounting.job_card_stock_rebuild.tests.test_golden_rule_v550",
	"erpnext_extensions.iran_accounting.job_card_stock_rebuild.tests.test_atomic_repair_v550",
	"erpnext_extensions.iran_accounting.job_card_stock_rebuild.tests.test_queued_dry_run_v553",
	"erpnext_extensions.iran_accounting.job_card_stock_rebuild.tests.test_queued_apply_v554",
	"erpnext_extensions.iran_accounting.job_card_stock_rebuild.tests.test_disposition_prefill_v552",
	"erpnext_extensions.iran_accounting.job_card_stock_rebuild.tests.test_savepoint_scan_v553",
]


def run():
	loader = unittest.TestLoader()
	suite = unittest.TestSuite()
	for m in MODULES:
		suite.addTests(loader.loadTestsFromName(m))
	result = unittest.TextTestRunner(verbosity=2).run(suite)
	details = []
	for test, err in list(result.failures) + list(result.errors):
		details.append({"test": str(test), "error": err[:2000]})
	return {
		"ok": result.wasSuccessful(),
		"testsRun": result.testsRun,
		"failures": len(result.failures),
		"errors": len(result.errors),
		"details": details,
	}

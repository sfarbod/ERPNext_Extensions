"""Run related v5.5.0 plan/shared/mi tests without full app discovery."""

from __future__ import annotations

import unittest


def run():
	modules = [
		"erpnext_extensions.iran_accounting.job_card_stock_rebuild.tests.test_shared_logistics_v550",
		"erpnext_extensions.iran_accounting.job_card_stock_rebuild.tests.test_mi_ownership_v550",
		"erpnext_extensions.iran_accounting.job_card_stock_rebuild.tests.test_manufacture_plan_v550",
	]
	loader = unittest.TestLoader()
	suite = unittest.TestSuite()
	for m in modules:
		suite.addTests(loader.loadTestsFromName(m))
	result = unittest.TextTestRunner(verbosity=2).run(suite)
	details = []
	for test, err in list(result.failures) + list(result.errors):
		details.append({"test": str(test), "error": err[:1500]})
	return {
		"ok": result.wasSuccessful(),
		"testsRun": result.testsRun,
		"failures": len(result.failures),
		"errors": len(result.errors),
		"details": details,
	}

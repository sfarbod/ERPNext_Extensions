"""Run Golden Rule Audit tests without full app discovery."""

from __future__ import annotations

import unittest


def run():
	loader = unittest.TestLoader()
	suite = loader.loadTestsFromName(
		"erpnext_extensions.iran_accounting.job_card_stock_rebuild.tests.test_golden_rule_audit_v550"
	)
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

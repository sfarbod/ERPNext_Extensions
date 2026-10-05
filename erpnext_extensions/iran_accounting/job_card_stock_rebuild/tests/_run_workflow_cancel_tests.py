"""Run workflow-cancel WC tests without full app test discovery."""

from __future__ import annotations

import unittest


def run():
	from erpnext_extensions.iran_accounting.job_card_stock_rebuild.tests.test_workflow_cancel_v556 import (
		TestWorkflowCancelV556,
	)

	loader = unittest.TestLoader()
	suite = loader.loadTestsFromTestCase(TestWorkflowCancelV556)
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

"""Run temporary bridge tests without full app test discovery."""

from __future__ import annotations

import traceback
import unittest


def run():
	from erpnext_extensions.iran_accounting.job_card_stock_rebuild.tests.test_temporary_bridge_v550 import (
		TestTemporaryBridgeV550,
	)

	loader = unittest.TestLoader()
	suite = loader.loadTestsFromTestCase(TestTemporaryBridgeV550)
	result = unittest.TextTestRunner(verbosity=2).run(suite)
	failures = []
	for test, err in list(result.failures) + list(result.errors):
		failures.append({"test": str(test), "error": err[:1200]})
	return {
		"ok": result.wasSuccessful(),
		"testsRun": result.testsRun,
		"failures": len(result.failures),
		"errors": len(result.errors),
		"details": failures,
	}


def run_quick():
	"""Shortages + plan + lifecycle + one failpoint + dry-run pass."""
	from erpnext_extensions.iran_accounting.job_card_stock_rebuild.tests.test_temporary_bridge_v550 import (
		TestTemporaryBridgeV550,
	)

	t = TestTemporaryBridgeV550()
	t.setUp()
	out = {"steps": []}
	try:
		t.test_shortage_exact_three_rows()
		out["steps"].append({"id": "shortage", "ok": True})
		t.test_plan_bridge_unlock()
		out["steps"].append({"id": "plan", "ok": True})
		t.test_tr20_create_submit_cancel_delete_lifecycle()
		out["steps"].append({"id": "lifecycle", "ok": True})
		# single failpoint sample
		import frappe
		from erpnext_extensions.iran_accounting.job_card_stock_rebuild.atomic_repair import run_repair

		before = t._baseline()
		frappe.flags.jc_repair_fail_at = "after_temp_receipt_submit"
		res = run_repair(t.JC if hasattr(t, "JC") else "PO-JOB08760", plan_input=t._plan_input(), dry_run=True)
		frappe.flags.jc_repair_fail_at = None
		after = t._baseline()
		ok = before == after and res.get("status") == "DRY_RUN_FAIL"
		out["steps"].append({"id": "TR01", "ok": ok, "status": res.get("status")})
		res2 = run_repair("PO-JOB08760", plan_input=t._plan_input(), dry_run=True)
		out["steps"].append(
			{
				"id": "DRY_RUN",
				"ok": bool(res2.get("ok")) and res2.get("status") == "DRY_RUN_PASS",
				"status": res2.get("status"),
				"error": (res2.get("error") or "")[:300],
			}
		)
	except Exception as exc:
		out["ok"] = False
		out["error"] = str(exc)
		out["trace"] = traceback.format_exc()[-1500:]
		return out
	finally:
		t.tearDown()
	out["ok"] = all(s.get("ok") for s in out["steps"])
	return out

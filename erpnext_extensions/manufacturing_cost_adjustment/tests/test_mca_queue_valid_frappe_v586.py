# Copyright (c) 2026, ERPNext Extensions contributors
"""Regression: MCA must enqueue only on standard Frappe queues.

Production UI failure: ``Queue should be one of short, default, long``
was caused by ``MCA_RQ_QUEUE = \"mca\"`` (unsupported custom queue name).
"""
from __future__ import annotations

import inspect
import unittest
from unittest.mock import MagicMock, patch


class TestMCAQueueValidFrappe(unittest.TestCase):
	def test_mca_rq_queue_is_standard_frappe_queue(self):
		from erpnext_extensions.manufacturing_cost_adjustment import constants as C

		self.assertIn(C.MCA_RQ_QUEUE, C.VALID_FRAPPE_QUEUES)
		self.assertEqual(C.MCA_RQ_QUEUE, "long")
		self.assertNotEqual(C.MCA_RQ_QUEUE, "mca")
		self.assertIn(C.MCA_RIV_RQ_QUEUE, C.VALID_FRAPPE_QUEUES)

	def test_on_submit_enqueues_valid_queue(self):
		from erpnext_extensions.manufacturing_cost_adjustment import constants as C
		from erpnext_extensions.manufacturing_cost_adjustment.doctype.manufacturing_cost_adjustment import (
			manufacturing_cost_adjustment as mca_mod,
		)

		src = inspect.getsource(mca_mod.ManufacturingCostAdjustment.on_submit)
		self.assertIn("MCA_RQ_QUEUE", src)
		# Must not hardcode the invalid custom queue name
		self.assertNotIn('queue="mca"', src)
		self.assertNotIn("queue='mca'", src)

	def test_retry_enqueues_valid_queue(self):
		from erpnext_extensions.manufacturing_cost_adjustment.doctype.manufacturing_cost_adjustment import (
			manufacturing_cost_adjustment as mca_mod,
		)

		src = inspect.getsource(mca_mod.retry_mca_processing)
		self.assertIn("MCA_RQ_QUEUE", src)
		self.assertNotIn('queue="mca"', src)

	def test_scheduler_enqueues_valid_queue(self):
		from erpnext_extensions.manufacturing_cost_adjustment import repost_engine as re

		src = inspect.getsource(re.process_pending_mca_reposts)
		self.assertIn("MCA_RQ_QUEUE", src)
		self.assertNotIn('queue="mca"', src)

	def test_enqueue_kwargs_reject_custom_mca_queue_name(self):
		"""Simulate Frappe's queue allow-list check used at enqueue time."""
		from erpnext_extensions.manufacturing_cost_adjustment import constants as C

		allowed = C.VALID_FRAPPE_QUEUES
		self.assertIn(C.MCA_RQ_QUEUE, allowed)

		def fake_enqueue(*, queue, **_kwargs):
			if queue not in ("short", "default", "long"):
				raise ValueError(f"Queue should be one of short, default, long")
			return MagicMock()

		# Valid constant must pass
		fake_enqueue(queue=C.MCA_RQ_QUEUE)
		# Historical invalid value must fail (documents the regression)
		with self.assertRaises(ValueError) as ctx:
			fake_enqueue(queue="mca")
		self.assertIn("Queue should be one of short, default, long", str(ctx.exception))


def run_checks():
	results = []

	def check(name, fn):
		try:
			fn()
			results.append({"name": name, "ok": True})
			print("PASS", name)
		except Exception as e:
			results.append({"name": name, "ok": False, "error": str(e)})
			print("FAIL", name, e)

	t = TestMCAQueueValidFrappe()
	check("mca_rq_queue_is_standard_frappe_queue", t.test_mca_rq_queue_is_standard_frappe_queue)
	check("on_submit_enqueues_valid_queue", t.test_on_submit_enqueues_valid_queue)
	check("retry_enqueues_valid_queue", t.test_retry_enqueues_valid_queue)
	check("scheduler_enqueues_valid_queue", t.test_scheduler_enqueues_valid_queue)
	check("enqueue_kwargs_reject_custom_mca_queue_name", t.test_enqueue_kwargs_reject_custom_mca_queue_name)
	ok = all(r["ok"] for r in results)
	return {"ok": ok, "checks": results}


if __name__ == "__main__":
	import frappe

	frappe.init(site="development.localhost")
	frappe.connect()
	out = run_checks()
	print("UNIT_RESULTS", out)
	raise SystemExit(0 if out["ok"] else 1)

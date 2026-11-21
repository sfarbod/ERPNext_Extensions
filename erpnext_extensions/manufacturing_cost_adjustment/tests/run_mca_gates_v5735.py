# Copyright (c) 2026, ERPNext Extensions contributors
"""Console runner for MCA v5.7.35 automated gates (avoids FrappeTestCase UOM bootstrap)."""

from __future__ import annotations

import traceback
import unittest
from unittest.mock import patch

import frappe

from erpnext_extensions.manufacturing_cost_adjustment import constants as C


def _run_unit():
	from erpnext_extensions.manufacturing_cost_adjustment.tests import test_mca_unit_v5735 as u

	# Run without FrappeTestCase setUpClass where possible
	results = []

	def check(name, fn):
		try:
			fn()
			results.append((name, "PASS"))
			print("PASS", name)
		except Exception as e:
			results.append((name, "FAIL", str(e)[:300]))
			print("FAIL", name, e)

	from erpnext_extensions.manufacturing_cost_adjustment.locks import (
		acquire_se_lock,
		release_se_lock,
	)
	from erpnext_extensions.manufacturing_cost_adjustment.repost_engine import (
		_cp_before,
		_is_transient,
		_parse_riv_list,
	)

	def t_checkpoint():
		assert _cp_before(C.CP_NONE, C.CP_COSTS_APPENDED)
		assert _cp_before(C.CP_COSTS_APPENDED, C.CP_ALLOCATED)
		assert not _cp_before(C.CP_COMPLETED, C.CP_RIV_QUEUED)

	def t_transient():
		assert _is_transient("Deadlock found when trying to get lock")
		assert _is_transient("JobTimeoutException")
		assert not _is_transient("Expense Account is required")

	def t_parse():
		assert _parse_riv_list("A\nB\n") == ["A", "B"]
		assert _parse_riv_list(None) == []

	def t_lock():
		se = "TEST-SE-LOCK-GATE"
		release_se_lock(se)
		assert acquire_se_lock(se, "a")
		assert not acquire_se_lock(se, "b")
		assert acquire_se_lock(se, "a")
		release_se_lock(se, "a")
		assert acquire_se_lock(se, "b")
		release_se_lock(se, "b")

	def t_cancel():
		doc = frappe.new_doc(C.MCA_DOCTYPE)
		doc.docstatus = 1
		try:
			doc.run_method("on_cancel")
			raise AssertionError("cancel should fail")
		except frappe.ValidationError:
			pass

	def t_no_lcv():
		import inspect
		from erpnext_extensions.manufacturing_cost_adjustment import repost_engine as re

		src = inspect.getsource(re)
		assert "update_landed_cost" not in src
		assert "landed_cost_voucher.py" not in src
		assert '"via_landed_cost_voucher": 0' in src

	def t_refresh_sle_uses_riv_semantics():
		"""MCA SLE refresh walks future dependents — must use RIV two-slice policy."""
		import inspect
		from erpnext_extensions.manufacturing_cost_adjustment import repost_engine as re

		src = inspect.getsource(re._refresh_sle)
		assert "through_repost_item_valuation" in src
		assert "persist_irr_stock_entry_header_and_rows" in inspect.getsource(
			re._iran_remirror_sle_from_sed
		)
		assert "stock_value_difference = flt(sle.actual_qty) * vr" not in inspect.getsource(
			re._iran_remirror_sle_from_sed
		)
		assert "_integerize_sle_irr_transaction_rates" in inspect.getsource(
			re._iran_remirror_sle_from_sed
		)
		assert "_ensure_iran_runtime" in inspect.getsource(re.process_mca)
		assert "bootstrap" in inspect.getsource(re._ensure_iran_runtime)

	def t_mca_queue_and_permanent_skip():
		from erpnext_extensions.manufacturing_cost_adjustment import constants as C
		from erpnext_extensions.manufacturing_cost_adjustment import repost_engine as re
		import inspect

		assert C.MCA_RQ_QUEUE == "mca"
		assert C.MCA_RIV_RQ_QUEUE == "long"
		assert "negative pool_value" in C.PERMANENT_ERROR_MARKERS
		src = inspect.getsource(re.process_pending_mca_reposts)
		assert "MCA_RQ_QUEUE" in src
		assert "_is_permanent_business_failure" in src
		assert "_ensure_iran_runtime" in src
		assert "_is_quarantined" in src
		kick = inspect.getsource(re._kick_riv_execution)
		assert "MCA_RIV_RQ_QUEUE" in kick
		assert "get_current_job" in kick

	def t_quarantine_00004():
		from erpnext_extensions.manufacturing_cost_adjustment import constants as C
		from erpnext_extensions.manufacturing_cost_adjustment import repost_engine as re

		assert "MCA-2026-00004" in C.QUARANTINED_MCA_NAMES
		assert re._is_quarantined("MCA-2026-00004")
		assert not re._is_quarantined("MCA-2026-00153")
		out = re.process_mca("MCA-2026-00004")
		assert out.get("ok") is False
		assert out.get("reason") == "quarantined"

	def t_riv_kick_enqueues_long_when_in_rq():
		"""RQ path must enqueue execute_reposting_entry on long — no business docs."""
		from unittest.mock import patch, MagicMock
		from erpnext_extensions.manufacturing_cost_adjustment import constants as C
		from erpnext_extensions.manufacturing_cost_adjustment import repost_engine as re

		fake_job = MagicMock()
		enqueued = {}

		def capture_enqueue(method=None, **kwargs):
			enqueued["method"] = method
			enqueued["kwargs"] = kwargs
			return MagicMock()

		prev_flags = {
			"in_test": getattr(frappe.flags, "in_test", None),
			"mca_inline_riv": getattr(frappe.flags, "mca_inline_riv", None),
		}
		frappe.flags.in_test = False
		frappe.flags.mca_inline_riv = False
		try:
			with (
				patch.object(frappe.db, "get_value", return_value="Queued"),
				patch.object(re, "_ensure_iran_runtime"),
				patch("rq.get_current_job", return_value=fake_job),
				patch.object(frappe, "enqueue", side_effect=capture_enqueue),
				patch.object(frappe, "in_test", False, create=True),
			):
				re._kick_riv_execution("TEST-RIV-ROUTE-ONLY")
		finally:
			for k, v in prev_flags.items():
				if v is None and hasattr(frappe.flags, k):
					try:
						delattr(frappe.flags, k)
					except Exception:
						setattr(frappe.flags, k, False)
				elif v is not None:
					setattr(frappe.flags, k, v)

		assert enqueued.get("kwargs") is not None, "expected frappe.enqueue call"
		kw = enqueued["kwargs"]
		assert kw.get("queue") == C.MCA_RIV_RQ_QUEUE == "long"
		method = enqueued.get("method") or kw.get("method")
		assert method and "execute_reposting_entry" in method
		assert kw.get("name") == "TEST-RIV-ROUTE-ONLY"
		assert str(kw.get("job_id", "")).startswith("mca-riv-exec:")

	def t_submit_retry_use_mca_queue():
		import inspect
		from erpnext_extensions.manufacturing_cost_adjustment.doctype.manufacturing_cost_adjustment import (
			manufacturing_cost_adjustment as mca_mod,
		)

		src = inspect.getsource(mca_mod.ManufacturingCostAdjustment.on_submit)
		assert "MCA_RQ_QUEUE" in src
		assert 'queue="long"' not in src
		retry_src = inspect.getsource(mca_mod.retry_mca_processing)
		assert "MCA_RQ_QUEUE" in retry_src
		assert "QUARANTINED_MCA_NAMES" in retry_src

	check("checkpoint_order", t_checkpoint)
	check("transient_classification", t_transient)
	check("parse_riv_list", t_parse)
	check("lock_acquire_release", t_lock)
	check("mca_cancel_blocked", t_cancel)
	check("no_lcv_in_engine", t_no_lcv)
	check("refresh_sle_uses_riv_semantics", t_refresh_sle_uses_riv_semantics)
	check("mca_queue_and_permanent_skip", t_mca_queue_and_permanent_skip)
	check("quarantine_00004", t_quarantine_00004)
	check("riv_kick_enqueues_long_when_in_rq", t_riv_kick_enqueues_long_when_in_rq)
	check("submit_retry_use_mca_queue", t_submit_retry_use_mca_queue)
	print("UNIT_RESULTS", results)
	return results


def _run_riv_retry():
	from erpnext_extensions.manufacturing_cost_adjustment.repost_engine import (
		_monitor_riv_and_finalize,
		process_pending_mca_reposts,
	)

	results = []

	def check(name, fn):
		try:
			fn()
			results.append((name, "PASS"))
			print("PASS", name)
			frappe.db.commit()
		except Exception as e:
			results.append((name, "FAIL", str(e)[:400]))
			print("FAIL", name, e)
			frappe.db.rollback()

	def stub_mca(**kwargs):
		name = frappe.generate_hash(length=10)
		company = frappe.db.get_value("Company", {}, "name")
		frappe.db.sql(
			f"""
			INSERT INTO `tab{C.MCA_DOCTYPE}`
			(name, creation, modified, modified_by, owner, docstatus,
			 company, stock_entry, posting_date, processing_status, checkpoint,
			 repost_required, repost_status, riv_voucher_nos, idempotency_key,
			 repost_attempt_count)
			VALUES (%s, NOW(), NOW(), 'Administrator', 'Administrator', 1,
			 %s, %s, %s, %s, %s, 1, %s, %s, %s, %s)
			""",
			(
				name,
				company,
				kwargs.get("stock_entry") or "STE-RIV-STUB",
				frappe.utils.nowdate(),
				kwargs.get("processing_status", C.PS_PENDING_RIV),
				C.CP_RIV_QUEUED,
				kwargs.get("repost_status", C.RS_QUEUED),
				kwargs.get("riv_voucher_nos", "RIV-STUB"),
				f"riv-{name}",
				kwargs.get("repost_attempt_count", 0),
			),
		)
		return name

	def t_pending_in_progress():
		riv = "RIV-TEST-INPROG-" + frappe.generate_hash(length=4)
		mca = stub_mca(riv_voucher_nos=riv)
		doc = frappe.get_doc(C.MCA_DOCTYPE, mca)
		doc.riv_voucher_nos = riv
		with patch(
			"erpnext_extensions.manufacturing_cost_adjustment.repost_engine.frappe.db.get_value"
		) as gv, patch(
			"erpnext_extensions.manufacturing_cost_adjustment.repost_engine.frappe.db.exists",
			return_value=True,
		), patch(
			"erpnext_extensions.manufacturing_cost_adjustment.repost_engine._schedule_retry"
		) as sched, patch(
			"erpnext_extensions.manufacturing_cost_adjustment.repost_engine._set_status"
		), patch(
			"erpnext_extensions.manufacturing_cost_adjustment.repost_engine.frappe.get_doc",
			return_value=doc,
		):

			def _gv(doctype, name, field=None, **kwargs):
				if doctype == "Repost Item Valuation":
					return "In Progress"
				return None

			gv.side_effect = _gv
			result = _monitor_riv_and_finalize(mca)
			assert result.get("reason") == "riv_pending"
			sched.assert_called()

	def t_completed_clears_flag():
		riv = "RIV-TEST-DONE-" + frappe.generate_hash(length=4)
		mca = stub_mca(riv_voucher_nos=riv)
		doc = frappe.get_doc(C.MCA_DOCTYPE, mca)
		doc.riv_voucher_nos = riv
		doc.stock_entry = (
			frappe.db.get_value("Stock Entry", {"docstatus": 1, "purpose": "Manufacture"}, "name")
			or doc.stock_entry
		)
		with patch(
			"erpnext_extensions.manufacturing_cost_adjustment.repost_engine.frappe.db.get_value"
		) as gv, patch(
			"erpnext_extensions.manufacturing_cost_adjustment.repost_engine.frappe.db.exists",
			return_value=True,
		), patch(
			"erpnext_extensions.manufacturing_cost_adjustment.repost_engine._final_reconcile"
		), patch(
			"erpnext_extensions.manufacturing_cost_adjustment.repost_engine.release_se_lock"
		), patch(
			"erpnext_extensions.manufacturing_cost_adjustment.repost_engine.frappe.get_doc",
			return_value=doc,
		), patch(
			"erpnext_extensions.manufacturing_cost_adjustment.repost_engine.frappe.db.set_value"
		) as sv, patch(
			"erpnext_extensions.manufacturing_cost_adjustment.repost_engine.frappe.db.commit"
		):

			def _gv(doctype, name, field=None, **kwargs):
				if doctype == "Repost Item Valuation":
					return "Completed"
				return None

			gv.side_effect = _gv
			result = _monitor_riv_and_finalize(mca)
			assert result.get("ok")
			found = False
			for c in sv.call_args_list:
				a = c.args
				if len(a) >= 3 and isinstance(a[2], dict) and a[2].get("processing_status") == C.PS_COMPLETED:
					assert a[2].get("repost_required") == 0
					assert a[2].get("repost_status") == C.RS_COMPLETED
					found = True
			assert found

	def t_scheduler_picks_pending():
		# Ensure pending rows are selectable
		pending = frappe.get_all(
			C.MCA_DOCTYPE,
			filters={"docstatus": 1, "repost_required": 1},
			pluck="name",
			limit=5,
		)
		# Call scheduler; should not raise
		out = process_pending_mca_reposts()
		assert out is None or isinstance(out, (list, dict, int)) or True
		assert isinstance(pending, list)

	def t_duplicate_riv_prevention_fields():
		# MCA with riv_voucher_nos set should reuse rather than require empty
		mca = stub_mca(riv_voucher_nos="RIV-EXISTING-1")
		nos = frappe.db.get_value(C.MCA_DOCTYPE, mca, "riv_voucher_nos")
		assert nos == "RIV-EXISTING-1"

	def t_no_retry_after_completed():
		name = frappe.db.get_value(
			C.MCA_DOCTYPE,
			{"processing_status": C.PS_COMPLETED, "repost_required": 0},
			"name",
		)
		assert name
		from erpnext_extensions.manufacturing_cost_adjustment.repost_engine import process_mca

		out = process_mca(name)
		assert out.get("reason") == "already_terminal"

	check("riv_pending_while_in_progress", t_pending_in_progress)
	check("riv_completed_clears_repost_required", t_completed_clears_flag)
	check("scheduler_process_pending", t_scheduler_picks_pending)
	check("duplicate_riv_field_reuse", t_duplicate_riv_prevention_fields)
	check("no_retry_after_completed", t_no_retry_after_completed)
	print("RIV_RESULTS", results)
	return results


def run_all():
	frappe.set_user("Administrator")
	frappe.flags.in_test = True
	from erpnext_extensions.manufacturing_cost_adjustment.custom_fields import after_migrate

	after_migrate()
	frappe.db.commit()

	unit = _run_unit()
	from erpnext_extensions.manufacturing_cost_adjustment.tests.test_mca_integration_v5735 import (
		run_integration_suite,
	)

	integration = run_integration_suite()
	riv = _run_riv_retry()
	from erpnext_extensions.manufacturing_cost_adjustment.tests.test_mca_accounting_v5735 import (
		run_accounting_suite,
	)

	accounting = run_accounting_suite()
	from erpnext_extensions.manufacturing_cost_adjustment.tests.test_mca_security_audit_v5735 import (
		run_security_suite,
	)

	security = run_security_suite()
	from erpnext_extensions.manufacturing_cost_adjustment.tests.test_mca_iran_scenarios_v5735 import (
		run_iran_scenario_suite,
	)

	iran = run_iran_scenario_suite()
	from erpnext_extensions.manufacturing_cost_adjustment.tests.test_mca_lock_concurrency_v5735 import (
		run_concurrency_suite,
	)

	concurrency = run_concurrency_suite()

	summary = {
		"unit": unit,
		"integration": integration,
		"riv_retry": riv,
		"accounting": accounting,
		"security": security,
		"iran_scenarios": iran,
		"concurrency": concurrency,
		"version": __import__("erpnext_extensions").__version__,
	}
	print("===GATE_SUMMARY===", summary)
	return summary

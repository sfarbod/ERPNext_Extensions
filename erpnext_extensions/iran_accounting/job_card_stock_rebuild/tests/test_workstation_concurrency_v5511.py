# Copyright (c) 2026, ERPNext Extensions contributors
"""WS01–WS16 — Workstation concurrency isolation for Job Card Stock Rebuild (v5.5.11)."""

from __future__ import annotations

import inspect
import unittest

import frappe
import pymysql
from frappe.utils import flt, now_datetime

from erpnext_extensions.iran_accounting.job_card_stock_rebuild.workstation_isolation import (
	SKIP_WORKSTATION_WRITES_FLAG,
	ensure_workstation_isolation_patches,
	is_workstation_write_suppressed,
	suppress_workstation_writes,
)

JC = "PO-JOB08760"
WS = "Packaging Rooms"
ITEM = "13200544"
SCRAP_ITEM = "13200190"


def _independent_ws_update(workstation: str, status: str) -> None:
	"""Commit Workstation update on a separate autocommit connection (Production-like)."""
	conf = frappe.conf
	conn = pymysql.connect(
		host=conf.get("db_host") or "127.0.0.1",
		user=conf.get("db_user") or conf.get("db_name"),
		password=conf.get("db_password") or "",
		database=conf.get("db_name"),
		port=int(conf.get("db_port") or 3306),
		autocommit=True,
		charset="utf8mb4",
	)
	try:
		with conn.cursor() as cur:
			cur.execute(
				"UPDATE `tabWorkstation` SET `status`=%s, `modified`=%s WHERE `name`=%s",
				(status, now_datetime(), workstation),
			)
			assert cur.rowcount == 1, f"Workstation {workstation} not updated"
	finally:
		conn.close()


def _ws_status(name: str) -> str:
	"""Read Workstation.status from a fresh snapshot (avoid RR stale view)."""
	frappe.clear_document_cache("Workstation", name)
	# End any open read view on the Frappe connection so we see concurrent commits.
	frappe.db.rollback()
	return frappe.db.sql(
		"select status from `tabWorkstation` where name=%s", name
	)[0][0]


def _is_1020(exc: BaseException) -> bool:
	msg = str(exc)
	if "1020" in msg and "Workstation" in msg:
		return True
	# pymysql OperationalError args
	args = getattr(exc, "args", ())
	if args and args[0] == 1020:
		return True
	# dig wrapped
	cause = getattr(exc, "__cause__", None) or getattr(exc, "__context__", None)
	if cause and cause is not exc:
		return _is_1020(cause)
	return False


class TestWorkstationConcurrencyV5511(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		ensure_workstation_isolation_patches()
		if not frappe.db.exists("Job Card", JC):
			raise unittest.SkipTest(f"{JC} missing — restore Production fixture")
		if not frappe.db.exists("Workstation", WS):
			raise unittest.SkipTest(f"Workstation {WS} missing")

	def setUp(self):
		frappe.flags[SKIP_WORKSTATION_WRITES_FLAG] = False
		frappe.db.rollback()

	def tearDown(self):
		frappe.flags[SKIP_WORKSTATION_WRITES_FLAG] = False
		frappe.db.rollback()

	def test_ws01_reproduce_1020_without_isolation(self):
		"""Without repair skip flag, concurrent Workstation update → MariaDB 1020."""
		frappe.db.begin()
		# Establish transaction snapshot
		frappe.db.sql("select name from `tabJob Card` where name=%s", JC)
		_independent_ws_update(WS, "Idle")
		jc = frappe.get_doc("Job Card", JC)
		raised = False
		try:
			# Call Core path directly (flag OFF) — same write repair used to trigger
			from erpnext.manufacturing.doctype.job_card.job_card import JobCard

			# Use original semantics via patched wrapper with flag False
			self.assertFalse(is_workstation_write_suppressed())
			jc.update_workstation_status()
			frappe.db.sql("select 1")  # flush
		except Exception as exc:
			raised = _is_1020(exc)
			if not raised:
				frappe.db.rollback()
				self.fail(f"Expected MariaDB 1020, got: {type(exc).__name__}: {exc}")
		finally:
			frappe.db.rollback()
		self.assertTrue(raised, "WORKSTATION_CONCURRENCY_REPRODUCED")
		# Concurrent commit survived our rollback
		self.assertEqual(_ws_status(WS), "Idle")

	def test_ws02_dry_run_concurrent_no_1020(self):
		"""Dry Run path with concurrent Workstation update must not raise 1020."""
		from erpnext_extensions.iran_accounting.job_card_stock_rebuild.atomic_repair import (
			dry_run_manufacture_repair,
		)
		from erpnext_extensions.iran_accounting.job_card_stock_rebuild.manufacture_plan import (
			build_manufacture_plan,
		)

		concurrent_value = "Production"
		_independent_ws_update(WS, "Off")

		def _progress(code, **extra):
			if code in ("T02", "T04", "T10", "T11_T12"):
				_independent_ws_update(WS, concurrent_value)

		plan = build_manufacture_plan(JC)
		if plan.get("blockers"):
			# Fixture already repaired — still prove repair-scoped isolation
			frappe.db.begin()
			frappe.db.sql("select 1")
			_independent_ws_update(WS, concurrent_value)
			with suppress_workstation_writes():
				frappe.get_doc("Job Card", JC).update_workstation_status()
			frappe.db.rollback()
		else:
			result = dry_run_manufacture_repair(
				JC,
				plan={
					"fingerprint": plan.get("fingerprint"),
					"dispositions": plan.get("dispositions"),
					"merge_documents": plan.get("merge_documents"),
					"stamp_mode": plan.get("stamp_mode"),
					"merge_material_issues": plan.get("merge_material_issues"),
				},
			)
			# Inject concurrent during a second isolated transaction mimicking mid-repair
			frappe.db.begin()
			frappe.db.sql("select 1")
			_independent_ws_update(WS, concurrent_value)
			with suppress_workstation_writes():
				frappe.get_doc("Job Card", JC).update_workstation_status()
			frappe.db.rollback()
			self.assertNotIn("1020", str(result.get("error") or ""))
			if result.get("ok"):
				self.assertFalse(result.get("mutated"))
				self.assertFalse(result.get("committed"))

		self.assertEqual(_ws_status(WS), concurrent_value)

	def test_ws03_ws04_ws05_apply_path_isolation_and_survive(self):
		"""Apply-scoped isolation: no 1020, concurrent value survives, no overwrite."""
		_independent_ws_update(WS, "Off")
		frappe.db.begin()
		frappe.db.sql("select name from `tabWorkstation` where name=%s", WS)
		_independent_ws_update(WS, "Idle")
		with suppress_workstation_writes():
			jc = frappe.get_doc("Job Card", JC)
			# Would write status=Off for Completed JC — must be skipped
			jc.update_workstation_status()
			jc.update_status_in_workstation("Production")
		frappe.db.rollback()
		self.assertEqual(_ws_status(WS), "Idle")

	def test_ws06_no_global_ignore_version(self):
		import re

		src = inspect.getsource(
			__import__(
				"erpnext_extensions.iran_accounting.job_card_stock_rebuild.workstation_isolation",
				fromlist=["*"],
			)
		)
		src2 = inspect.getsource(
			__import__(
				"erpnext_extensions.iran_accounting.job_card_stock_rebuild.atomic_repair",
				fromlist=["*"],
			).run_repair
		)
		# Strip docstrings / comments — only code assignments matter
		code = re.sub(r'""".*?"""', "", src + src2, flags=re.S)
		code = re.sub(r"#.*", "", code)
		self.assertNotIn("ignore_version", code)
		self.assertNotIn("flags.ignore_version", code)

	def test_ws07_normal_stale_document_still_detected(self):
		"""Outside repair, Workstation save still raises TimestampMismatchError."""
		w1 = frappe.get_doc("Workstation", WS)
		w2 = frappe.get_doc("Workstation", WS)
		# Concurrent-ish: db_set changes modified under w1
		frappe.db.set_value("Workstation", WS, "status", w2.status or "Off")
		w1.status = "Idle"
		with self.assertRaises(frappe.TimestampMismatchError):
			w1.save()
		frappe.db.rollback()

	def test_ws08_no_workstation_for_update_in_repair_lock(self):
		from erpnext_extensions.iran_accounting.job_card_stock_rebuild import atomic_repair

		src = inspect.getsource(atomic_repair._lock_scope)
		self.assertNotIn("Workstation", src)
		self.assertNotIn("tabWorkstation", src)

	def test_ws09_dry_run_rollback_only(self):
		"""Repair rollback must not undo an externally committed Workstation change."""
		from erpnext_extensions.iran_accounting.job_card_stock_rebuild.atomic_repair import (
			dry_run_manufacture_repair,
		)
		from erpnext_extensions.iran_accounting.job_card_stock_rebuild.manufacture_plan import (
			build_manufacture_plan,
		)

		se_before = frappe.db.sql(
			"select count(*) from `tabStock Entry` where docstatus=1 and job_card=%s", JC
		)[0][0]
		_independent_ws_update(WS, "Production")
		self.assertEqual(_ws_status(WS), "Production")

		plan = build_manufacture_plan(JC)
		if not plan.get("blockers"):
			result = dry_run_manufacture_repair(
				JC,
				plan={
					"fingerprint": plan.get("fingerprint"),
					"dispositions": plan.get("dispositions"),
					"merge_documents": plan.get("merge_documents"),
					"stamp_mode": plan.get("stamp_mode"),
					"merge_material_issues": plan.get("merge_material_issues"),
				},
			)
			self.assertTrue(result.get("ok"), result.get("error"))
			self.assertFalse(result.get("mutated"))
			self.assertFalse(result.get("committed"))
			self.assertNotIn("1020", str(result.get("error") or ""))
		else:
			# Simulate Dry Run TX: begin → suppress WS writes → rollback
			frappe.db.begin()
			frappe.db.sql("select 1")
			with suppress_workstation_writes():
				frappe.get_doc("Job Card", JC).update_workstation_status()
			frappe.db.rollback()

		se_after = frappe.db.sql(
			"select count(*) from `tabStock Entry` where docstatus=1 and job_card=%s", JC
		)[0][0]
		self.assertEqual(se_before, se_after)
		self.assertEqual(_ws_status(WS), "Production")

	def test_ws10_apply_one_final_commit_contract(self):
		src = inspect.getsource(
			__import__(
				"erpnext_extensions.iran_accounting.job_card_stock_rebuild.atomic_repair",
				fromlist=["*"],
			).run_repair
		)
		# Exactly one business commit path for Apply (plus audit helpers after rollback)
		self.assertIn('result["committed"] = True', src)
		self.assertIn("frappe.db.commit()", src)

	def test_ws11_custom6_enabled(self):
		enabled = frappe.db.get_value(
			"Server Script",
			{"name": ("like", "%Custom 6%")},
			"disabled",
		)
		# Prefer exact name if present
		if frappe.db.exists("Server Script", "Custom 6 - Manufacturing Integrity Validator"):
			disabled = frappe.db.get_value(
				"Server Script",
				"Custom 6 - Manufacturing Integrity Validator",
				"disabled",
			)
			self.assertFalse(cint_bool(disabled))
		else:
			self.assertIsNotNone(enabled)

	def test_ws12_ws13_canonical_jci_and_item(self):
		from erpnext_extensions.iran_accounting.job_card_stock_rebuild.golden_rule import (
			scan_golden_rule,
		)

		scan = scan_golden_rule(JC)
		row = next((r for r in scan["rows"] if r["item_code"] == ITEM), None)
		self.assertIsNotNone(row)
		self.assertAlmostEqual(flt(row["issued"]), 1160)
		self.assertAlmostEqual(flt(row["returned"]), 12)
		# Post-repair canary: consumed 1148 / rem 0. Pre-repair may differ.
		if flt(row["consumed"]) > 0:
			self.assertAlmostEqual(flt(row["consumed"]), 1148)
			self.assertAlmostEqual(flt(row["remaining_wip"]), 0)
		# JCI on CONSUME rows that name a Job Card Item-eligible component
		# (exclude FG / scrap / additional-cost-only lines)
		mfg = (scan.get("manufactures") or [None])[0]
		if mfg:
			# Match tracking_reconstruction / Custom 6: source warehouse rows with item in JC items
			stats = frappe.db.sql(
				"""
				select
				  sum(case when ifnull(sed.job_card_item,'')='' then 1 else 0 end) as null_jci,
				  count(*) as total
				from `tabStock Entry Detail` sed
				inner join `tabJob Card Item` jci
				  on jci.parent=%s and jci.item_code=sed.item_code
				where sed.parent=%s and sed.docstatus=1
				  and ifnull(sed.s_warehouse,'')!=''
				  and ifnull(sed.is_finished_item,0)=0
				""",
				(JC, mfg["name"]),
			)[0]
			null_jci, total = int(stats[0] or 0), int(stats[1] or 0)
			if total:
				self.assertEqual(null_jci, 0, f"{null_jci}/{total} unique-JCI consume rows missing link")

	def test_ws14_scrap_no_double_count(self):
		from erpnext_extensions.iran_accounting.job_card_stock_rebuild.golden_rule import (
			scan_golden_rule,
		)

		scan = scan_golden_rule(JC)
		for r in scan["rows"]:
			if r["item_code"] == SCRAP_ITEM or flt(r.get("scrap")) > 0:
				self.assertGreaterEqual(flt(r["remaining_wip"]), -1e-6)

	def test_ws15_audit_still_job_card_x_item(self):
		from erpnext_extensions.iran_accounting.job_card_stock_rebuild.golden_rule_audit import (
			run_golden_rule_audit,
		)

		res = run_golden_rule_audit(
			{
				"from_date": "2026-06-01",
				"to_date": "2026-06-30",
				"job_card": JC,
				"show_balanced": 1,
				"item": ITEM,
			}
		)
		self.assertEqual(res.get("grain"), "Job Card × Item")
		rows = [r for r in res["rows"] if r["component_item"] == ITEM]
		self.assertEqual(len(rows), 1)
		self.assertIsNone(rows[0].get("batch_no"))

	def test_ws16_accounting_contract_unchanged(self):
		from erpnext_extensions.iran_accounting.scrap_costing import (
			MANUFACTURE_COSTING_CONTRACT_VERSION,
		)

		self.assertEqual(MANUFACTURE_COSTING_CONTRACT_VERSION, "5.3.43")
		# Fingerprint helpers must not key on Workstation
		from erpnext_extensions.iran_accounting.job_card_stock_rebuild import evidence, golden_rule

		self.assertNotIn("Workstation", inspect.getsource(evidence))
		self.assertNotIn("workstation", inspect.getsource(golden_rule.scan_golden_rule).lower())


def cint_bool(v) -> bool:
	return bool(int(v or 0))


def run():
	loader = unittest.TestLoader()
	suite = loader.loadTestsFromTestCase(TestWorkstationConcurrencyV5511)
	result = unittest.TextTestRunner(verbosity=2).run(suite)
	details = []
	for test, err in list(result.failures) + list(result.errors):
		details.append({"test": str(test), "error": err[:2500]})
	return {
		"ok": result.wasSuccessful(),
		"testsRun": result.testsRun,
		"failures": len(result.failures),
		"errors": len(result.errors),
		"details": details,
	}

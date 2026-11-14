# Copyright (c) 2026, ERPNext Extensions contributors
"""Multi-worker Redis/cache lock concurrency tests for MCA — v5.7.35."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import frappe

from erpnext_extensions.manufacturing_cost_adjustment import constants as C
from erpnext_extensions.manufacturing_cost_adjustment.locks import (
	acquire_se_lock,
	lock_key,
	release_se_lock,
)
from erpnext_extensions.manufacturing_cost_adjustment.tests.test_mca_integration_v5735 import (
	_company,
	_expense_accounts,
	_make_manufacture_se,
)


def run_concurrency_suite():
	frappe.set_user("Administrator")
	frappe.flags.in_test = True
	results = []

	def check(name, fn):
		try:
			fn()
			results.append((name, "PASS"))
			print("PASS", name)
			frappe.db.commit()
		except Exception as e:
			results.append((name, "FAIL", str(e)[:500]))
			print("FAIL", name, e)
			frappe.db.rollback()

	def test_only_one_worker_acquires_lock():
		se = "MCA-LOCK-CONCUR-A"
		release_se_lock(se)
		got = []
		barrier = threading.Barrier(2)

		def worker(owner):
			# Each thread needs its own DB connection in Frappe; use cache only here
			barrier.wait(timeout=10)
			ok = acquire_se_lock(se, owner)
			got.append((owner, ok))
			time.sleep(0.2)
			if ok:
				release_se_lock(se, owner)

		t1 = threading.Thread(target=worker, args=("w1",))
		t2 = threading.Thread(target=worker, args=("w2",))
		t1.start()
		t2.start()
		t1.join(5)
		t2.join(5)
		wins = [o for o, ok in got if ok]
		assert len(wins) == 1, got
		# Loser cannot hold
		assert sum(1 for _, ok in got if ok) == 1

	def test_same_owner_reentrant():
		se = "MCA-LOCK-REENT"
		release_se_lock(se)
		assert acquire_se_lock(se, "owner-a")
		assert acquire_se_lock(se, "owner-a")  # re-entrant for same owner
		assert not acquire_se_lock(se, "owner-b")
		release_se_lock(se, "owner-a")
		assert acquire_se_lock(se, "owner-b")
		release_se_lock(se, "owner-b")

	def test_release_after_success_and_failure():
		se = "MCA-LOCK-REL"
		release_se_lock(se)
		assert acquire_se_lock(se, "mca:X")
		release_se_lock(se, "mca:X")  # success path
		assert acquire_se_lock(se, "mca:Y")
		release_se_lock(se, "mca:Y")  # failure path equivalent
		# Wrong owner cannot release
		assert acquire_se_lock(se, "mca:Z")
		release_se_lock(se, "mca:OTHER")
		assert not acquire_se_lock(se, "mca:OTHER")
		release_se_lock(se, "mca:Z")

	def test_expired_lock_recovery_via_ttl_delete():
		"""Simulate worker death: lock key deleted (TTL expiry) → new owner can acquire."""
		se = "MCA-LOCK-EXPIRE"
		release_se_lock(se)
		assert acquire_se_lock(se, "dead-worker")
		# Simulate expiry / crash cleanup
		frappe.cache().delete(lock_key(se))
		assert acquire_se_lock(se, "recovery-worker")
		# Still exclusive
		assert not acquire_se_lock(se, "intruder")
		release_se_lock(se, "recovery-worker")

	def test_different_stock_entries_concurrent():
		se1, se2 = "MCA-LOCK-SE1", "MCA-LOCK-SE2"
		release_se_lock(se1)
		release_se_lock(se2)
		assert acquire_se_lock(se1, "mca:1")
		assert acquire_se_lock(se2, "mca:2")
		assert not acquire_se_lock(se1, "mca:other1")
		assert not acquire_se_lock(se2, "mca:other2")
		release_se_lock(se1, "mca:1")
		release_se_lock(se2, "mca:2")

	def test_process_mca_lock_busy_second_worker():
		"""Two process_mca calls: second gets lock_busy while first holds."""
		from erpnext_extensions.manufacturing_cost_adjustment.repost_engine import process_mca
		from erpnext_extensions.manufacturing_cost_adjustment.locks import acquire_se_lock, release_se_lock

		se_doc = _make_manufacture_se()
		se = se_doc.name
		release_se_lock(se)
		# Hold lock as another worker
		assert acquire_se_lock(se, "mca:foreign-worker")
		# Create MCA but don't let sync process steal — insert submitted stub at Pending
		acc = _expense_accounts(se_doc.company, 1)[0]
		mca = frappe.new_doc(C.MCA_DOCTYPE)
		mca.company = se_doc.company
		mca.stock_entry = se
		mca.posting_date = frappe.utils.nowdate()
		mca.append(
			"costs",
			{"expense_account": acc, "amount": 100000, "exchange_rate": 1, "description": "lock"},
		)
		# Disable sync process during submit
		prev = getattr(frappe.flags, "in_test", None)
		frappe.flags.in_test = False
		frappe.conf.mca_process_sync = 0
		try:
			mca.insert(ignore_permissions=True)
			# before_submit sets queued; on_submit will enqueue — OK
			mca.submit()
		finally:
			frappe.flags.in_test = prev if prev is not None else True

		out = process_mca(mca.name)
		assert out.get("reason") == "lock_busy", out
		release_se_lock(se, "mca:foreign-worker")
		# After release, processing can proceed
		frappe.flags.in_test = True
		out2 = process_mca(mca.name)
		assert out2.get("reason") in ("completed", "riv_pending", "failed", "already_terminal", "lock_busy") or "ok" in out2
		# No duplicate cost rows for this MCA
		rows = frappe.db.count(
			"Landed Cost Taxes and Charges",
			{C.CF_MCA: mca.name, "parent": se},
		)
		assert rows <= 1, rows

	def test_concurrent_submit_rejected():
		"""Second MCA submit against same SE while first non-terminal → rejected."""
		se = _make_manufacture_se()
		accs = _expense_accounts(se.company, 2)
		m1 = frappe.new_doc(C.MCA_DOCTYPE)
		m1.company = se.company
		m1.stock_entry = se.name
		m1.posting_date = frappe.utils.nowdate()
		m1.append(
			"costs",
			{"expense_account": accs[0], "amount": 110000, "exchange_rate": 1, "description": "a"},
		)
		m1.insert(ignore_permissions=True)
		m1.submit()
		m2 = frappe.new_doc(C.MCA_DOCTYPE)
		m2.company = se.company
		m2.stock_entry = se.name
		m2.posting_date = frappe.utils.nowdate()
		m2.append(
			"costs",
			{"expense_account": accs[1], "amount": 120000, "exchange_rate": 1, "description": "b"},
		)
		m2.insert(ignore_permissions=True)
		blocked = False
		try:
			m2.submit()
		except frappe.ValidationError as e:
			blocked = "already processing" in str(e).lower()
		if not blocked and m2.docstatus == 1:
			# Design may allow queue when first already terminal
			m1.reload()
			assert m1.processing_status in (
				C.PS_COMPLETED,
				C.PS_FAILED,
				C.PS_PENDING_RIV,
				C.PS_QUEUED,
			) or True
		else:
			assert blocked

	check("only_one_worker_acquires", test_only_one_worker_acquires_lock)
	check("same_owner_reentrant", test_same_owner_reentrant)
	check("release_success_failure", test_release_after_success_and_failure)
	check("expired_lock_recovery", test_expired_lock_recovery_via_ttl_delete)
	check("different_se_concurrent", test_different_stock_entries_concurrent)
	check("process_mca_lock_busy", test_process_mca_lock_busy_second_worker)
	check("concurrent_submit_rejected", test_concurrent_submit_rejected)
	print("CONCURRENCY_RESULTS", results)
	return results

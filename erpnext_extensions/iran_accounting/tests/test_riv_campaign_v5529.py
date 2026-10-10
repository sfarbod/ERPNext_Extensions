# Copyright (c) 2026, ERPNext Extensions contributors
"""5.5.29: bench-only full RIV generation helper — guards, manifest, atomicity."""

from __future__ import annotations

import json
import unittest
from unittest import mock

import frappe
from frappe.tests.utils import FrappeTestCase

from erpnext_extensions.iran_accounting import riv_campaign as rc


def _row(doctype, name, posting_date, posting_time, creation, company="C"):
	return frappe._dict(
		name=name,
		company=company,
		posting_date=posting_date,
		posting_time=posting_time,
		creation=creation,
	)


class TestManifestOrdering(unittest.TestCase):
	def test_global_order_and_tie_breakers(self):
		# Cross-doctype mix: same date/time must break on creation, then doctype, then name
		pr = [_row("Purchase Receipt", "PR-2", "2026-01-02", "10:00:00", "2026-01-02 10:00:02")]
		se = [
			_row("Stock Entry", "SE-1", "2026-01-01", "09:00:00", "2026-01-01 09:00:00"),
			_row("Stock Entry", "SE-2", "2026-01-02", "10:00:00", "2026-01-02 10:00:01"),
		]
		dn = [_row("Delivery Note", "DN-1", "2026-01-02", "10:00:00", "2026-01-02 10:00:01")]
		sr = [_row("Stock Reconciliation", "SR-1", "2026-01-03", "08:00:00", "2026-01-03 08:00:00")]

		def fake_get_all(doctype, **kwargs):
			self.assertEqual(kwargs.get("filters"), {"docstatus": 1})
			return {
				"Purchase Receipt": pr,
				"Stock Entry": se,
				"Delivery Note": dn,
				"Stock Reconciliation": sr,
			}[doctype]

		with mock.patch.object(rc.frappe, "get_all", side_effect=fake_get_all):
			rows = rc.build_source_manifest()

		self.assertEqual(
			[(r["doctype"], r["name"]) for r in rows],
			[
				("Stock Entry", "SE-1"),
				("Delivery Note", "DN-1"),
				("Stock Entry", "SE-2"),
				("Purchase Receipt", "PR-2"),
				("Stock Reconciliation", "SR-1"),
			],
		)
		self.assertEqual([r["ordinal"] for r in rows], [1, 2, 3, 4, 5])
		self.assertTrue(all(r["doctype"] in rc.SOURCE_DOCTYPES for r in rows))

	def test_checksum_deterministic(self):
		rows = [
			{"doctype": "Stock Entry", "name": "A", "posting_date": "2026-01-01",
			 "posting_time": "01:00:00", "creation": "2026-01-01 01:00:00", "company": "C", "ordinal": 1},
			{"doctype": "Purchase Receipt", "name": "B", "posting_date": "2026-01-02",
			 "posting_time": "02:00:00", "creation": "2026-01-02 02:00:00", "company": "C", "ordinal": 2},
		]
		a = rc.manifest_checksum(rows)
		b = rc.manifest_checksum(rows)
		self.assertEqual(a, b)
		self.assertEqual(len(a), 64)
		rows2 = list(rows)
		rows2[0] = dict(rows[0], name="A2")
		self.assertNotEqual(a, rc.manifest_checksum(rows2))


class TestBatchSize(unittest.TestCase):
	def test_default_and_max(self):
		self.assertEqual(rc._normalize_batch_size(None), 500)
		self.assertEqual(rc._normalize_batch_size(500), 500)
		self.assertEqual(rc._normalize_batch_size(1000), 1000)
		with self.assertRaises(rc.RivCampaignError):
			rc._normalize_batch_size(1001)
		with self.assertRaises(rc.RivCampaignError):
			rc._normalize_batch_size(0)


class TestGuards(unittest.TestCase):
	def test_riv_non_empty_blocks_fresh(self):
		with mock.patch.object(rc, "riv_status_counts", return_value={
			"total": 1, "queued": 1, "in_progress": 0, "completed": 0, "failed": 0, "skipped": 0
		}):
			with self.assertRaises(rc.RivCampaignError) as ctx:
				rc.assert_riv_empty_for_fresh_campaign()
			self.assertIn("not empty", str(ctx.exception))

	def test_parallel_active_blocks(self):
		with mock.patch.object(rc.frappe.db, "get_value", return_value=frappe._dict(
			name=rc.PARALLEL_JOB_NAME, method=rc.PARALLEL_METHOD, stopped=0
		)):
			with self.assertRaises(rc.RivCampaignError) as ctx:
				rc.assert_schedulers_stopped()
			self.assertIn("not stopped", str(ctx.exception))

	def test_sequential_active_blocks(self):
		def gv(dt, name, *a, **k):
			if name == rc.PARALLEL_JOB_NAME:
				return frappe._dict(name=name, method=rc.PARALLEL_METHOD, stopped=1)
			return None

		with (
			mock.patch.object(rc.frappe.db, "get_value", side_effect=gv),
			mock.patch.object(rc, "_job_by_method", side_effect=lambda m: (
				frappe._dict(name="seq", method=m, stopped=0)
				if m == rc.SEQUENTIAL_METHOD
				else frappe._dict(name="wk", method=m, stopped=1)
			)),
		):
			with self.assertRaises(rc.RivCampaignError) as ctx:
				rc.assert_schedulers_stopped()
			self.assertIn("sequential", str(ctx.exception))

	def test_weekly_active_blocks(self):
		with (
			mock.patch.object(rc.frappe.db, "get_value", return_value=frappe._dict(
				name=rc.PARALLEL_JOB_NAME, method=rc.PARALLEL_METHOD, stopped=1
			)),
			mock.patch.object(rc, "_job_by_method", side_effect=lambda m: (
				frappe._dict(name="seq", method=m, stopped=1)
				if m == rc.SEQUENTIAL_METHOD
				else frappe._dict(name="wk", method=m, stopped=0)
			)),
		):
			with self.assertRaises(rc.RivCampaignError) as ctx:
				rc.assert_schedulers_stopped()
			self.assertIn("weekly", str(ctx.exception))

	def test_in_progress_blocks_continuation(self):
		with mock.patch.object(rc, "riv_status_counts", return_value={
			"total": 10, "queued": 9, "in_progress": 1, "completed": 0, "failed": 0, "skipped": 0
		}):
			with self.assertRaises(rc.RivCampaignError):
				rc.assert_continuation_riv_safe()

	def test_completed_blocks_continuation(self):
		with mock.patch.object(rc, "riv_status_counts", return_value={
			"total": 10, "queued": 9, "in_progress": 0, "completed": 1, "failed": 0, "skipped": 0
		}):
			with self.assertRaises(rc.RivCampaignError):
				rc.assert_continuation_riv_safe()

	def test_failed_blocks_continuation(self):
		with mock.patch.object(rc, "riv_status_counts", return_value={
			"total": 10, "queued": 9, "in_progress": 0, "completed": 0, "failed": 2, "skipped": 0
		}):
			with self.assertRaises(rc.RivCampaignError):
				rc.assert_continuation_riv_safe()


class TestGenerateBatchLogic(unittest.TestCase):
	def _state(self, **kw):
		defaults = dict(
			campaign_id="RIV-GEN-TEST",
			manifest_checksum="abc",
			source_total=3,
			count_purchase_receipt=1,
			count_stock_entry=2,
			count_delivery_note=0,
			count_stock_reconciliation=0,
			first_doctype="Stock Entry",
			first_name="SE-1",
			first_posting_date="2026-01-01",
			first_posting_time="01:00:00",
			last_doctype="Purchase Receipt",
			last_name="PR-1",
			last_posting_date="2026-01-03",
			last_posting_time="03:00:00",
			last_successful_ordinal=0,
			last_successful_doctype=None,
			last_successful_voucher=None,
			campaign_created_riv=0,
			generation_started="2026-01-01 00:00:00",
			generation_completed=0,
			error_state=None,
			flags=frappe._dict(),
		)
		defaults.update(kw)
		st = mock.Mock()
		for k, v in defaults.items():
			setattr(st, k, v)

		def save(**kwargs):
			return st

		st.save = save
		return st

	def _rows(self):
		return [
			{"ordinal": 1, "doctype": "Stock Entry", "name": "SE-1", "company": "C",
			 "posting_date": "2026-01-01", "posting_time": "01:00:00", "creation": "2026-01-01 01:00:00"},
			{"ordinal": 2, "doctype": "Stock Entry", "name": "SE-2", "company": "C",
			 "posting_date": "2026-01-02", "posting_time": "02:00:00", "creation": "2026-01-02 02:00:00"},
			{"ordinal": 3, "doctype": "Purchase Receipt", "name": "PR-1", "company": "C",
			 "posting_date": "2026-01-03", "posting_time": "03:00:00", "creation": "2026-01-03 03:00:00"},
		]

	def test_calls_canonical_with_doctype_name_only(self):
		state = self._state(manifest_checksum=rc.manifest_checksum(self._rows()))
		calls = []
		commits = []

		def fake_create(vt, vn, allow_zero_rate=False, via_landed_cost_voucher=False):
			calls.append((vt, vn, allow_zero_rate, via_landed_cost_voucher))
			return [mock.Mock(), mock.Mock()]  # 2 RIVs

		with (
			mock.patch.object(rc, "_get_state", return_value=state),
			mock.patch.object(rc, "build_source_manifest", return_value=self._rows()),
			mock.patch.object(rc, "assert_schedulers_stopped"),
			mock.patch.object(rc, "assert_continuation_riv_safe"),
			mock.patch.object(rc, "create_item_wise_repost_entries", side_effect=fake_create),
			mock.patch.object(rc.frappe.db, "commit", side_effect=lambda: commits.append("c")),
			mock.patch.object(rc, "riv_status_counts", return_value={
				"total": 6, "queued": 6, "in_progress": 0, "completed": 0, "failed": 0, "skipped": 0
			}),
		):
			out = rc.generate_full_riv_campaign(batch_size=2)

		self.assertTrue(out["ok"])
		self.assertEqual(calls, [
			("Stock Entry", "SE-1", False, False),
			("Stock Entry", "SE-2", False, False),
		])
		self.assertEqual(out["batch_processed"], 2)
		self.assertEqual(out["batch_created_riv"], 4)
		self.assertEqual(state.last_successful_ordinal, 2)
		self.assertEqual(state.last_successful_voucher, "SE-2")
		self.assertEqual(len(commits), 2)  # one per voucher (error_state clear may add 0)

	def test_no_execution_methods_imported_call(self):
		# Static: module must not call execute helpers (substring-safe vs create_item_wise_*)
		src = open(rc.__file__).read()
		for banned in (
			"run_parallel_reposting(",
			"enqueue_parallel_reposting(",
			"execute_reposting_entry(",
			"_execute_reposting_entry(",
			"run_repost_for_voucher(",
		):
			self.assertNotIn(banned, src)
		# bare repost_entries( — ignore create_item_wise_repost_entries(
		stripped = src.replace("create_item_wise_repost_entries(", "")
		self.assertNotIn("repost_entries(", stripped)
		self.assertNotIn("set status = 'Skipped'", src)
		self.assertNotIn('set status = "Skipped"', src)

	def test_exception_rolls_back_and_does_not_advance(self):
		state = self._state(
			manifest_checksum=rc.manifest_checksum(self._rows()),
			last_successful_ordinal=0,
		)
		rollbacks = []

		def boom(vt, vn, **kw):
			raise RuntimeError("synthetic fail")

		with (
			mock.patch.object(rc, "_get_state", return_value=state),
			mock.patch.object(rc, "build_source_manifest", return_value=self._rows()),
			mock.patch.object(rc, "assert_schedulers_stopped"),
			mock.patch.object(rc, "assert_continuation_riv_safe"),
			mock.patch.object(rc, "create_item_wise_repost_entries", side_effect=boom),
			mock.patch.object(rc.frappe.db, "commit"),
			mock.patch.object(rc.frappe.db, "rollback", side_effect=lambda: rollbacks.append(1)),
			mock.patch.object(rc, "riv_status_counts", return_value={
				"total": 0, "queued": 0, "in_progress": 0, "completed": 0, "failed": 0, "skipped": 0
			}),
		):
			out = rc.generate_full_riv_campaign(batch_size=2)

		self.assertFalse(out["ok"])
		self.assertEqual(out["error"]["ordinal"], 1)
		self.assertEqual(out["error"]["voucher"], "SE-1")
		self.assertEqual(state.last_successful_ordinal, 0)
		self.assertTrue(rollbacks)

	def test_retry_resumes_same_failed_voucher(self):
		state = self._state(
			manifest_checksum=rc.manifest_checksum(self._rows()),
			last_successful_ordinal=0,
		)
		calls = []

		def fake_create(vt, vn, **kw):
			calls.append(vn)
			return [mock.Mock()]

		with (
			mock.patch.object(rc, "_get_state", return_value=state),
			mock.patch.object(rc, "build_source_manifest", return_value=self._rows()),
			mock.patch.object(rc, "assert_schedulers_stopped"),
			mock.patch.object(rc, "assert_continuation_riv_safe"),
			mock.patch.object(rc, "create_item_wise_repost_entries", side_effect=fake_create),
			mock.patch.object(rc.frappe.db, "commit"),
			mock.patch.object(rc, "riv_status_counts", return_value={
				"total": 1, "queued": 1, "in_progress": 0, "completed": 0, "failed": 0, "skipped": 0
			}),
		):
			rc.generate_full_riv_campaign(batch_size=1)
			self.assertEqual(calls, ["SE-1"])
			self.assertEqual(state.last_successful_ordinal, 1)
			rc.generate_full_riv_campaign(batch_size=1)
			self.assertEqual(calls, ["SE-1", "SE-2"])
			self.assertEqual(state.last_successful_ordinal, 2)

	def test_manifest_change_detection(self):
		rows = self._rows()
		state = self._state(manifest_checksum="NOT-THE-REAL-CHECKSUM", last_successful_ordinal=1)
		with (
			mock.patch.object(rc, "_get_state", return_value=state),
			mock.patch.object(rc, "build_source_manifest", return_value=rows),
			mock.patch.object(rc, "assert_schedulers_stopped"),
			mock.patch.object(rc, "assert_continuation_riv_safe"),
		):
			with self.assertRaises(rc.RivCampaignError) as ctx:
				rc.generate_full_riv_campaign(batch_size=1)
			self.assertIn("checksum changed", str(ctx.exception))

	def test_final_voucher_sets_generation_completed(self):
		rows = self._rows()
		state = self._state(
			manifest_checksum=rc.manifest_checksum(rows),
			last_successful_ordinal=2,
			campaign_created_riv=4,
		)

		with (
			mock.patch.object(rc, "_get_state", return_value=state),
			mock.patch.object(rc, "build_source_manifest", return_value=rows),
			mock.patch.object(rc, "assert_schedulers_stopped"),
			mock.patch.object(rc, "assert_continuation_riv_safe"),
			mock.patch.object(rc, "create_item_wise_repost_entries", return_value=[mock.Mock()]),
			mock.patch.object(rc.frappe.db, "commit"),
			mock.patch.object(rc, "riv_status_counts", return_value={
				"total": 5, "queued": 5, "in_progress": 0, "completed": 0, "failed": 0, "skipped": 0
			}),
		):
			out = rc.generate_full_riv_campaign(batch_size=10)

		self.assertTrue(out["ok"])
		self.assertEqual(state.generation_completed, 1)
		self.assertEqual(out["generation_completed"], 1)
		self.assertEqual(state.last_successful_ordinal, 3)

	def test_skipped_zero_accepted(self):
		counts = {
			"total": 10, "queued": 10, "in_progress": 0, "completed": 0, "failed": 0, "skipped": 0
		}
		# continuation must NOT fail solely because skipped is 0
		with mock.patch.object(rc, "riv_status_counts", return_value=counts):
			rc.assert_continuation_riv_safe()


class TestStatusReadOnly(unittest.TestCase):
	def test_status_does_not_mutate(self):
		state = mock.Mock(
			campaign_id="X",
			manifest_checksum="c",
			source_total=10,
			last_successful_ordinal=3,
			last_successful_doctype="Stock Entry",
			last_successful_voucher="SE-3",
			campaign_created_riv=7,
			generation_completed=0,
			generation_started=None,
			error_state=None,
			count_purchase_receipt=1,
			count_stock_entry=9,
			count_delivery_note=0,
			count_stock_reconciliation=0,
			first_doctype="Stock Entry",
			first_name="SE-1",
			first_posting_date="2026-01-01",
			first_posting_time="01:00:00",
			last_doctype="Stock Entry",
			last_name="SE-9",
			last_posting_date="2026-01-09",
			last_posting_time="09:00:00",
		)
		with (
			mock.patch.object(rc, "_get_state", return_value=state),
			mock.patch.object(rc, "riv_status_counts", return_value={
				"total": 7, "queued": 7, "in_progress": 0, "completed": 0, "failed": 0, "skipped": 0
			}),
			mock.patch.object(rc, "scheduler_guard_states", return_value={"parallel": {"stopped": 1}}),
			mock.patch.object(rc.frappe.db, "commit") as commit,
			mock.patch.object(rc.frappe.db, "set_value") as set_value,
		):
			out = rc.get_full_riv_campaign_status()
		self.assertEqual(out["remaining"], 7)
		self.assertEqual(out["last_successful_ordinal"], 3)
		commit.assert_not_called()
		set_value.assert_not_called()


class TestAtomicCommitOrder(unittest.TestCase):
	def test_checkpoint_advances_only_after_create_in_same_commit(self):
		"""create → checkpoint save → commit; never commit before checkpoint."""
		rows = [
			{"ordinal": 1, "doctype": "Stock Entry", "name": "SE-1", "company": "C",
			 "posting_date": "2026-01-01", "posting_time": "01:00:00", "creation": "2026-01-01 01:00:00"},
		]
		state = mock.Mock(
			campaign_id="RIV-GEN-TEST",
			manifest_checksum=rc.manifest_checksum(rows),
			source_total=1,
			count_purchase_receipt=0,
			count_stock_entry=1,
			count_delivery_note=0,
			count_stock_reconciliation=0,
			first_doctype="Stock Entry",
			first_name="SE-1",
			first_posting_date="2026-01-01",
			first_posting_time="01:00:00",
			last_doctype="Stock Entry",
			last_name="SE-1",
			last_posting_date="2026-01-01",
			last_posting_time="01:00:00",
			last_successful_ordinal=0,
			last_successful_doctype=None,
			last_successful_voucher=None,
			campaign_created_riv=0,
			generation_started="2026-01-01",
			generation_completed=0,
			error_state=None,
			flags=frappe._dict(),
		)
		order = []

		def create(vt, vn, **kw):
			order.append(("create", vn))
			return [mock.Mock()]

		def save(**kw):
			order.append(("checkpoint", state.last_successful_ordinal, state.last_successful_voucher))
			return state

		state.save = save

		def commit():
			order.append(("commit", state.last_successful_ordinal))

		with (
			mock.patch.object(rc, "_get_state", return_value=state),
			mock.patch.object(rc, "build_source_manifest", return_value=rows),
			mock.patch.object(rc, "assert_schedulers_stopped"),
			mock.patch.object(rc, "assert_continuation_riv_safe"),
			mock.patch.object(rc, "create_item_wise_repost_entries", side_effect=create),
			mock.patch.object(rc.frappe.db, "commit", side_effect=commit),
			mock.patch.object(rc, "riv_status_counts", return_value={
				"total": 1, "queued": 1, "in_progress": 0, "completed": 0, "failed": 0, "skipped": 0
			}),
		):
			rc.generate_full_riv_campaign(batch_size=1)

		self.assertEqual(order[0], ("create", "SE-1"))
		self.assertEqual(order[1][0], "checkpoint")
		self.assertEqual(order[1][1], 1)
		self.assertEqual(order[2], ("commit", 1))


if __name__ == "__main__":
	unittest.main()

# Copyright (c) 2026, Farbod Siyahpoosh and contributors
"""Unit/API tests for Account Explorer prepared-result architecture (v5.5.36)."""

from __future__ import annotations

import gzip
import json
import os
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.account_explorer import api
from erpnext_extensions.iran_accounting.account_explorer.cache_revision import (
	bump_accounting_revision,
	get_accounting_revision,
)
from erpnext_extensions.iran_accounting.account_explorer.prepared_report import (
	PREPARED_DOCTYPE,
	_load_artifact,
	extract_execution_id,
)
from erpnext_extensions.iran_accounting.account_explorer.query_fingerprint import (
	build_fingerprint,
	canonical_query_dict,
	query_hash,
)
from erpnext_extensions.iran_accounting.account_explorer.query_spec import (
	AccountExplorerQuerySpec_from_client,
)
from erpnext_extensions.iran_accounting.tests.test_account_explorer_fixtures import (
	current_fiscal_year,
	enable_account_explorer,
	require_site,
)


class TestAccountExplorerPreparedResult(unittest.TestCase):
	def setUp(self):
		try:
			self.company = require_site(self)
		except unittest.SkipTest:
			if frappe.db and frappe.db.exists("Company", "اسپاد فارمد دارو"):
				self.company = "اسپاد فارمد دارو"
			else:
				raise
		enable_account_explorer()
		frappe.set_user("Administrator")
		frappe.flags.ae_prepared_inline = True
		frappe.flags.ae_prepared_defer = False
		frappe.flags.ae_prepared_skip = False
		if hasattr(frappe.local, "request_cache"):
			frappe.local.request_cache.clear()

	def tearDown(self):
		frappe.flags.ae_prepared_inline = False
		frappe.flags.ae_prepared_defer = False
		frappe.flags.ae_prepared_skip = False

	def _new_execution_id(self) -> str:
		return f"test-{uuid.uuid4().hex[:24]}"

	def _payload(self, *, include_opening=0, page=1, sort_field="display_code", execution_id=None):
		fy = current_fiscal_year(self.company)
		if fy:
			fy_name, from_date, to_date = fy
		elif frappe.db.exists("Fiscal Year", "1405"):
			fy_name, from_date, to_date = ("1405", "2026-03-21", "2027-03-20")
		else:
			fy_name, from_date, to_date = (None, "2026-03-21", "2027-03-20")
		payload = {
			"document_scope": {
				"company": self.company,
				"fiscal_year": fy_name,
				"from_date": from_date,
				"to_date": to_date,
				"hide_zero_rows": 1,
				"status": {
					"include_opening_entries": include_opening,
					"include_cancelled_entries": 0,
					"include_default_finance_book_entries": 1,
					"include_period_closing_vouchers": 0,
				},
			},
			"analysis_context": {
				"view_axis": "account_level",
				"detail_mode": "summary",
				"page": page,
				"page_size": 50,
				"sort_field": sort_field,
				"sort_order": "asc",
			},
			"execution_id": execution_id or self._new_execution_id(),
		}
		return payload

	def test_fingerprint_stable_and_ignores_page(self):
		spec_a = AccountExplorerQuerySpec_from_client(self._payload(page=1), require_dates=True)
		spec_b = AccountExplorerQuerySpec_from_client(self._payload(page=2), require_dates=True)
		self.assertEqual(query_hash(spec_a), query_hash(spec_b))
		self.assertNotIn("page", canonical_query_dict(spec_a))

	def test_opening_policy_changes_fingerprint(self):
		off = AccountExplorerQuerySpec_from_client(self._payload(include_opening=0), require_dates=True)
		on = AccountExplorerQuerySpec_from_client(self._payload(include_opening=1), require_dates=True)
		self.assertNotEqual(query_hash(off), query_hash(on))

	def test_prepared_miss_then_same_execution_hit(self):
		payload = self._payload(include_opening=0)
		frappe.flags.ae_prepared_defer = True
		miss = api.get_account_summary(json.dumps(payload))
		self.assertEqual(miss.get("status"), "preparing")
		job_id = miss.get("job_id")
		self.assertTrue(job_id)
		self.assertEqual(miss.get("execution_id"), payload["execution_id"])

		frappe.flags.ae_prepared_defer = False
		from erpnext_extensions.iran_accounting.account_explorer.background_jobs import (
			build_account_explorer_prepared_result,
		)

		build_account_explorer_prepared_result(job_id, json.dumps(payload))
		hit = api.get_account_summary(json.dumps(payload))
		self.assertEqual(hit.get("status"), "ready")
		self.assertEqual(cint(hit.get("prepared")), 1)
		self.assertEqual(cint(hit.get("fresh")), 1)
		self.assertEqual(hit.get("execution_id"), payload["execution_id"])

		frappe.flags.ae_prepared_skip = True
		live = api.get_account_summary(json.dumps(payload))
		frappe.flags.ae_prepared_skip = False
		self.assertEqual(len(hit.get("rows") or []), len(live.get("rows") or []))
		for left, right in zip(hit.get("rows") or [], live.get("rows") or [], strict=False):
			self.assertEqual(left.get("row_key"), right.get("row_key"))
			self.assertEqual(flt(left.get("period_debit")), flt(right.get("period_debit")))

	def test_old_completed_artifact_ignored_by_new_execution(self):
		"""A/B: prior Completed row must not satisfy a new execution_id."""
		first = self._payload(include_opening=0)
		ready1 = api.get_account_summary(json.dumps(first))
		self.assertEqual(ready1.get("status"), "ready")
		doc1 = frappe.get_doc(
			PREPARED_DOCTYPE, {"execution_id": first["execution_id"]}
		)
		self.assertEqual(doc1.status, "Completed")
		self.assertTrue(doc1.result_file)

		# New execution with identical filters must rebuild, not reuse doc1.
		frappe.flags.ae_prepared_defer = True
		second = self._payload(include_opening=0)
		self.assertNotEqual(first["execution_id"], second["execution_id"])
		preparing = api.get_account_summary(json.dumps(second))
		self.assertEqual(preparing.get("status"), "preparing")
		self.assertNotEqual(preparing.get("job_id"), doc1.name)
		doc1.reload()
		self.assertEqual(doc1.status, "Completed")  # untouched

		frappe.flags.ae_prepared_defer = False
		from erpnext_extensions.iran_accounting.account_explorer.background_jobs import (
			build_account_explorer_prepared_result,
		)

		build_account_explorer_prepared_result(preparing["job_id"], json.dumps(second))
		ready2 = api.get_account_summary(json.dumps(second))
		self.assertEqual(ready2.get("status"), "ready")
		self.assertNotEqual(ready2.get("job_id"), doc1.name)

	def test_pagination_and_sort_reuse_execution(self):
		"""E/F: page/sort keep execution_id → same prepared row."""
		base = self._payload(include_opening=0, page=1)
		ready = api.get_account_summary(json.dumps(base))
		self.assertEqual(ready.get("status"), "ready")
		job = frappe.db.get_value(PREPARED_DOCTYPE, {"execution_id": base["execution_id"]}, "name")

		page2 = dict(base)
		page2["analysis_context"] = {**base["analysis_context"], "page": 2}
		hit = api.get_account_summary(json.dumps(page2))
		self.assertEqual(hit.get("status"), "ready")
		self.assertEqual(
			frappe.db.get_value(PREPARED_DOCTYPE, {"execution_id": base["execution_id"]}, "name"),
			job,
		)

		sorted_payload = dict(base)
		sorted_payload["analysis_context"] = {
			**base["analysis_context"],
			"sort_field": "debit_balance",
			"sort_order": "desc",
		}
		hit2 = api.get_account_summary(json.dumps(sorted_payload))
		self.assertEqual(hit2.get("status"), "ready")
		self.assertEqual(
			frappe.db.get_value(PREPARED_DOCTYPE, {"execution_id": base["execution_id"]}, "name"),
			job,
		)

	def test_filter_change_with_new_execution_rebuilds(self):
		"""G: opening flag change + new execution_id → new job."""
		a = self._payload(include_opening=0)
		api.get_account_summary(json.dumps(a))
		b = self._payload(include_opening=1)
		frappe.flags.ae_prepared_defer = True
		prep = api.get_account_summary(json.dumps(b))
		self.assertEqual(prep.get("status"), "preparing")
		frappe.flags.ae_prepared_defer = False

	def test_revision_bump_forces_rebuild_same_execution(self):
		payload = self._payload(include_opening=0)
		api.get_account_summary(json.dumps(payload))
		before = get_accounting_revision(self.company)
		bump_accounting_revision(company=self.company)
		self.assertGreater(get_accounting_revision(self.company), before)
		frappe.flags.ae_prepared_defer = True
		after = api.get_account_summary(json.dumps(payload))
		self.assertEqual(after.get("status"), "preparing")
		frappe.flags.ae_prepared_defer = False

	def _completed_prepared_doc(self, payload: dict):
		frappe.flags.ae_prepared_defer = False
		ready = api.get_account_summary(json.dumps(payload))
		self.assertEqual(ready.get("status"), "ready")
		name = frappe.db.get_value(PREPARED_DOCTYPE, {"execution_id": payload["execution_id"]}, "name")
		self.assertTrue(name)
		return frappe.get_doc(PREPARED_DOCTYPE, name), payload["execution_id"], ready

	def test_missing_physical_artifact_rebuilds_without_filenotfound(self):
		payload = self._payload(include_opening=0)
		doc, _eid, first = self._completed_prepared_doc(payload)
		rel = doc.result_file.replace("/private/files/", "")
		abs_path = frappe.get_site_path("private", "files", rel)
		os.remove(abs_path)

		second = api.get_account_summary(json.dumps(payload))
		self.assertNotEqual(second.get("status"), "Error")
		self.assertIn(second.get("status"), {"ready", "preparing"})
		if second.get("status") == "ready":
			self.assertEqual(cint(second.get("prepared")), 1)
			self.assertEqual(len(second.get("rows") or []), len(first.get("rows") or []))

	def test_missing_file_document_rebuilds(self):
		payload = self._payload(include_opening=0)
		doc, _eid, first = self._completed_prepared_doc(payload)
		file_name = frappe.db.get_value("File", {"file_url": doc.result_file}, "name")
		frappe.delete_doc("File", file_name, ignore_permissions=True, force=True)
		frappe.db.commit()
		second = api.get_account_summary(json.dumps(payload))
		self.assertIn(second.get("status"), {"ready", "preparing"})

	def test_corrupt_gzip_and_invalid_json_soft_fail(self):
		payload = self._payload(include_opening=0)
		doc, _eid, _first = self._completed_prepared_doc(payload)
		rel = doc.result_file.replace("/private/files/", "")
		abs_path = frappe.get_site_path("private", "files", rel)
		with open(abs_path, "wb") as handle:
			handle.write(b"not-a-valid-gzip-or-json")
		self.assertIsNone(_load_artifact(doc))
		with open(abs_path, "wb") as handle:
			handle.write(gzip.compress(b"{not-json"))
		self.assertIsNone(_load_artifact(doc))
		second = api.get_account_summary(json.dumps(payload))
		self.assertIn(second.get("status"), {"ready", "preparing"})

	def test_concurrent_polls_same_execution(self):
		"""I: polls for one execution_id share a single job; parallel first-hits stay unique."""
		payload = self._payload(include_opening=0)
		frappe.flags.ae_prepared_defer = True
		first = api.get_account_summary(json.dumps(payload))
		second = api.get_account_summary(json.dumps(payload))
		self.assertEqual(first.get("status"), "preparing")
		self.assertEqual(second.get("job_id"), first.get("job_id"))
		self.assertEqual(second.get("execution_id"), payload["execution_id"])

		site = frappe.local.site
		eid = self._new_execution_id()
		parallel_payload = self._payload(include_opening=0, execution_id=eid)

		def _poll(_):
			frappe.init(site=site)
			frappe.connect()
			frappe.set_user("Administrator")
			frappe.flags.ae_prepared_inline = True
			frappe.flags.ae_prepared_defer = True
			try:
				result = api.get_account_summary(json.dumps(parallel_payload))
				frappe.db.commit()
				return result.get("job_id")
			finally:
				frappe.destroy()

		with ThreadPoolExecutor(max_workers=4) as pool:
			job_ids = set(pool.map(_poll, range(4)))
		self.assertEqual(len(job_ids), 1)
		# End this connection's snapshot so committed peer inserts are visible.
		frappe.db.commit()
		self.assertEqual(frappe.db.count(PREPARED_DOCTYPE, {"execution_id": eid}), 1)

		frappe.flags.ae_prepared_defer = False
		from erpnext_extensions.iran_accounting.account_explorer.background_jobs import (
			build_account_explorer_prepared_result,
		)

		build_account_explorer_prepared_result(first["job_id"], json.dumps(payload))
		final = api.get_account_summary(json.dumps(payload))
		self.assertEqual(final.get("status"), "ready")
		self.assertEqual(frappe.db.count(PREPARED_DOCTYPE, {"execution_id": payload["execution_id"]}), 1)

	def test_sessions_isolated_by_execution_id(self):
		"""H: two browser sessions / execution ids stay isolated."""
		a = self._payload(include_opening=0)
		b = self._payload(include_opening=0)
		ra = api.get_account_summary(json.dumps(a))
		rb = api.get_account_summary(json.dumps(b))
		self.assertEqual(ra.get("status"), "ready")
		self.assertEqual(rb.get("status"), "ready")
		na = frappe.db.get_value(PREPARED_DOCTYPE, {"execution_id": a["execution_id"]}, "name")
		nb = frappe.db.get_value(PREPARED_DOCTYPE, {"execution_id": b["execution_id"]}, "name")
		self.assertNotEqual(na, nb)

	def test_extract_execution_id_mints_when_missing(self):
		first = extract_execution_id({})
		second = extract_execution_id({})
		self.assertTrue(first)
		self.assertNotEqual(first, second)

	def test_load_artifact_does_not_swallow_unexpected_errors(self):
		payload = self._payload(include_opening=0)
		doc, _eid, _first = self._completed_prepared_doc(payload)
		with patch(
			"erpnext_extensions.iran_accounting.account_explorer.prepared_report.frappe.get_doc",
			side_effect=RuntimeError("boom"),
		):
			with self.assertRaises(RuntimeError):
				_load_artifact(doc)

	def test_cleanup_skips_active_and_respects_batch(self):
		from erpnext_extensions.iran_accounting.account_explorer.prepared_cleanup import (
			cleanup_expired_prepared_executions,
		)

		payload = self._payload(include_opening=0)
		frappe.flags.ae_prepared_defer = True
		prep = api.get_account_summary(json.dumps(payload))
		frappe.flags.ae_prepared_defer = False
		# Active Queued must not be deleted even if aged (cleanup filters terminal only).
		result = cleanup_expired_prepared_executions(batch_size=5)
		self.assertIn("deleted", result)
		self.assertTrue(frappe.db.exists(PREPARED_DOCTYPE, prep["job_id"]))


def cint(value):
	from frappe.utils import cint as _cint

	return _cint(value)

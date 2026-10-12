# Copyright (c) 2026, Farbod Siyahpoosh and contributors
"""Unit/API tests for Account Explorer prepared-result architecture (v4.6.0)."""

from __future__ import annotations

import gzip
import json
import os
import unittest
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
		# Prefer ERPNext fixture company; fall back to restored Production company
		# so artifact lifecycle tests still run after DB-only restores.
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

	def _payload(self, *, include_opening=0, page=1, sort_field="display_code"):
		fy = current_fiscal_year(self.company)
		if fy:
			fy_name, from_date, to_date = fy
		elif frappe.db.exists("Fiscal Year", "1405"):
			fy_name, from_date, to_date = ("1405", "2026-03-21", "2027-03-20")
		else:
			fy_name, from_date, to_date = (None, "2026-03-21", "2027-03-20")
		return {
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
		}

	def test_fingerprint_stable_and_ignores_page(self):
		spec_a = AccountExplorerQuerySpec_from_client(self._payload(page=1), require_dates=True)
		spec_b = AccountExplorerQuerySpec_from_client(self._payload(page=2), require_dates=True)
		self.assertEqual(query_hash(spec_a), query_hash(spec_b))
		self.assertNotIn("page", canonical_query_dict(spec_a))

	def test_opening_policy_changes_fingerprint(self):
		off = AccountExplorerQuerySpec_from_client(self._payload(include_opening=0), require_dates=True)
		on = AccountExplorerQuerySpec_from_client(self._payload(include_opening=1), require_dates=True)
		self.assertNotEqual(query_hash(off), query_hash(on))

	def _purge_prepared_for_payload(self, payload: dict):
		"""Remove leftover prepared rows for this fingerprint (DB-restore leftovers)."""
		spec = AccountExplorerQuerySpec_from_client(payload, require_dates=True)
		fingerprint = build_fingerprint(spec, get_accounting_revision(self.company))
		for name in frappe.get_all(
			PREPARED_DOCTYPE, filters={"fingerprint": fingerprint}, pluck="name"
		):
			frappe.delete_doc(PREPARED_DOCTYPE, name, force=True, ignore_permissions=True)
		frappe.db.commit()
		return fingerprint

	def test_prepared_miss_hit_and_invalidation(self):
		payload = self._payload(include_opening=0)
		self._purge_prepared_for_payload(payload)
		# Defer first call → preparing
		frappe.flags.ae_prepared_defer = True
		miss = api.get_account_summary(json.dumps(payload))
		self.assertEqual(miss.get("status"), "preparing")
		job_id = miss.get("job_id")
		self.assertTrue(job_id)

		frappe.flags.ae_prepared_defer = False
		from erpnext_extensions.iran_accounting.account_explorer.background_jobs import (
			build_account_explorer_prepared_result,
		)

		build_account_explorer_prepared_result(job_id, json.dumps(payload))
		hit = api.get_account_summary(json.dumps(payload))
		self.assertEqual(hit.get("status"), "ready")
		self.assertEqual(cint(hit.get("prepared")), 1)
		self.assertTrue(hit.get("rows") is not None)

		# Live path must match accounting numbers
		frappe.flags.ae_prepared_skip = True
		live = api.get_account_summary(json.dumps(payload))
		frappe.flags.ae_prepared_skip = False
		self.assertEqual(len(hit.get("rows") or []), len(live.get("rows") or []))
		for left, right in zip(hit.get("rows") or [], live.get("rows") or [], strict=False):
			self.assertEqual(left.get("row_key"), right.get("row_key"))
			self.assertEqual(flt(left.get("period_debit")), flt(right.get("period_debit")))
			self.assertEqual(flt(left.get("period_credit")), flt(right.get("period_credit")))

		before = get_accounting_revision(self.company)
		bump_accounting_revision(company=self.company)
		self.assertGreater(get_accounting_revision(self.company), before)

		frappe.flags.ae_prepared_defer = True
		after = api.get_account_summary(json.dumps(payload))
		self.assertEqual(after.get("status"), "preparing")

	def _completed_prepared_doc(self, payload: dict):
		frappe.flags.ae_prepared_defer = False
		ready = api.get_account_summary(json.dumps(payload))
		self.assertEqual(ready.get("status"), "ready")
		spec = AccountExplorerQuerySpec_from_client(payload, require_dates=True)
		fingerprint = build_fingerprint(spec, get_accounting_revision(self.company))
		name = frappe.db.get_value(PREPARED_DOCTYPE, {"fingerprint": fingerprint}, "name")
		self.assertTrue(name)
		return frappe.get_doc(PREPARED_DOCTYPE, name), fingerprint, ready

	def test_missing_physical_artifact_rebuilds_without_filenotfound(self):
		"""B: Completed metadata + missing disk file → rebuild, no FileNotFoundError."""
		payload = self._payload(include_opening=0)
		doc, _fingerprint, first = self._completed_prepared_doc(payload)
		self.assertTrue(doc.result_file)

		rel = doc.result_file.replace("/private/files/", "")
		abs_path = frappe.get_site_path("private", "files", rel)
		self.assertTrue(os.path.exists(abs_path), abs_path)
		os.remove(abs_path)
		self.assertFalse(os.path.exists(abs_path))

		# File DocType row remains (restore-like); only bytes are gone.
		self.assertTrue(frappe.db.exists("File", {"file_url": doc.result_file}))

		second = api.get_account_summary(json.dumps(payload))
		self.assertNotEqual(second.get("status"), "Error")
		self.assertIn(second.get("status"), {"ready", "preparing"})
		if second.get("status") == "ready":
			self.assertEqual(cint(second.get("prepared")), 1)
			self.assertEqual(len(second.get("rows") or []), len(first.get("rows") or []))
			doc.reload()
			self.assertEqual(doc.status, "Completed")
			self.assertTrue(doc.result_file)
			rel2 = doc.result_file.replace("/private/files/", "")
			self.assertTrue(os.path.exists(frappe.get_site_path("private", "files", rel2)))

	def test_missing_file_document_rebuilds(self):
		"""C: Prepared row points at a File URL with no File document."""
		payload = self._payload(include_opening=0)
		doc, _fingerprint, first = self._completed_prepared_doc(payload)
		file_name = frappe.db.get_value("File", {"file_url": doc.result_file}, "name")
		self.assertTrue(file_name)
		# Delete File row only; leave bytes if any (simulates orphaned pointer).
		frappe.delete_doc("File", file_name, ignore_permissions=True, force=True)
		frappe.db.commit()

		second = api.get_account_summary(json.dumps(payload))
		self.assertIn(second.get("status"), {"ready", "preparing"})
		if second.get("status") == "ready":
			self.assertEqual(len(second.get("rows") or []), len(first.get("rows") or []))

	def test_corrupt_gzip_and_invalid_json_soft_fail(self):
		"""D/E: corrupt gzip / invalid JSON return None from loader (no raise)."""
		payload = self._payload(include_opening=0)
		doc, _fingerprint, _first = self._completed_prepared_doc(payload)
		rel = doc.result_file.replace("/private/files/", "")
		abs_path = frappe.get_site_path("private", "files", rel)

		with open(abs_path, "wb") as handle:
			handle.write(b"not-a-valid-gzip-or-json")
		self.assertIsNone(_load_artifact(doc))

		with open(abs_path, "wb") as handle:
			handle.write(gzip.compress(b"{not-json"))
		self.assertIsNone(_load_artifact(doc))

		# API recovers by rebuild.
		second = api.get_account_summary(json.dumps(payload))
		self.assertIn(second.get("status"), {"ready", "preparing"})

	def test_concurrent_missing_artifact_single_rebuild(self):
		"""F: concurrent polls for the same missing artifact do not raise."""
		payload = self._payload(include_opening=0)
		doc, _fingerprint, _first = self._completed_prepared_doc(payload)
		rel = doc.result_file.replace("/private/files/", "")
		abs_path = frappe.get_site_path("private", "files", rel)
		os.remove(abs_path)

		site = frappe.local.site

		def _poll(_):
			# Race the invalidate path only (defer rebuild) so concurrent polls
			# exercise locking without four full materializations.
			frappe.init(site=site)
			frappe.connect()
			frappe.set_user("Administrator")
			frappe.flags.ae_prepared_inline = True
			frappe.flags.ae_prepared_defer = True
			try:
				return api.get_account_summary(json.dumps(payload))
			finally:
				frappe.destroy()

		with ThreadPoolExecutor(max_workers=4) as pool:
			results = list(pool.map(_poll, range(4)))

		for result in results:
			self.assertNotEqual(result.get("status"), "Error")
			self.assertIn(result.get("status"), {"ready", "preparing"})
			self.assertNotIn("FileNotFoundError", str(result.get("message") or ""))
			self.assertNotIn("FileNotFoundError", str(result.get("error_message") or ""))

		# Single rebuild after the storm must succeed and keep one prepared row.
		frappe.flags.ae_prepared_defer = False
		final = api.get_account_summary(json.dumps(payload))
		self.assertEqual(final.get("status"), "ready")
		spec = AccountExplorerQuerySpec_from_client(payload, require_dates=True)
		fingerprint = build_fingerprint(spec, get_accounting_revision(self.company))
		count = frappe.db.count(PREPARED_DOCTYPE, {"fingerprint": fingerprint})
		self.assertEqual(count, 1)

	def test_valid_artifact_still_loads(self):
		"""A/J: valid artifact path unchanged."""
		payload = self._payload(include_opening=0)
		_doc, _fp, first = self._completed_prepared_doc(payload)
		second = api.get_account_summary(json.dumps(payload))
		self.assertEqual(second.get("status"), "ready")
		self.assertEqual(cint(second.get("prepared")), 1)
		self.assertEqual(len(second.get("rows") or []), len(first.get("rows") or []))

	def test_load_artifact_does_not_swallow_unexpected_errors(self):
		"""Unexpected loader failures must still surface."""
		payload = self._payload(include_opening=0)
		doc, _fp, _first = self._completed_prepared_doc(payload)

		with patch(
			"erpnext_extensions.iran_accounting.account_explorer.prepared_report.frappe.get_doc",
			side_effect=RuntimeError("boom"),
		):
			with self.assertRaises(RuntimeError):
				_load_artifact(doc)


def cint(value):
	from frappe.utils import cint as _cint

	return _cint(value)

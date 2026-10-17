# Copyright (c) 2026, ERPNext Extensions contributors
"""Regression: shared/empty RIV JSON recovery + get_reposting_data safety."""

from __future__ import annotations

import gzip
import json
import unittest
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from erpnext_extensions.iran_accounting.historical_rebuild.riv_file_recovery import (
	_is_json_file_failure,
	list_recoverable_failed_rivs,
	recover_failed_riv,
)
from erpnext_extensions.iran_accounting.integration.monkey_patches import (
	_patch_reposting_data_json_safety,
)


class TestRivJsonFailureClassify(FrappeTestCase):
	def test_jsondecode_is_recoverable(self):
		self.assertTrue(
			_is_json_file_failure(
				"JSONDecodeError: Expecting value: line 1 column 1 (char 0)",
				"/private/files/repost_item_valuation-5946ptkd5h.json.gz",
			)
		)

	def test_get_reposting_data_marker_is_recoverable(self):
		self.assertTrue(
			_is_json_file_failure(
				"File apps/erpnext/.../get_reposting_data",
				"/private/files/repost_item_valuation-abc.json.gz",
			)
		)

	def test_unrelated_failure_without_file_not_auto_json(self):
		# Without file pointer and without JSON markers → not this recovery class
		# (component scrap / I3 handled elsewhere).
		self.assertFalse(_is_json_file_failure("Component Scrap issued rate missing", None))


class TestGetRepostingDataPatch(FrappeTestCase):
	def setUp(self):
		_patch_reposting_data_json_safety()

	def test_empty_content_returns_empty_dict_not_json_error(self):
		import erpnext.stock.stock_ledger as sl

		fake_file = MagicMock()
		fake_file.get_content.return_value = b""
		with patch("frappe.db.get_value", return_value="FILE-1"), patch(
			"frappe.get_doc", return_value=fake_file
		):
			out = sl.get_reposting_data("/private/files/repost_item_valuation-x.json.gz")
		self.assertEqual(dict(out), {})

	def test_empty_gzip_payload_returns_empty_dict(self):
		import erpnext.stock.stock_ledger as sl

		fake_file = MagicMock()
		fake_file.get_content.return_value = gzip.compress(b"")
		with patch("frappe.db.get_value", return_value="FILE-1"), patch(
			"frappe.get_doc", return_value=fake_file
		):
			out = sl.get_reposting_data("/private/files/repost_item_valuation-x.json.gz")
		self.assertEqual(dict(out), {})

	def test_valid_empty_skeleton_json_parses(self):
		import erpnext.stock.stock_ledger as sl

		payload = {"item_wh_wise_last_posted_sle": {}, "repost_affected_transaction": []}
		fake_file = MagicMock()
		fake_file.get_content.return_value = gzip.compress(
			frappe.safe_encode(frappe.as_json(payload))
		)
		with patch("frappe.db.get_value", return_value="FILE-1"), patch(
			"frappe.get_doc", return_value=fake_file
		):
			out = sl.get_reposting_data("/private/files/repost_item_valuation-x.json.gz")
		self.assertEqual(out.get("repost_affected_transaction"), [])


class TestRecoverFailedRivPreconditions(FrappeTestCase):
	def test_missing_riv_returns_not_found(self):
		out = recover_failed_riv("DOES-NOT-EXIST-RIV", execute=False)
		self.assertFalse(out["ok"])
		self.assertEqual(out["detail"], "riv_not_found")


if __name__ == "__main__":
	unittest.main()

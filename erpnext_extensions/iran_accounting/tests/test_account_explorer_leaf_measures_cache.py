# Copyright (c) 2026, Farbod Siyahpoosh and contributors

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase

from erpnext_extensions.iran_accounting.account_explorer.cache_revision import (
	bump_accounting_revision,
	get_accounting_revision,
)
from erpnext_extensions.iran_accounting.account_explorer.query_builder import (
	_get_account_wise_measures_for_level_summary,
)
from erpnext_extensions.iran_accounting.account_explorer.query_fingerprint import (
	measures_query_hash,
	query_hash,
)
from erpnext_extensions.iran_accounting.account_explorer.query_spec import (
	AccountExplorerQuerySpec_from_client,
)


def _pick_company() -> str | None:
	for name in ("_Test Company", "اسپاد فارمد دارو"):
		if frappe.db.exists("Company", name):
			return name
	row = frappe.db.get_value("Company", {}, "name")
	return row


def _payload(company: str, level_sequence: int = 4, execution_id: str = "test-exec-1") -> dict:
	fy = frappe.db.get_value("Fiscal Year", {"year_start_date": ("<=", "2024-06-01"), "year_end_date": (">=", "2024-06-01")}, "name")
	return {
		"document_scope": {
			"company": company,
			"fiscal_year": fy,
			"from_date": "2024-01-01",
			"to_date": "2024-12-31",
			"hide_zero_rows": 1,
			"status": {
				"include_opening_entries": 1,
				"include_cancelled_entries": 0,
				"include_default_finance_book_entries": 1,
				"include_period_closing_vouchers": 0,
			},
		},
		"analysis_context": {
			"view_axis": "account_level",
			"detail_mode": "summary",
			"level_sequence": level_sequence,
			"page": 1,
			"page_size": 50,
		},
		"execution_id": execution_id,
	}


class TestAccountExplorerLeafMeasuresCache(FrappeTestCase):
	def setUp(self):
		self.company = _pick_company()
		if not self.company:
			self.skipTest("No Company available")

	def test_measures_hash_ignores_level_sequence(self):
		spec_a = AccountExplorerQuerySpec_from_client(_payload(self.company, 4), require_dates=True)
		spec_b = AccountExplorerQuerySpec_from_client(_payload(self.company, 6), require_dates=True)
		self.assertEqual(measures_query_hash(spec_a), measures_query_hash(spec_b))
		self.assertNotEqual(query_hash(spec_a), query_hash(spec_b))

	def test_execution_local_cache_hit_and_revision_invalidation(self):
		import uuid

		execution_id = f"test-exec-leaf-cache-{uuid.uuid4().hex[:12]}"
		spec = AccountExplorerQuerySpec_from_client(
			_payload(self.company, 4, execution_id=execution_id), require_dates=True
		)
		frappe.flags.ae_execution_id = execution_id
		frappe.flags.ae_leaf_measures_cache_hit = None

		first = _get_account_wise_measures_for_level_summary(spec, spec.included_account_names)
		self.assertEqual(frappe.flags.ae_leaf_measures_cache_hit, 0)

		spec_level = AccountExplorerQuerySpec_from_client(
			_payload(self.company, 8, execution_id=execution_id), require_dates=True
		)
		frappe.flags.ae_execution_id = execution_id
		second = _get_account_wise_measures_for_level_summary(
			spec_level, spec_level.included_account_names
		)
		self.assertEqual(frappe.flags.ae_leaf_measures_cache_hit, 1)
		self.assertEqual(set(first.keys()), set(second.keys()))

		bump_accounting_revision(company=spec.company)
		frappe.flags.ae_execution_id = execution_id
		_get_account_wise_measures_for_level_summary(spec, spec.included_account_names)
		self.assertEqual(frappe.flags.ae_leaf_measures_cache_hit, 0)
		self.assertGreaterEqual(get_accounting_revision(spec.company), 1)

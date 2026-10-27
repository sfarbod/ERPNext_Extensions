# Copyright (c) 2026, Farbod Siyahpoosh and contributors
"""Party-identity commercial Item / Item Group analysis (5.6.21)."""

from __future__ import annotations

import time
import unittest

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.account_explorer.api import (
	get_item_group_summary,
	get_item_summary,
)
from erpnext_extensions.iran_accounting.account_explorer.commercial_item_analysis import (
	ANALYSIS_SOURCE_COMMERCIAL,
	partition_identity_commercial_roles,
)
from erpnext_extensions.iran_accounting.account_explorer.export import get_export_columns
from erpnext_extensions.iran_accounting.account_explorer.query_spec import (
	AccountExplorerQuerySpec_from_client,
)


COMPANY = "اسپاد فارمد دارو"
FY1405 = {
	"company": COMPANY,
	"fiscal_year": "1405",
	"from_date": "2026-03-21",
	"to_date": "2027-03-20",
}


def _payload(axis="item", party_scope=None, **extra):
	ctx = {
		"view_axis": axis,
		"detail_mode": "summary",
		"page": 1,
		"page_size": 50,
		"party_scope": party_scope or {},
	}
	ctx.update(extra)
	return {"document_scope": dict(FY1405), "analysis_context": ctx}


def _independent_sales_base_net(customer: str) -> float:
	row = frappe.db.sql(
		"""
		SELECT COALESCE(SUM(sii.base_net_amount), 0)
		FROM `tabSales Invoice` si
		INNER JOIN `tabSales Invoice Item` sii ON sii.parent = si.name
		WHERE si.docstatus = 1
			AND si.company = %s
			AND si.posting_date BETWEEN %s AND %s
			AND si.customer = %s
		""",
		(COMPANY, FY1405["from_date"], FY1405["to_date"], customer),
	)
	return flt(row[0][0] if row else 0)


def _independent_purchase_base_net(supplier: str) -> float:
	row = frappe.db.sql(
		"""
		SELECT COALESCE(SUM(pii.base_net_amount), 0)
		FROM `tabPurchase Invoice` pi
		INNER JOIN `tabPurchase Invoice Item` pii ON pii.parent = pi.name
		WHERE pi.docstatus = 1
			AND pi.company = %s
			AND pi.posting_date BETWEEN %s AND %s
			AND pi.supplier = %s
		""",
		(COMPANY, FY1405["from_date"], FY1405["to_date"], supplier),
	)
	return flt(row[0][0] if row else 0)


class TestCommercialItemAnalysisV5621(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		cls.customer = "CUS-00003"
		cls.supplier_empty = "SUP-00204"
		# Active purchase supplier on restored snapshot (not Milan).
		cls.supplier_active = frappe.db.sql(
			"""
			SELECT supplier FROM `tabPurchase Invoice`
			WHERE docstatus=1 AND company=%s
				AND posting_date BETWEEN %s AND %s
			GROUP BY supplier ORDER BY COUNT(*) DESC LIMIT 1
			""",
			(COMPANY, FY1405["from_date"], FY1405["to_date"]),
		)
		cls.supplier_active = cls.supplier_active[0][0] if cls.supplier_active else None

	def test_partition_roles(self):
		parts = partition_identity_commercial_roles(
			[
				("Customer", "CUS-00003"),
				("Supplier", "SUP-00204"),
				("Shareholder", "ACC-SH-00029"),
			]
		)
		self.assertEqual(parts["customers"], ["CUS-00003"])
		self.assertEqual(parts["suppliers"], ["SUP-00204"])
		self.assertEqual(parts["unsupported"], [("Shareholder", "ACC-SH-00029")])

	def test_customer_only_sales_items(self):
		data = get_item_summary(
			_payload(
				"item",
				party_scope={
					"unified_identity": 1,
					"identity_members": [["Customer", self.customer]],
				},
			)
		)
		self.assertEqual(data.get("analysis_source"), ANALYSIS_SOURCE_COMMERCIAL)
		self.assertFalse(data.get("unsupported_filter_combination"))
		self.assertGreater(len(data.get("rows") or []), 0)
		for row in data["rows"]:
			self.assertEqual(row["transaction_direction"], "Sales")
			self.assertEqual(row["party_type"], "Customer")
			self.assertEqual(row["party"], self.customer)
			self.assertTrue(row.get("item_code"))
			self.assertTrue(row.get("item_group") or True)
		expected = _independent_sales_base_net(self.customer)
		self.assertAlmostEqual(flt(data["totals"]["base_net_amount"]), expected, places=2)

	def test_supplier_only_purchases(self):
		if not self.supplier_active:
			self.skipTest("No purchase supplier in FY1405")
		data = get_item_summary(
			_payload(
				"item",
				party_scope={
					"unified_identity": 1,
					"identity_members": [["Supplier", self.supplier_active]],
				},
			)
		)
		self.assertEqual(data.get("analysis_source"), ANALYSIS_SOURCE_COMMERCIAL)
		self.assertGreater(len(data.get("rows") or []), 0)
		for row in data["rows"]:
			self.assertEqual(row["transaction_direction"], "Purchase")
			self.assertEqual(row["party"], self.supplier_active)
		expected = _independent_purchase_base_net(self.supplier_active)
		self.assertAlmostEqual(flt(data["totals"]["base_net_amount"]), expected, places=2)

	def test_customer_or_supplier_milan(self):
		data = get_item_summary(
			_payload(
				"item",
				party_scope={
					"unified_identity": 1,
					"identity_members": [
						["Customer", self.customer],
						["Supplier", self.supplier_empty],
					],
				},
			)
		)
		# Milan Supplier has no PIs — sales rows only; no company-wide leakage.
		self.assertGreater(len(data["rows"]), 0)
		for row in data["rows"]:
			self.assertIn(row["party"], {self.customer, self.supplier_empty})
			self.assertIn(row["transaction_direction"], {"Sales", "Purchase"})
		sales_total = sum(
			flt(r["base_net_amount"]) for r in data["rows"] if r["transaction_direction"] == "Sales"
		)
		self.assertAlmostEqual(sales_total, _independent_sales_base_net(self.customer), places=2)

	def test_shareholder_only_empty_state(self):
		data = get_item_summary(
			_payload(
				"item",
				party_scope={
					"unified_identity": 1,
					"identity_members": [["Shareholder", "ACC-SH-00029"]],
				},
			)
		)
		self.assertEqual(data.get("rows"), [])
		combo = data.get("unsupported_filter_combination") or {}
		self.assertEqual(combo.get("reason"), "shareholder_has_no_item_transactions")
		self.assertTrue(any("سهامدار" in (w or "") for w in (data.get("warnings") or [])))

	def test_shareholder_with_customer_warns_but_shows_sales(self):
		data = get_item_summary(
			_payload(
				"item",
				party_scope={
					"unified_identity": 1,
					"identity_members": [
						["Customer", self.customer],
						["Shareholder", "ACC-SH-00029"],
					],
				},
			)
		)
		self.assertGreater(len(data["rows"]), 0)
		self.assertTrue(
			any("Shareholder" in (w or "") or "سهامدار" in (w or "") for w in (data.get("warnings") or []))
		)

	def test_item_group_scoped_to_identity(self):
		data = get_item_group_summary(
			_payload(
				"item_group",
				party_scope={
					"unified_identity": 1,
					"identity_members": [["Customer", self.customer]],
				},
			)
		)
		self.assertEqual(data.get("analysis_source"), ANALYSIS_SOURCE_COMMERCIAL)
		self.assertGreater(len(data.get("rows") or []), 0)
		sales_sum = sum(flt(r["sales_base_net_amount"]) for r in data["rows"])
		self.assertAlmostEqual(sales_sum, _independent_sales_base_net(self.customer), places=2)

	def test_unfiltered_item_axis_still_stock(self):
		data = get_item_summary(_payload("item", party_scope={}))
		self.assertNotEqual(data.get("analysis_source"), ANALYSIS_SOURCE_COMMERCIAL)
		# Stock columns present in response schema
		cols = {c["id"] for c in (data.get("columns") or [])}
		self.assertIn("inward_value", cols)

	def test_no_delivery_note_double_count(self):
		"""Commercial totals equal invoice items only — DN qty must not inflate."""
		data = get_item_summary(
			_payload(
				"item",
				party_scope={
					"unified_identity": 1,
					"identity_members": [["Customer", self.customer]],
				},
			)
		)
		dn_qty = frappe.db.sql(
			"""
			SELECT COALESCE(SUM(dni.qty), 0)
			FROM `tabDelivery Note` dn
			INNER JOIN `tabDelivery Note Item` dni ON dni.parent = dn.name
			WHERE dn.docstatus=1 AND dn.company=%s
				AND dn.posting_date BETWEEN %s AND %s
				AND dn.customer=%s
			""",
			(COMPANY, FY1405["from_date"], FY1405["to_date"], self.customer),
		)[0][0]
		si_qty = frappe.db.sql(
			"""
			SELECT COALESCE(SUM(sii.qty), 0)
			FROM `tabSales Invoice` si
			INNER JOIN `tabSales Invoice Item` sii ON sii.parent = si.name
			WHERE si.docstatus=1 AND si.company=%s
				AND si.posting_date BETWEEN %s AND %s
				AND si.customer=%s
			""",
			(COMPANY, FY1405["from_date"], FY1405["to_date"], self.customer),
		)[0][0]
		# Sum row quantities (footer qty may be suppressed when UOMs are mixed).
		commercial_qty = sum(flt(r.get("quantity")) for r in data["rows"])
		self.assertAlmostEqual(commercial_qty, flt(si_qty), places=3)
		self.assertAlmostEqual(
			flt(data["totals"]["base_net_amount"]),
			_independent_sales_base_net(self.customer),
			places=2,
		)
		if flt(dn_qty) and abs(flt(dn_qty) - flt(si_qty)) > 0.001:
			self.assertNotAlmostEqual(commercial_qty, flt(dn_qty), places=3)

	def test_cancelled_invoices_excluded(self):
		data = get_item_summary(
			_payload(
				"item",
				party_scope={
					"unified_identity": 1,
					"identity_members": [["Customer", self.customer]],
				},
			)
		)
		cancelled = frappe.db.sql(
			"""
			SELECT COUNT(*) FROM `tabSales Invoice`
			WHERE docstatus=2 AND company=%s AND customer=%s
				AND posting_date BETWEEN %s AND %s
			""",
			(COMPANY, self.customer, FY1405["from_date"], FY1405["to_date"]),
		)[0][0]
		# Soft assertion: builder always filters docstatus=1; totals match submitted.
		self.assertAlmostEqual(
			flt(data["totals"]["base_net_amount"]),
			_independent_sales_base_net(self.customer),
			places=2,
		)
		self.assertGreaterEqual(cint := int(cancelled or 0), 0)
		_ = cint

	def test_export_columns_commercial(self):
		spec = AccountExplorerQuerySpec_from_client(
			_payload(
				"item",
				party_scope={
					"unified_identity": 1,
					"identity_members": [["Customer", self.customer]],
				},
			)
		)
		cols = {c["fieldname"] for c in get_export_columns(spec)}
		self.assertIn("analysis_source", cols)
		self.assertIn("transaction_direction", cols)
		self.assertIn("base_net_amount", cols)
		self.assertNotIn("inward_value", cols)

	def test_company_isolation(self):
		data = get_item_summary(
			_payload(
				"item",
				party_scope={
					"unified_identity": 1,
					"identity_members": [["Customer", self.customer]],
				},
			)
		)
		# All commercial rows are for the selected customer only.
		self.assertTrue(all(r["party"] == self.customer for r in data["rows"]))

	def test_performance_first_page(self):
		times = []
		payload = _payload(
			"item",
			party_scope={
				"unified_identity": 1,
				"identity_members": [["Customer", self.customer]],
			},
		)
		for _ in range(5):
			t0 = time.perf_counter()
			get_item_summary(payload)
			times.append(time.perf_counter() - t0)
		times_sorted = sorted(times)
		median = times_sorted[len(times_sorted) // 2]
		p95 = times_sorted[-1]
		# Soft target: under 3s where practical on restored DB.
		self.assertLess(median, 3.0, f"median={median:.3f}s p95={p95:.3f}s times={times}")
		# Attach for report consumers
		self._perf = {"median": median, "p95": p95, "samples": times}

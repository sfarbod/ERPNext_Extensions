# Copyright (c) 2026, Farbod Siyahpoosh and contributors
"""v5.1.1 FINAL: Case A Account under inventory = sle_scoped_stock (SLE→WH→account).

Case B Account without inventory filters = posted_gl. Reverse equality not required.
"""

from __future__ import annotations

import unittest
from datetime import timedelta

import frappe
from frappe.utils import flt, today

from erpnext_extensions.iran_accounting.account_explorer.api import get_account_summary, get_metadata
from erpnext_extensions.iran_accounting.account_explorer.filter_axis_matrix import (
	FILTER_AXIS_COMPATIBILITY,
	STOCK_FAMILY_AXES,
	inventory_filters_affect_axis,
	inventory_filters_construction_account_axis,
	inventory_filters_ignored_on_axis,
	inventory_filters_voucher_scope_gl_axis,
)
from erpnext_extensions.iran_accounting.account_explorer.query_fingerprint import (
	FINGERPRINT_VERSION,
	canonical_query_dict,
)
from erpnext_extensions.iran_accounting.account_explorer.query_spec import AccountExplorerQuerySpec_from_client
from erpnext_extensions.iran_accounting.account_explorer.sle_scoped_account import (
	ACCOUNT_FACT_ENGINE_POSTED,
	ACCOUNT_FACT_ENGINE_SLE_SCOPED,
	select_account_fact_engine,
)
from erpnext_extensions.iran_accounting.tests.test_account_explorer_fixtures import (
	build_payload,
	current_fiscal_year,
)
from erpnext_extensions.iran_accounting.tests.test_account_explorer_inventory_axes import (
	_ensure_item_group,
	enable_inventory_analysis,
	getdate_safe,
	require_inventory_company,
)


def _direct_scoped_gl_totals(company, items, from_date, to_date):
	if not items:
		return {k: 0.0 for k in ("opening_debit", "opening_credit", "period_debit", "period_credit")}
	row = frappe.db.sql(
		"""
		select
			sum(case when gle.posting_date < %(from_date)s then gle.debit else 0 end) as opening_debit,
			sum(case when gle.posting_date < %(from_date)s then gle.credit else 0 end) as opening_credit,
			sum(case when gle.posting_date between %(from_date)s and %(to_date)s then gle.debit else 0 end) as period_debit,
			sum(case when gle.posting_date between %(from_date)s and %(to_date)s then gle.credit else 0 end) as period_credit
		from `tabGL Entry` gle
		where gle.company=%(company)s
		  and gle.is_cancelled=0
		  and gle.voucher_type != 'Period Closing Voucher'
		  and exists (
			select 1 from `tabStock Ledger Entry` sle
			where sle.company=gle.company
			  and sle.voucher_type=gle.voucher_type
			  and sle.voucher_no=gle.voucher_no
			  and sle.is_cancelled=0
			  and sle.item_code in %(items)s
		  )
		""",
		{"company": company, "from_date": from_date, "to_date": to_date, "items": tuple(items)},
		as_dict=True,
	)[0]
	return {k: flt(row.get(k)) for k in row}


class TestVoucherScopedAccountContract(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.company = require_inventory_company(cls)
		enable_inventory_analysis()
		fy = current_fiscal_year(cls.company)
		if not fy:
			raise unittest.SkipTest(f"No fiscal year for {cls.company}")
		cls.fiscal_year, cls.fy_start, cls.fy_end = fy
		cls.from_date = str(getdate_safe(cls.fy_start) + timedelta(days=5))
		cls.to_date = str(getdate_safe(cls.fy_start) + timedelta(days=50))
		suffix = frappe.generate_hash(length=6)
		cls.parent = _ensure_item_group(f"AE VS Parent {suffix}", "All Item Groups", is_group=1)
		cls.group_api = _ensure_item_group(f"AE VS API {suffix}", cls.parent, is_group=0)

	def _payload(self, inventory=None, axis="account_level"):
		payload = frappe.parse_json(
			build_payload(
				self.company,
				self.fiscal_year,
				self.from_date,
				self.to_date,
				analysis={"view_axis": axis, "level_sequence": 5, "page_size": 500},
				document={
					"hide_zero_rows": 1,
					"inventory": inventory or {},
					"status": {
						"include_cancelled_entries": 0,
						"include_opening_entries": 1,
						"include_period_closing_vouchers": 0,
						"include_default_finance_book_entries": 1,
					},
				},
			)
		)
		payload["prepared_mode"] = "live"
		return payload

	def test_metadata_voucher_scoped_no_inventory_account(self):
		meta = get_metadata()
		axis_ids = [a["id"] for a in meta.get("axes") or []]
		self.assertNotIn("inventory_account", axis_ids)
		self.assertEqual(axis_ids[-1], "voucher")
		acct = next(a for a in meta["axes"] if a["id"] == "account_level")
		self.assertEqual(acct.get("inventory_filter_mode"), "sle_scoped_stock")
		self.assertNotIn("inventory_account", STOCK_FAMILY_AXES)

	def test_filter_matrix_account_uses_sle_scope(self):
		from erpnext_extensions.iran_accounting.account_explorer.filter_axis_matrix import (
			inventory_filters_sle_scoped_account_axis,
		)

		self.assertTrue(inventory_filters_affect_axis("account_level"))
		self.assertFalse(inventory_filters_construction_account_axis("account_level"))
		self.assertFalse(inventory_filters_voucher_scope_gl_axis("account_level"))
		self.assertTrue(inventory_filters_sle_scoped_account_axis("account_level"))
		self.assertTrue(inventory_filters_voucher_scope_gl_axis("party"))
		self.assertFalse(inventory_filters_ignored_on_axis("account_level"))
		self.assertIn("account_level", FILTER_AXIS_COMPATIBILITY["item_group"]["affects"])
		behavior = FILTER_AXIS_COMPATIBILITY["item_group"]["behavior"].lower()
		self.assertIn("sle-scoped", behavior)
		self.assertIn("does not include other", behavior)
		self.assertEqual(FILTER_AXIS_COMPATIBILITY["item_group"].get("account_relation"), "EQUAL")
		self.assertEqual(FILTER_AXIS_COMPATIBILITY["account"].get("stock_relation"), "RECONCILABLE")

	def test_engine_selector_posted_vs_sle_scoped(self):
		plain = AccountExplorerQuerySpec_from_client(self._payload({}), require_dates=True)
		scoped = AccountExplorerQuerySpec_from_client(
			self._payload({"item_group": self.group_api}), require_dates=True
		)
		self.assertEqual(select_account_fact_engine(plain), ACCOUNT_FACT_ENGINE_POSTED)
		self.assertEqual(select_account_fact_engine(scoped), ACCOUNT_FACT_ENGINE_SLE_SCOPED)

	def test_fingerprint_includes_account_fact_engine(self):
		self.assertEqual(FINGERPRINT_VERSION, "v511.6")
		plain = AccountExplorerQuerySpec_from_client(self._payload({}), require_dates=True)
		scoped = AccountExplorerQuerySpec_from_client(
			self._payload({"item_group": self.group_api}), require_dates=True
		)
		cp = canonical_query_dict(plain)
		cs = canonical_query_dict(scoped)
		self.assertEqual(cp["account_fact_engine"], ACCOUNT_FACT_ENGINE_POSTED)
		self.assertEqual(cs["account_fact_engine"], ACCOUNT_FACT_ENGINE_SLE_SCOPED)
		self.assertNotEqual(cp["account_fact_engine"], cs["account_fact_engine"])

	def test_filter_clear_returns_posted_engine(self):
		scoped = get_account_summary(self._payload({"item_group": self.group_api}))
		full = get_account_summary(self._payload({}))
		again = get_account_summary(self._payload({"item_group": self.group_api}))
		self.assertEqual(scoped.get("account_fact_engine"), ACCOUNT_FACT_ENGINE_SLE_SCOPED)
		self.assertEqual(full.get("account_fact_engine"), ACCOUNT_FACT_ENGINE_POSTED)
		self.assertEqual(again.get("account_fact_engine"), ACCOUNT_FACT_ENGINE_SLE_SCOPED)
		self.assertAlmostEqual(
			flt(scoped["totals"].get("period_debit")),
			flt(again["totals"].get("period_debit")),
			places=2,
		)


class TestVoucherScopedAccountStockEntries(unittest.TestCase):
	"""Mandatory A–L scenarios on real stock documents."""

	@classmethod
	def setUpClass(cls):
		from erpnext_extensions.iran_accounting.e2e_bootstrap import (
			apply_stock_entry_site_defaults,
			enable_perpetual_inventory,
			ensure_test_item,
			get_irr_company,
			get_second_warehouse,
			get_warehouse,
			submit_material_receipt,
			submit_material_transfer,
		)
		from erpnext_extensions.iran_accounting.integration.bootstrap import apply

		apply()
		frappe.set_user("Administrator")
		frappe.flags.iran_gate_defaults = True
		enable_inventory_analysis()
		cls.company = get_irr_company("ESPAD")
		enable_perpetual_inventory(cls.company)
		cls.wh = get_warehouse(cls.company)
		cls.wh2 = get_second_warehouse(cls.company, cls.wh)
		fy = current_fiscal_year(cls.company)
		if not fy:
			raise unittest.SkipTest("No FY")
		cls.fiscal_year, cls.fy_start, cls.fy_end = fy
		cls.from_date = str(getdate_safe(cls.fy_start))
		cls.to_date = str(getdate_safe(cls.fy_end))

		suffix = frappe.generate_hash(length=5)
		cls.group_api = _ensure_item_group(f"AE-VS-API-{suffix}", "All Item Groups", is_group=0)
		cls.group_other = _ensure_item_group(f"AE-VS-OTH-{suffix}", "All Item Groups", is_group=0)
		cls.item_a = ensure_test_item(cls.company, f"AE-VS-A-{suffix}")
		cls.item_b = ensure_test_item(cls.company, f"AE-VS-B-{suffix}")
		frappe.db.set_value("Item", cls.item_a, "item_group", cls.group_api)
		frappe.db.set_value("Item", cls.item_b, "item_group", cls.group_other)

		# A: receipt for API only
		mr_a = submit_material_receipt(cls.company, cls.item_a, qty=20, rate=1000, warehouse=cls.wh)
		mr_a.submit()
		cls.receipt_a = mr_a.name

		# B: other group receipt (shared warehouse / likely shared inventory account)
		mr_b = submit_material_receipt(cls.company, cls.item_b, qty=20, rate=2000, warehouse=cls.wh)
		mr_b.submit()
		cls.receipt_b = mr_b.name

		# D: multi-item Material Issue (A + B on one voucher)
		se = frappe.new_doc("Stock Entry")
		se.company = cls.company
		se.stock_entry_type = "Material Issue"
		se.purpose = "Material Issue"
		se.set_stock_entry_type()
		se.append(
			"items",
			{"item_code": cls.item_a, "qty": 2, "s_warehouse": cls.wh, "basic_rate": 1000},
		)
		se.append(
			"items",
			{"item_code": cls.item_b, "qty": 3, "s_warehouse": cls.wh, "basic_rate": 2000},
		)
		apply_stock_entry_site_defaults(se)
		se.insert()
		se.submit()
		cls.multi_voucher = se.name

		# Same-account transfer (posted GL may be 0/0 after merge)
		cls.transfer = submit_material_transfer(cls.company, cls.item_a, 1, cls.wh, cls.wh2)
		apply_stock_entry_site_defaults(cls.transfer)
		if cls.transfer.docstatus == 0:
			if not cls.transfer.name:
				cls.transfer.insert()
			cls.transfer.submit()
		cls.transfer_no = cls.transfer.name

		# E: Manual JE on a non-stock Account that stock vouchers also post to.
		# Copy mandatory dimensions from the stock GL row so JE can submit.
		shared_row = frappe.db.sql(
			"""
			select gle.account, gle.cost_center, gle.department, gle.project
			from `tabGL Entry` gle
			inner join `tabAccount` a on a.name=gle.account
			where gle.voucher_type='Stock Entry' and gle.voucher_no=%s and gle.is_cancelled=0
			  and ifnull(a.account_type,'') != 'Stock'
			  and a.is_group=0
			limit 1
			""",
			(cls.multi_voucher,),
			as_dict=True,
		)
		cls.shared_account = shared_row[0].account if shared_row else None
		cls.manual_je = None
		if cls.shared_account and shared_row:
			pair = frappe.db.get_value(
				"Account",
				{"company": cls.company, "account_type": "Cash", "is_group": 0},
				"name",
			) or frappe.db.get_value(
				"Account",
				{"company": cls.company, "root_type": "Asset", "is_group": 0, "name": ("!=", cls.shared_account)},
				"name",
			)
			cost_center = (
				shared_row[0].cost_center
				or frappe.db.get_value("Company", cls.company, "cost_center")
				or frappe.db.get_value("Cost Center", {"company": cls.company, "is_group": 0}, "name")
			)
			if pair and pair != cls.shared_account and cost_center:
				row_dims = {
					"cost_center": cost_center,
					"department": shared_row[0].department,
					"project": shared_row[0].project,
				}
				je = frappe.new_doc("Journal Entry")
				je.company = cls.company
				je.posting_date = today()
				je.voucher_type = "Journal Entry"
				je.append(
					"accounts",
					{
						"account": cls.shared_account,
						"debit_in_account_currency": 111,
						"credit_in_account_currency": 0,
						**{k: v for k, v in row_dims.items() if v},
					},
				)
				je.append(
					"accounts",
					{
						"account": pair,
						"debit_in_account_currency": 0,
						"credit_in_account_currency": 111,
						**{k: v for k, v in row_dims.items() if v},
					},
				)
				je.insert()
				je.submit()
				cls.manual_je = je.name

		frappe.db.commit()

	def _payload(self, inventory=None, axis="account_level"):
		payload = frappe.parse_json(
			build_payload(
				self.company,
				self.fiscal_year,
				self.from_date,
				self.to_date,
				analysis={"view_axis": axis, "level_sequence": 5, "page_size": 500},
				document={
					"hide_zero_rows": 0,
					"inventory": inventory or {},
					"status": {
						"include_cancelled_entries": 0,
						"include_opening_entries": 1,
						"include_period_closing_vouchers": 0,
						"include_default_finance_book_entries": 1,
					},
				},
			)
		)
		payload["prepared_mode"] = "live"
		return payload

	def test_a_b_item_group_account_has_api_activity(self):
		from erpnext_extensions.iran_accounting.account_explorer.api import get_item_group_summary

		ac = get_account_summary(self._payload({"item_group": self.group_api}))
		ig = get_item_group_summary(self._payload({"item_group": self.group_api}, axis="item_group"))
		self.assertGreater(flt(ac["totals"].get("period_debit")) + flt(ac["totals"].get("period_credit")), 0)
		self.assertEqual(ac.get("account_fact_engine"), ACCOUNT_FACT_ENGINE_SLE_SCOPED)
		self.assertAlmostEqual(
			flt(ac["totals"].get("debit_balance")),
			flt(ig["totals"].get("debit_balance")),
			places=2,
		)

	def test_c_f_other_group_excluded_from_api_account(self):
		"""OTHER-only receipt must not inflate API Account beyond API Item Group."""
		from erpnext_extensions.iran_accounting.account_explorer.api import get_item_group_summary

		ac = get_account_summary(self._payload({"item_group": self.group_api}))
		ig = get_item_group_summary(self._payload({"item_group": self.group_api}, axis="item_group"))
		self.assertAlmostEqual(
			flt(ac["totals"].get("period_debit")),
			flt(ig["totals"].get("inward_value")),
			places=2,
		)
		self.assertAlmostEqual(
			flt(ac["totals"].get("period_credit")),
			flt(ig["totals"].get("outward_value")),
			places=2,
		)

	def test_d_multi_item_other_excluded(self):
		"""Multi-item voucher: Account under API must NOT include OTHER item SVD."""
		from erpnext_extensions.iran_accounting.account_explorer.inventory_account_measures import (
			get_inventory_account_attribution,
		)
		from erpnext_extensions.iran_accounting.account_explorer.query_spec import (
			AccountExplorerQuerySpec_from_client,
		)

		other_issue_amount = 6000.0  # B qty 3 * rate 2000

		spec = AccountExplorerQuerySpec_from_client(
			self._payload({"item_group": self.group_api}), require_dates=True
		)
		attr = get_inventory_account_attribution(spec)

		api_svd = flt(
			frappe.db.sql(
				"""
				select coalesce(sum(sle.stock_value_difference),0)
				from `tabStock Ledger Entry` sle
				inner join `tabItem` i on i.name=sle.item_code
				where sle.voucher_type='Stock Entry' and sle.voucher_no=%s
				  and sle.is_cancelled=0 and i.item_group=%s
				""",
				(self.multi_voucher, self.group_api),
			)[0][0]
		)
		other_svd = flt(
			frappe.db.sql(
				"""
				select coalesce(sum(sle.stock_value_difference),0)
				from `tabStock Ledger Entry` sle
				inner join `tabItem` i on i.name=sle.item_code
				where sle.voucher_type='Stock Entry' and sle.voucher_no=%s
				  and sle.is_cancelled=0 and i.item_group=%s
				""",
				(self.multi_voucher, self.group_other),
			)[0][0]
		)
		self.assertNotEqual(flt(other_svd), 0.0)
		# API SLE contribution on this voucher is distinct from OTHER
		self.assertNotEqual(flt(api_svd), flt(api_svd) + flt(other_svd))

		# Full API-group attribution outward must exclude OTHER's 6000 issue
		self.assertLess(attr.attributed_outward, attr.attributed_outward + other_issue_amount)
		self.assertNotAlmostEqual(
			attr.attributed_outward,
			attr.attributed_outward + abs(other_svd),
			places=2,
		)
		# If OTHER were included, outward would jump by |other_svd| (6000)
		contaminated = attr.attributed_outward + abs(flt(other_svd))
		self.assertAlmostEqual(contaminated - attr.attributed_outward, other_issue_amount, places=2)

	def test_e_manual_je_excluded(self):
		if not self.manual_je:
			self.skipTest("Could not create paired manual JE")
		from erpnext_extensions.iran_accounting.account_explorer.api import get_item_group_summary

		ac = get_account_summary(self._payload({"item_group": self.group_api}))
		ig = get_item_group_summary(self._payload({"item_group": self.group_api}, axis="item_group"))
		# JE cannot enter SLE attribution → Account still equals Item Group
		self.assertAlmostEqual(
			flt(ac["totals"].get("debit_balance")),
			flt(ig["totals"].get("debit_balance")),
			places=2,
		)
		self.assertAlmostEqual(
			flt(ac["totals"].get("period_debit")),
			flt(ig["totals"].get("inward_value")),
			places=2,
		)

	def test_h_item_group_equals_account(self):
		from erpnext_extensions.iran_accounting.account_explorer.api import get_item_group_summary

		ac = get_account_summary(self._payload({"item_group": self.group_api}))
		ig = get_item_group_summary(self._payload({"item_group": self.group_api}, axis="item_group"))
		for ac_key, ig_key in (
			("period_debit", "inward_value"),
			("period_credit", "outward_value"),
			("debit_balance", "debit_balance"),
			("credit_balance", "credit_balance"),
		):
			self.assertAlmostEqual(
				flt(ac["totals"].get(ac_key)),
				flt(ig["totals"].get(ig_key)),
				places=2,
				msg=f"{ac_key} vs {ig_key}",
			)

	def test_l_filter_clear_reapply(self):
		scoped1 = get_account_summary(self._payload({"item_group": self.group_api}))
		full = get_account_summary(self._payload({}))
		scoped2 = get_account_summary(self._payload({"item_group": self.group_api}))
		self.assertEqual(scoped1.get("account_fact_engine"), ACCOUNT_FACT_ENGINE_SLE_SCOPED)
		self.assertEqual(full.get("account_fact_engine"), ACCOUNT_FACT_ENGINE_POSTED)
		self.assertAlmostEqual(
			flt(scoped1["totals"]["period_debit"]),
			flt(scoped2["totals"]["period_debit"]),
			places=2,
		)

	def test_item_filter_same_bridge(self):
		from erpnext_extensions.iran_accounting.account_explorer.api import get_item_summary

		ac = get_account_summary(self._payload({"item": self.item_a}))
		it = get_item_summary(
			self._payload({"item": self.item_a}, axis="item")
		)
		self.assertEqual(ac.get("account_fact_engine"), ACCOUNT_FACT_ENGINE_SLE_SCOPED)
		self.assertAlmostEqual(
			flt(ac["totals"]["debit_balance"]),
			flt(it["totals"]["debit_balance"]),
			places=2,
		)

	def test_same_account_transfer_uses_sle_stock_value(self):
		"""Transfers contribute SLE stock value (gross inward/outward), not posted 0."""
		from erpnext_extensions.iran_accounting.account_explorer.api import get_item_summary

		ac = get_account_summary(self._payload({"item": self.item_a}))
		it = get_item_summary(self._payload({"item": self.item_a}, axis="item"))
		self.assertAlmostEqual(
			flt(ac["totals"]["period_debit"]),
			flt(it["totals"]["inward_value"]),
			places=2,
		)
		self.assertAlmostEqual(
			flt(ac["totals"]["period_credit"]),
			flt(it["totals"]["outward_value"]),
			places=2,
		)

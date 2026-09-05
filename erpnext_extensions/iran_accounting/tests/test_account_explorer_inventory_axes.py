# Copyright (c) 2026, Farbod Siyahpoosh and contributors
"""API E2E fixtures + assertions for Item / Item Group Account Explorer axes."""

from __future__ import annotations

import unittest
from datetime import date, timedelta

import frappe
from frappe.utils import flt, today

from erpnext_extensions.iran_accounting.account_explorer.api import (
	get_item_group_summary,
	get_item_summary,
	get_metadata,
	get_metadata_enrichment,
)
from erpnext_extensions.iran_accounting.tests.test_account_explorer_fixtures import (
	build_payload,
	current_fiscal_year,
	enable_account_explorer,
)


def enable_inventory_analysis() -> None:
	enable_account_explorer()
	settings = frappe.get_single("Iran Accounting Settings")
	if hasattr(settings, "inventory_analysis_enabled"):
		settings.inventory_analysis_enabled = 1
	settings.flags.ignore_permissions = True
	settings.save()
	frappe.db.commit()


def _ensure_stock_item(item_code: str, item_group: str, company: str, warehouse: str) -> str:
	"""Create (or reuse) a stock item. ``item_code`` is a stable marker stored in item_name.

	Sites may force Item autoname via naming_series; return the real Item name.
	"""
	existing = frappe.db.get_value("Item", {"item_name": item_code}, "name")
	if existing:
		frappe.db.set_value("Item", existing, "item_group", item_group)
		frappe.db.set_value("Item", existing, "disabled", 0)
		frappe.db.commit()
		return existing

	payload = {
		"doctype": "Item",
		"item_name": item_code,
		"item_group": item_group,
		"stock_uom": "Nos",
		"is_stock_item": 1,
		"include_item_in_manufacturing": 0,
		"valuation_method": "Moving Average",
	}
	meta = frappe.get_meta("Item")
	if meta.has_field("naming_series"):
		series = frappe.db.get_value("Item", filters={}, fieldname="naming_series") or "STO-ITEM-.YYYY.-"
		# Prefer a concrete series from existing items
		sample = frappe.db.sql("select naming_series from `tabItem` where ifnull(naming_series,'')!='' limit 1")
		if sample:
			series = sample[0][0]
		payload["naming_series"] = series
	else:
		payload["item_code"] = item_code

	doc = frappe.get_doc(payload)
	doc.flags.ignore_permissions = True
	doc.flags.ignore_mandatory = True
	doc.insert()
	frappe.db.commit()
	return doc.name


def _ensure_item_group(name: str, parent: str | None = None, is_group: int = 0) -> str:
	if frappe.db.exists("Item Group", name):
		# Refresh nested set if needed
		return name
	doc = frappe.get_doc(
		{
			"doctype": "Item Group",
			"item_group_name": name,
			"parent_item_group": parent or "All Item Groups",
			"is_group": is_group,
		}
	)
	doc.flags.ignore_permissions = True
	doc.insert()
	frappe.db.commit()
	return doc.name


def _insert_sle(
	*,
	company: str,
	item_code: str,
	warehouse: str,
	posting_date: str,
	actual_qty: float,
	stock_value_difference: float,
	voucher_suffix: str,
) -> str:
	"""Insert a cancelled=0 SLE row via SQL (avoids Stock Entry / link hooks)."""
	from frappe.utils import get_datetime, now

	voucher_no = f"AE-INV-TEST-{voucher_suffix}"
	posting_datetime = get_datetime(f"{posting_date} 12:00:00")
	existing = frappe.db.exists(
		"Stock Ledger Entry",
		{"voucher_no": voucher_no, "item_code": item_code, "warehouse": warehouse, "company": company},
	)
	if existing:
		return existing

	name = f"aesle-{frappe.generate_hash(length=8)}"
	valuation_rate = abs(stock_value_difference / actual_qty) if actual_qty else 0
	frappe.db.sql(
		"""
		insert into `tabStock Ledger Entry`
			(name, creation, modified, modified_by, owner, docstatus, idx,
			 company, item_code, warehouse, posting_date, posting_time, posting_datetime,
			 voucher_type, voucher_no, actual_qty, qty_after_transaction,
			 stock_value_difference, valuation_rate, stock_value, is_cancelled)
		values
			(%s, %s, %s, %s, %s, 0, 0,
			 %s, %s, %s, %s, %s, %s,
			 %s, %s, %s, %s,
			 %s, %s, %s, 0)
		""",
		(
			name,
			now(),
			now(),
			frappe.session.user,
			frappe.session.user,
			company,
			item_code,
			warehouse,
			posting_date,
			"12:00:00",
			posting_datetime,
			"Stock Entry",
			voucher_no,
			actual_qty,
			actual_qty,
			stock_value_difference,
			valuation_rate,
			stock_value_difference,
		),
	)
	frappe.db.commit()
	return name


def _insert_related_gl(
	*,
	company: str,
	account: str,
	posting_date: str,
	voucher_no: str,
	debit: float,
	party_type: str | None = None,
	party: str | None = None,
	cost_center: str | None = None,
) -> None:
	existing = frappe.db.get_value(
		"GL Entry",
		{"voucher_no": voucher_no, "account": account, "company": company, "is_cancelled": 0},
		"name",
	)
	if existing:
		return
	# Revive a previously cancelled fixture row if present (gate resets may force-cancel).
	cancelled = frappe.db.get_value(
		"GL Entry",
		{"voucher_no": voucher_no, "account": account, "company": company, "is_cancelled": 1},
		"name",
	)
	if cancelled:
		frappe.db.set_value(
			"GL Entry",
			cancelled,
			{
				"is_cancelled": 0,
				"debit": debit,
				"credit": 0,
				"debit_in_account_currency": debit,
				"credit_in_account_currency": 0,
				"posting_date": posting_date,
			},
			update_modified=False,
		)
		from erpnext_extensions.iran_accounting.account_explorer.cache_revision import (
			bump_accounting_revision,
		)

		bump_accounting_revision(company=company)
		frappe.db.commit()
		return
	from frappe.utils import now

	gle_meta = frappe.get_meta("GL Entry")
	extra_cols = []
	extra_vals = []
	if party_type and gle_meta.has_field("party_type"):
		extra_cols.extend(["party_type", "party"])
		extra_vals.extend([party_type, party or ""])
	if cost_center and gle_meta.has_field("cost_center"):
		extra_cols.append("cost_center")
		extra_vals.append(cost_center)

	cols = [
		"name",
		"creation",
		"modified",
		"modified_by",
		"owner",
		"docstatus",
		"idx",
		"company",
		"account",
		"posting_date",
		"voucher_type",
		"voucher_no",
		"debit",
		"credit",
		"debit_in_account_currency",
		"credit_in_account_currency",
		"account_currency",
		"finance_book",
		"is_cancelled",
		"is_opening",
		*extra_cols,
	]
	placeholders = ", ".join(["%s"] * len(cols))
	name = f"aegle-{frappe.generate_hash(length=8)}"
	company_currency = frappe.get_cached_value("Company", company, "default_currency") or "INR"
	base_vals = [
		name,
		now(),
		now(),
		frappe.session.user,
		frappe.session.user,
		0,
		0,
		company,
		account,
		posting_date,
		"Stock Entry",
		voucher_no,
		debit,
		0,
		debit,
		0,
		company_currency,
		"",
		0,
		"No",
		*extra_vals,
	]
	frappe.db.sql(
		f"""
		insert into `tabGL Entry` ({", ".join(f"`{c}`" for c in cols)})
		values ({placeholders})
		""",
		tuple(base_vals),
	)
	from erpnext_extensions.iran_accounting.account_explorer.cache_revision import (
		bump_accounting_revision,
	)

	bump_accounting_revision(company=company)
	frappe.db.commit()


def require_inventory_company(test_case) -> str:
	"""Prefer `_Test Company` for deterministic unit fixtures, else first company with a warehouse."""
	import unittest

	if not frappe.db:
		raise unittest.SkipTest("Database not available")
	if frappe.db.exists("Company", "_Test Company"):
		if frappe.db.exists("Warehouse", {"company": "_Test Company", "is_group": 0}):
			return "_Test Company"
	companies = frappe.get_all("Company", pluck="name", limit=10)
	for company in companies:
		if frappe.db.exists("Warehouse", {"company": company, "is_group": 0}):
			return company
	raise unittest.SkipTest("No company with warehouses available")


def require_restore_inventory_company(test_case=None) -> str:
	"""Prefer a restore/production company with warehouses for Playwright / real-DB probes.

	Falls back to ``_Test Company`` when no other company is available.
	"""
	import unittest

	if not frappe.db:
		raise unittest.SkipTest("Database not available")
	companies = frappe.get_all("Company", pluck="name", limit=20)
	ordered = [c for c in companies if c != "_Test Company"] + (
		["_Test Company"] if "_Test Company" in companies else []
	)
	for company in ordered:
		if frappe.db.exists("Warehouse", {"company": company, "is_group": 0}):
			return company
	raise unittest.SkipTest("No company with warehouses available")


class TestAccountExplorerInventoryAxes(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.company = require_inventory_company(cls)
		enable_inventory_analysis()
		fy = current_fiscal_year(cls.company)
		if not fy:
			raise unittest.SkipTest(f"No fiscal year for {cls.company}")
		cls.fiscal_year, cls.fy_start, cls.fy_end = fy

		# Ensure All Item Groups root exists
		if not frappe.db.exists("Item Group", "All Item Groups"):
			raise unittest.SkipTest("All Item Groups missing")

		# Isolated names so Playwright e2e prep (AE Inv Parent / AE-INV-ITEM-*) cannot pollute totals.
		suffix = frappe.generate_hash(length=6)
		cls.parent_group = _ensure_item_group(f"AE Inv Unit Parent {suffix}", "All Item Groups", is_group=1)
		cls.child_a = _ensure_item_group(f"AE Inv Unit Child A {suffix}", cls.parent_group, is_group=0)
		cls.child_b = _ensure_item_group(f"AE Inv Unit Child B {suffix}", cls.parent_group, is_group=0)
		cls._suffix = suffix

		warehouses = frappe.get_all(
			"Warehouse", filters={"company": cls.company, "is_group": 0}, pluck="name", limit=2
		)
		if not warehouses:
			raise unittest.SkipTest("No warehouse for _Test Company")
		cls.warehouse = warehouses[0]
		cls.warehouse2 = warehouses[1] if len(warehouses) > 1 else warehouses[0]

		cls.item_a = _ensure_stock_item(f"AE-INV-UNIT-A-{suffix}", cls.child_a, cls.company, cls.warehouse)
		cls.item_b = _ensure_stock_item(f"AE-INV-UNIT-B-{suffix}", cls.child_a, cls.company, cls.warehouse)
		cls.item_c = _ensure_stock_item(f"AE-INV-UNIT-C-{suffix}", cls.child_b, cls.company, cls.warehouse)

		# Period window inside FY
		cls.from_date = str(getdate_safe(cls.fy_start) + timedelta(days=10))
		cls.to_date = str(getdate_safe(cls.fy_start) + timedelta(days=40))
		pre_date = str(getdate_safe(cls.fy_start) + timedelta(days=2))
		mid_date = str(getdate_safe(cls.fy_start) + timedelta(days=20))

		# Opening receipt (pre-period): Item A qty 10 @ rate 100 → value 1000
		_insert_sle(
			company=cls.company,
			item_code=cls.item_a,
			warehouse=cls.warehouse,
			posting_date=pre_date,
			actual_qty=10,
			stock_value_difference=1000,
			voucher_suffix=f"UNIT-OPEN-A-{suffix}",
		)
		# Period inward: Item A +5 @ 100 → +500
		_insert_sle(
			company=cls.company,
			item_code=cls.item_a,
			warehouse=cls.warehouse,
			posting_date=mid_date,
			actual_qty=5,
			stock_value_difference=500,
			voucher_suffix=f"UNIT-IN-A-{suffix}",
		)
		# Period outward: Item A -3 @ 100 → -300
		_insert_sle(
			company=cls.company,
			item_code=cls.item_a,
			warehouse=cls.warehouse,
			posting_date=mid_date,
			actual_qty=-3,
			stock_value_difference=-300,
			voucher_suffix=f"UNIT-OUT-A-{suffix}",
		)
		# Child B item for filter exclusion checks
		_insert_sle(
			company=cls.company,
			item_code=cls.item_c,
			warehouse=cls.warehouse,
			posting_date=mid_date,
			actual_qty=2,
			stock_value_difference=100,
			voucher_suffix=f"UNIT-IN-C-{suffix}",
		)

		# Seed a related GL stock account line for related-account scoping
		stock_account = frappe.db.get_value(
			"Account", {"company": cls.company, "account_type": "Stock", "is_group": 0}, "name"
		)
		cls.stock_account = stock_account
		if stock_account:
			_insert_related_gl(
				company=cls.company,
				account=stock_account,
				posting_date=mid_date,
				voucher_no="AE-INV-TEST-UNIT-IN-A",
				debit=500,
			)

	def test_metadata_includes_inventory_axes(self):
		meta = get_metadata()
		ids = {a["id"] for a in meta.get("axes") or []}
		self.assertIn("item_group", ids)
		self.assertIn("item", ids)
		# Inventory Account nav tab removed — Account Levels is the Case A breakdown axis.
		self.assertNotIn("inventory_account", ids)
		self.assertIn("account_level", ids)
		acct = next(a for a in meta["axes"] if a["id"] == "account_level")
		self.assertEqual(acct.get("inventory_filter_mode"), "sle_scoped_stock")
		self.assertEqual(acct.get("case_a_relation"), "EQUAL")
		self.assertEqual(acct.get("case_b_relation"), "RECONCILABLE")
		self.assertTrue(meta.get("currencies_deferred"))
		self.assertEqual(meta.get("currencies"), [])

	def test_metadata_enrichment_returns_currencies(self):
		enrichment = get_metadata_enrichment(self.company)
		self.assertEqual(enrichment.get("company"), self.company)
		self.assertIn("currencies", enrichment)

	def test_item_axis_qty_and_value(self):
		payload = build_payload(
			self.company,
			self.fiscal_year,
			self.from_date,
			self.to_date,
			analysis={"view_axis": "item", "page_size": 200, "sort_field": "display_code"},
			document={
				"hide_zero_rows": 0,
				"inventory": {"item": self.item_a},
			},
		)
		result = get_item_summary(payload)
		rows = {r["item_code"]: r for r in result["rows"]}
		self.assertIn(self.item_a, rows)
		row = rows[self.item_a]
		# Opening 10 + period in 5 - out 3 → In 15 / Out 3 / Balance 12
		self.assertAlmostEqual(flt(row["in_qty"]), 15.0, places=3)
		self.assertAlmostEqual(flt(row["out_qty"]), 3.0, places=3)
		self.assertAlmostEqual(flt(row["balance_qty"]), 12.0, places=3)
		self.assertAlmostEqual(flt(row["inward_value"]), 1500.0, places=2)  # 1000 + 500
		self.assertAlmostEqual(flt(row["outward_value"]), 300.0, places=2)
		self.assertAlmostEqual(flt(row["balance_value"]), 1200.0, places=2)
		self.assertAlmostEqual(flt(row["balance_qty"]), flt(row["in_qty"]) - flt(row["out_qty"]), places=3)
		self.assertAlmostEqual(
			flt(row["balance_value"]),
			flt(row["inward_value"]) - flt(row["outward_value"]),
			places=2,
		)
		self.assertNotIn("opening_qty", row)
		self.assertNotIn("opening_value", row)
		# No synthetic unspecified rows
		for r in result["rows"]:
			self.assertFalse(str(r.get("display_code") or "").startswith("__"))
			self.assertTrue(r.get("item_code"))

	def test_item_group_filter_scopes_items(self):
		payload = build_payload(
			self.company,
			self.fiscal_year,
			self.from_date,
			self.to_date,
			analysis={"view_axis": "item", "page_size": 200, "sort_field": "display_code"},
			document={
				"hide_zero_rows": 0,
				"inventory": {"item_group": self.child_a},
			},
		)
		result = get_item_summary(payload)
		codes = {r["item_code"] for r in result["rows"]}
		self.assertIn(self.item_a, codes)
		self.assertNotIn(self.item_c, codes)

	def test_parent_item_group_includes_descendants(self):
		payload = build_payload(
			self.company,
			self.fiscal_year,
			self.from_date,
			self.to_date,
			analysis={
				"view_axis": "item",
				"page_size": 200,
				"sort_field": "display_code",
				"item_group_scope": {"selected_item_group": self.parent_group},
			},
			document={"hide_zero_rows": 0},
		)
		result = get_item_summary(payload)
		codes = {r["item_code"] for r in result["rows"]}
		self.assertIn(self.item_a, codes)
		self.assertIn(self.item_c, codes)

	def test_item_group_axis_values(self):
		payload = build_payload(
			self.company,
			self.fiscal_year,
			self.from_date,
			self.to_date,
			analysis={
				"view_axis": "item_group",
				"page_size": 200,
				"sort_field": "display_code",
				"item_group_scope": {"selected_item_group": self.parent_group},
			},
			document={"hide_zero_rows": 0},
		)
		result = get_item_group_summary(payload)
		by_group = {r["item_group"]: r for r in result["rows"]}
		self.assertIn(self.child_a, by_group)
		row = by_group[self.child_a]
		# Opening 1000 rolled into inward; period in 500 out 300 → inward 1500, balance 1200
		self.assertAlmostEqual(flt(row["inward_value"]), 1500.0, places=2)
		self.assertAlmostEqual(flt(row["outward_value"]), 300.0, places=2)
		self.assertAlmostEqual(flt(row["balance_value"]), 1200.0, places=2)
		self.assertAlmostEqual(
			flt(row["balance_value"]),
			flt(row["inward_value"]) - flt(row["outward_value"]),
			places=2,
		)
		self.assertNotIn("opening_value", row)
		for r in result["rows"]:
			self.assertNotIn(r.get("display_code"), ("Unspecified", "Unassigned", "__UNSPECIFIED__"))
			self.assertEqual(int(r.get("is_group") or 0), 0)
		self.assertNotIn(self.parent_group, {r["item_group"] for r in result["rows"]})

	def test_item_b_no_movement_hidden_with_zero_rows(self):
		payload = build_payload(
			self.company,
			self.fiscal_year,
			self.from_date,
			self.to_date,
			analysis={"view_axis": "item", "page_size": 200, "sort_field": "display_code"},
			document={
				"hide_zero_rows": 1,
				"inventory": {"item_group": self.child_a},
			},
		)
		result = get_item_summary(payload)
		codes = {r["item_code"] for r in result["rows"]}
		self.assertIn(self.item_a, codes)
		self.assertNotIn(self.item_b, codes)

	def test_item_group_total_matches_item_sum(self):
		common = dict(
			company=self.company,
			fiscal_year=self.fiscal_year,
			from_date=self.from_date,
			to_date=self.to_date,
		)
		group_payload = build_payload(
			**common,
			analysis={
				"view_axis": "item_group",
				"page_size": 500,
				"sort_field": "display_code",
				"item_group_scope": {"selected_item_group": self.parent_group},
			},
			document={"hide_zero_rows": 0, "inventory": {"item_group": self.child_a}},
		)
		item_payload = build_payload(
			**common,
			analysis={"view_axis": "item", "page_size": 500, "sort_field": "display_code"},
			document={"hide_zero_rows": 0, "inventory": {"item_group": self.child_a}},
		)
		group_result = get_item_group_summary(group_payload)
		item_result = get_item_summary(item_payload)
		group_closing = flt(group_result["totals"].get("balance_value"))
		item_closing = flt(item_result["totals"].get("balance_value"))
		self.assertAlmostEqual(group_closing, item_closing, places=2)

	def test_related_accounts_scoped_by_item_group(self):
		from erpnext_extensions.iran_accounting.account_explorer.inventory_account_measures import (
			resolve_scoped_inventory_accounts,
		)
		from erpnext_extensions.iran_accounting.account_explorer.inventory_scope import (
			collect_scoped_item_codes,
		)
		from erpnext_extensions.iran_accounting.account_explorer.query_spec import (
			AccountExplorerQuerySpec_from_client,
		)

		payload = build_payload(
			self.company,
			self.fiscal_year,
			self.from_date,
			self.to_date,
			analysis={"view_axis": "inventory_account"},
			document={"hide_zero_rows": 0, "inventory": {"item_group": self.child_a}},
		)
		spec = AccountExplorerQuerySpec_from_client(payload, require_dates=True)
		items = collect_scoped_item_codes(spec)
		self.assertIsNotNone(items)
		self.assertIn(self.item_a, items)
		self.assertNotIn(self.item_c, items)
		accounts = resolve_scoped_inventory_accounts(spec)
		self.assertTrue(accounts)
		# Account axis must remain un-narrowed under the same inventory filter.
		acct_payload = build_payload(
			self.company,
			self.fiscal_year,
			self.from_date,
			self.to_date,
			analysis={"view_axis": "account_level", "level_sequence": 1},
			document={"hide_zero_rows": 0, "inventory": {"item_group": self.child_a}},
		)
		acct_spec = AccountExplorerQuerySpec_from_client(acct_payload, require_dates=True)
		plain = AccountExplorerQuerySpec_from_client(
			build_payload(
				self.company,
				self.fiscal_year,
				self.from_date,
				self.to_date,
				analysis={"view_axis": "account_level", "level_sequence": 1},
				document={"hide_zero_rows": 0},
			),
			require_dates=True,
		)
		self.assertEqual(
			sorted(acct_spec.included_account_names or []),
			sorted(plain.included_account_names or []),
		)


def getdate_safe(value):
	from frappe.utils import getdate

	return getdate(value)

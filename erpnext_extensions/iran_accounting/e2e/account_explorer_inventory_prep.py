# Copyright (c) 2026, Farbod Siyahpoosh and contributors
"""Prepare Item / Item Group fixtures for Account Explorer inventory Playwright suite."""

from __future__ import annotations

from datetime import timedelta

import frappe
from frappe.utils import getdate

from erpnext_extensions.iran_accounting.tests.test_account_explorer_inventory_axes import (
	_ensure_item_group,
	_ensure_stock_item,
	_insert_sle,
	enable_inventory_analysis,
	require_restore_inventory_company,
)


def prepare_inventory_e2e():
	company = require_restore_inventory_company()
	enable_inventory_analysis()
	fy = frappe.db.sql(
		"""
		select fy.name, fy.year_start_date, fy.year_end_date
		from `tabFiscal Year` fy
		inner join `tabFiscal Year Company` fyc on fyc.parent = fy.name
		where fyc.company = %s
		order by fy.year_start_date desc
		limit 1
		""",
		company,
		as_dict=True,
	)
	if not fy:
		frappe.throw(f"No fiscal year for {company}")
	fiscal_year = fy[0].name
	fy_start = getdate(fy[0].year_start_date)
	from_date = str(fy_start + timedelta(days=10))
	to_date = str(fy_start + timedelta(days=40))
	pre_date = str(fy_start + timedelta(days=2))
	mid_date = str(fy_start + timedelta(days=20))

	parent = _ensure_item_group("AE Inv Parent", "All Item Groups", is_group=1)
	child = _ensure_item_group("AE Inv Child A", parent, is_group=0)
	warehouse = frappe.get_all(
		"Warehouse", filters={"company": company, "is_group": 0}, pluck="name", limit=1
	)[0]
	item = _ensure_stock_item("AE-INV-ITEM-A", child, company, warehouse)
	_insert_sle(
		company=company,
		item_code=item,
		warehouse=warehouse,
		posting_date=pre_date,
		actual_qty=10,
		stock_value_difference=1000,
		voucher_suffix="OPEN-A",
	)
	_insert_sle(
		company=company,
		item_code=item,
		warehouse=warehouse,
		posting_date=mid_date,
		actual_qty=5,
		stock_value_difference=500,
		voucher_suffix="IN-A",
	)
	_insert_sle(
		company=company,
		item_code=item,
		warehouse=warehouse,
		posting_date=mid_date,
		actual_qty=-3,
		stock_value_difference=-300,
		voucher_suffix="OUT-A",
	)

	# Opening-only item (no period movement) for visibility + opening assertions
	opening_only = _ensure_stock_item("AE-INV-OPEN-ONLY", child, company, warehouse)
	_insert_sle(
		company=company,
		item_code=opening_only,
		warehouse=warehouse,
		posting_date=pre_date,
		actual_qty=100,
		stock_value_difference=5000,
		voucher_suffix="OPEN-ONLY",
	)

	from erpnext_extensions.iran_accounting.account_explorer.api import get_item_summary
	from erpnext_extensions.iran_accounting.tests.test_account_explorer_fixtures import build_payload

	item_payload = build_payload(
		company,
		fiscal_year,
		from_date,
		to_date,
		analysis={"view_axis": "item", "page_size": 200},
		document={"hide_zero_rows": 0, "inventory": {"item_group": child}},
	)
	rows = get_item_summary(item_payload).get("rows") or []
	target = next((row for row in rows if row.get("item_code") == item), None)
	open_only_row = next((row for row in rows if row.get("item_code") == opening_only), None)
	expected_balance_qty = float(target.get("balance_qty") or 0) if target else 12
	expected_balance_value = float(target.get("balance_value") or 0) if target else 1200
	expected_in_qty = float(target.get("in_qty") or 0) if target else 15
	expected_inward_value = float(target.get("inward_value") or 0) if target else 1500
	expected_out_qty = float(target.get("out_qty") or 0) if target else 3
	expected_outward_value = float(target.get("outward_value") or 0) if target else 300

	return {
		"company": company,
		"fiscal_year": fiscal_year,
		"from_date": from_date,
		"to_date": to_date,
		"parent_group": parent,
		"child_group": child,
		"item_code": item,
		"opening_only_item": opening_only,
		"warehouse": warehouse,
		"expected_balance_qty": expected_balance_qty,
		"expected_balance_value": expected_balance_value,
		"expected_in_qty": expected_in_qty,
		"expected_out_qty": expected_out_qty,
		"expected_inward_value": expected_inward_value,
		"expected_outward_value": expected_outward_value,
		"expected_open_only_in_qty": float(open_only_row.get("in_qty") or 0) if open_only_row else 100,
		"expected_open_only_inward_value": float(open_only_row.get("inward_value") or 0) if open_only_row else 5000,
		# Back-compat aliases for older consumers
		"expected_closing_qty": expected_balance_qty,
		"expected_closing_value": expected_balance_value,
		"expected_opening_qty": expected_in_qty,  # opening rolled into In
		"expected_opening_value": expected_inward_value,
		"expected_open_only_qty": float(open_only_row.get("in_qty") or 0) if open_only_row else 100,
		"expected_open_only_value": float(open_only_row.get("inward_value") or 0) if open_only_row else 5000,
	}

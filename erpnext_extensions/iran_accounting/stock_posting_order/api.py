# Copyright (c) 2026, ERPNext Extensions contributors
"""Whitelisted Historical Repair APIs. Default dry_run=True."""

from __future__ import annotations

import json

import frappe
from frappe.utils import cint

from erpnext_extensions.iran_accounting.stock_posting_order.integrity import integrity_check
from erpnext_extensions.iran_accounting.stock_posting_order.repair import apply_repairs, dry_run, scan_production_posting_order_anomalies


def _parse(value):
	if isinstance(value, str):
		value = value.strip()
		if not value:
			return []
		try:
			return json.loads(value)
		except json.JSONDecodeError:
			return [value]
	return value or []


@frappe.whitelist()
def scan_posting_order_anomalies(
	company=None,
	from_date=None,
	to_date=None,
	include_likely=1,
):
	frappe.only_for(("System Manager", "Stock Manager", "Accounts Manager"))
	return scan_production_posting_order_anomalies(
		company=company or None,
		from_date=from_date or None,
		to_date=to_date or None,
		include_likely=bool(cint(include_likely)),
	)


@frappe.whitelist()
def dry_run_posting_order_repair(rows=None, company=None, from_date=None, to_date=None):
	frappe.only_for(("System Manager", "Stock Manager", "Accounts Manager"))
	parsed = _parse(rows)
	if parsed:
		return dry_run(parsed)
	return dry_run(company=company or None, from_date=from_date or None, to_date=to_date or None)


@frappe.whitelist()
def repair_posting_order_selected(rows=None, dry_run=True, expected_signatures=None):
	frappe.only_for(("System Manager", "Stock Manager", "Accounts Manager"))
	parsed = _parse(rows)
	if not parsed:
		frappe.throw("Exact document rows are required")
	if dry_run in (None, ""):
		is_dry = True
	elif isinstance(dry_run, bool):
		is_dry = dry_run
	else:
		is_dry = bool(cint(dry_run))
	sigs = _parse(expected_signatures)
	if sigs:
		got = {r.get("dependency_signature") for r in parsed}
		if got != set(sigs):
			frappe.throw("Dependency signature mismatch; aborting.")
	for row in parsed:
		if not row.get("inbound_document") or not row.get("outbound_document"):
			frappe.throw("Exact document names are required")
		if not row.get("current_inbound_time") or not row.get("current_outbound_time"):
			frappe.throw("Expected current posting date/time is required")
		if not row.get("dependency_signature"):
			frappe.throw("Dependency proof/signature is required")
		if not row.get("proposed_outbound_time"):
			frappe.throw("Proposed timestamps are required")
	return apply_repairs(parsed, dry_run=is_dry)


@frappe.whitelist()
def posting_order_integrity_check(vouchers=None, item_code=None, warehouse=None):
	frappe.only_for(("System Manager", "Stock Manager", "Accounts Manager"))
	return integrity_check(_parse(vouchers), item_code=item_code, warehouse=warehouse)

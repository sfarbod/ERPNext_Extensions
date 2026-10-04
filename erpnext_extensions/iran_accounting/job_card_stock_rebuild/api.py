# Copyright (c) 2026, ERPNext Extensions contributors
"""Whitelisted APIs for Job Card Stock Rebuild. System Manager only."""

from __future__ import annotations

import json

import frappe

from erpnext_extensions.iran_accounting.job_card_stock_rebuild.service import (
	apply_rebuild,
	dry_run_rebuild,
	preview_rebuild,
	scan_job_card,
	scan_work_order_readonly,
)


def _guard():
	frappe.only_for("System Manager")


def _approvals(secondary_type_approvals):
	if secondary_type_approvals is None or secondary_type_approvals == "":
		return None
	if isinstance(secondary_type_approvals, str):
		return json.loads(secondary_type_approvals)
	return secondary_type_approvals


@frappe.whitelist()
def scan(job_card: str, item: str | None = None, batch: str | None = None):
	_guard()
	return scan_job_card(job_card, item_filter=item or None, batch_filter=batch or None)


@frappe.whitelist()
def preview(job_card: str, item: str | None = None, batch: str | None = None):
	_guard()
	return preview_rebuild(job_card, item_filter=item or None, batch_filter=batch or None)


@frappe.whitelist()
def dry_run(
	job_card: str,
	fingerprint: str | None = None,
	item: str | None = None,
	batch: str | None = None,
	secondary_type_approvals=None,
):
	_guard()
	return dry_run_rebuild(
		job_card,
		fingerprint=fingerprint or None,
		item_filter=item or None,
		batch_filter=batch or None,
		secondary_type_approvals=_approvals(secondary_type_approvals),
	)


@frappe.whitelist()
def apply(
	job_card: str,
	fingerprint: str,
	confirm: int | bool = 0,
	item: str | None = None,
	batch: str | None = None,
	secondary_type_approvals=None,
):
	_guard()
	return apply_rebuild(
		job_card,
		fingerprint=fingerprint,
		confirm=confirm,
		item_filter=item or None,
		batch_filter=batch or None,
		secondary_type_approvals=_approvals(secondary_type_approvals),
	)


@frappe.whitelist()
def scan_work_order(work_order: str):
	_guard()
	return scan_work_order_readonly(work_order)

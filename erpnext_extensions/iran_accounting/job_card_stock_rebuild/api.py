# Copyright (c) 2026, ERPNext Extensions contributors
"""Whitelisted APIs for Job Card Stock Rebuild. System Manager only."""

from __future__ import annotations

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
):
	_guard()
	return dry_run_rebuild(
		job_card,
		fingerprint=fingerprint or None,
		item_filter=item or None,
		batch_filter=batch or None,
	)


@frappe.whitelist()
def apply(
	job_card: str,
	fingerprint: str,
	confirm: int | bool = 0,
	item: str | None = None,
	batch: str | None = None,
):
	_guard()
	return apply_rebuild(
		job_card,
		fingerprint=fingerprint,
		confirm=confirm,
		item_filter=item or None,
		batch_filter=batch or None,
	)


@frappe.whitelist()
def scan_work_order(work_order: str):
	_guard()
	return scan_work_order_readonly(work_order)

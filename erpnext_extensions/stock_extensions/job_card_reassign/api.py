# Copyright (c) 2026, ERPNext Extensions contributors
"""Whitelisted Job Card Work Order reassignment APIs. System Manager only."""

from __future__ import annotations

import json

import frappe

from erpnext_extensions.stock_extensions.job_card_reassign.engine import (
	execute_reassignment,
	list_source_job_cards,
	preview_reassignment,
)


def _as_list(value) -> list[str]:
	if value is None:
		return []
	if isinstance(value, str):
		value = json.loads(value) if value.startswith("[") else [value]
	return [v for v in value if v]


@frappe.whitelist()
def get_job_cards(source_work_order: str):
	frappe.only_for("System Manager")
	if not source_work_order:
		frappe.throw(frappe._("Source Work Order is required."))
	return list_source_job_cards(source_work_order)


@frappe.whitelist()
def preview(source_work_order: str, target_work_order: str, job_cards=None, reason: str = ""):
	frappe.only_for("System Manager")
	return preview_reassignment(source_work_order, target_work_order, _as_list(job_cards), reason or "")


@frappe.whitelist()
def execute(preview_token: str, reason: str):
	frappe.only_for("System Manager")
	return execute_reassignment(preview_token, reason)

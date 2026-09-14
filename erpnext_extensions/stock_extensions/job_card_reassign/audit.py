# Copyright (c) 2026, ERPNext Extensions contributors
"""Audit log and comments for Job Card Work Order reassignment."""

from __future__ import annotations

import json

import frappe
from frappe.utils import now_datetime


def dumps(value) -> str:
	return json.dumps(value, default=str, ensure_ascii=False)


def write_audit(
	*,
	source_work_order: str,
	target_work_order: str,
	reason: str,
	job_cards: list,
	stock_entries: list,
	operation_mapping: dict,
	counters_before: dict,
	counters_after: dict,
	preview_token: str,
	source_empty_after: int,
	result: str = "Success",
	error_message: str | None = None,
) -> str:
	doc = frappe.get_doc(
		{
			"doctype": "Job Card Work Order Reassignment Log",
			"source_work_order": source_work_order,
			"target_work_order": target_work_order,
			"reason": reason,
			"reassigned_by": frappe.session.user,
			"reassigned_on": now_datetime(),
			"result": result,
			"preview_token": preview_token,
			"job_cards_json": dumps(job_cards),
			"stock_entries_json": dumps(stock_entries),
			"operation_mapping_json": dumps(operation_mapping),
			"counters_before": dumps(counters_before),
			"counters_after": dumps(counters_after),
			"source_empty_after": source_empty_after,
			"error_message": error_message,
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


def add_comments(
	*,
	source_work_order: str,
	target_work_order: str,
	job_cards: list[str],
	stock_entries: list[str],
	reason: str,
	log_name: str,
) -> None:
	user = frappe.session.user
	summary = (
		f"Job Cards reassigned from {source_work_order} to {target_work_order} "
		f"by {user}. Reason: {reason}. Audit: {log_name}."
	)
	for doctype, name in (
		[("Work Order", source_work_order), ("Work Order", target_work_order)]
		+ [("Job Card", n) for n in job_cards]
	):
		frappe.get_doc(doctype, name).add_comment("Comment", summary)

	se_note = (
		f"Work Order reference reassigned {source_work_order} → {target_work_order} "
		f"(manufacturing metadata only; SLE/GL unchanged). Audit: {log_name}."
	)
	for name in stock_entries:
		frappe.get_doc("Stock Entry", name).add_comment("Comment", se_note)

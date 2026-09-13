# Copyright (c) 2026, ERPNext Extensions contributors
"""Unified Historical Stock Repair Log (resumable)."""

from __future__ import annotations

import json

import frappe
from frappe.utils import now_datetime


def start_run(topic: str, *, dry_run=True, user=None):
	if not frappe.db.exists("DocType", "Historical Stock Repair Log"):
		return None
	run_id = f"HSR-{now_datetime().strftime('%Y%m%d%H%M%S')}-{frappe.generate_hash(length=6)}"
	doc = frappe.new_doc("Historical Stock Repair Log")
	doc.repair_run_id = run_id
	doc.topic = topic
	doc.status = "Dry Run" if dry_run else "In Progress"
	doc.repaired_by = user or frappe.session.user
	doc.started_on = now_datetime()
	doc.resume_cursor = 0
	doc.insert(ignore_permissions=True)
	return doc


def append_entry(log, row: dict, *, written=False) -> None:
	if not log:
		return
	payload = {
		"topic": row.get("topic"),
		"patient_zero": (row.get("patient_zero") or {}).get("voucher_no")
		if isinstance(row.get("patient_zero"), dict)
		else row.get("patient_zero"),
		"voucher": row.get("voucher") or row.get("inbound_document"),
		"voucher_detail": row.get("voucher_detail"),
		"item": row.get("item") or row.get("item_code"),
		"warehouse": row.get("warehouse"),
		"batch_no": row.get("batch"),
		"current_value": str(row.get("current_rate") if row.get("current_rate") is not None else row.get("current_value") or ""),
		"proposed_value": str(row.get("proposed_rate") if row.get("proposed_rate") is not None else row.get("proposed_value") or ""),
		"source_of_truth": row.get("source_of_truth"),
		"confidence": row.get("confidence"),
		"status": row.get("status"),
		"payload": json.dumps(row, default=str)[: 60_000],
	}
	log.append("entries", payload)
	log.resume_cursor = (log.resume_cursor or 0) + 1


def finish_run(log, *, applied=0, blocked=0, error=None) -> None:
	if not log:
		return
	log.ended_on = now_datetime()
	log.status = "Failed" if error else ("Completed" if log.status != "Dry Run" else "Dry Run")
	log.summary = json.dumps({"applied": applied, "blocked": blocked, "error": error}, default=str)
	log.save(ignore_permissions=True)


def last_open_run(topic=None):
	filters = {"status": "In Progress"}
	if topic:
		filters["topic"] = topic
	name = frappe.db.get_value("Historical Stock Repair Log", filters, "name", order_by="creation desc")
	return frappe.get_doc("Historical Stock Repair Log", name) if name else None

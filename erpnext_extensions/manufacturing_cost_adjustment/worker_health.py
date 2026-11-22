# Copyright (c) 2026, ERPNext Extensions contributors
"""RQ health probe for the dedicated MCA queue."""

from __future__ import annotations

import frappe


def mca_queue_health() -> dict:
	"""Executed inside an MCA-capable worker — proves Iran bootstrap + queue wiring."""
	from erpnext_extensions.manufacturing_cost_adjustment import constants as C

	return queue_health_probe(queue_name=C.MCA_RQ_QUEUE)


def queue_health_probe(queue_name: str | None = None) -> dict:
	"""Generic RQ probe — works on short/default/long workers."""
	from erpnext_extensions.iran_accounting.integration.bootstrap import apply as apply_iran
	from erpnext_extensions.manufacturing_cost_adjustment import constants as C

	apply_iran()
	from erpnext.accounts import general_ledger as gl

	payload = {
		"ok": True,
		"queue": queue_name or C.MCA_RQ_QUEUE,
		"gl_patch_module": getattr(gl.process_debit_credit_difference, "__module__", ""),
		"gl_patch_qualname": getattr(gl.process_debit_credit_difference, "__qualname__", ""),
		"site": frappe.local.site,
	}
	frappe.cache().set_value(f"mca:queue_health:{payload['queue']}", payload, expires_in_sec=600)
	if payload["queue"] == C.MCA_RQ_QUEUE:
		frappe.cache().set_value("mca:queue_health:last", payload, expires_in_sec=600)
	frappe.logger("mca").info(f"queue health {payload}")
	return payload

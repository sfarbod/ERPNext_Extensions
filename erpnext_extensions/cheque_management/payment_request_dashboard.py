# Copyright (c) 2026, Farbod Siyahpoosh and contributors
# For license information, please see license.txt

"""Additive Payment Request dashboard: related Post Dated Cheques (traceability)."""

from __future__ import annotations

import frappe
from frappe import _


def get_data(data=None):
	"""Extend ERPNext Payment Request dashboard with Post Dated Cheque under Payment.

	Uses a custom open-count method so the badge and List filter share the same
	allocation/header relationship query (see ``pdc_payment_request_links``).
	"""
	data = frappe._dict(data or {})

	if not data.get("fieldname"):
		data.fieldname = "payment_request"

	# Count + List ``name in (...)`` from authoritative PDC↔PR link query.
	data.method = (
		"erpnext_extensions.cheque_management.pdc_payment_request_links.get_payment_request_open_count"
	)

	if not data.get("transactions"):
		data.transactions = []

	_append_payment_item(data, "Post Dated Cheque")
	return data


def _append_payment_item(data, item: str) -> None:
	payment_label = _("Payment")
	for group in data.transactions:
		label = group.get("label") or ""
		if label == payment_label or _(label) == payment_label or label == "Payment":
			items = group.setdefault("items", [])
			if item not in items:
				# After Payment Entry, before Payment Order when present.
				if "Payment Entry" in items:
					idx = items.index("Payment Entry") + 1
					items.insert(idx, item)
				else:
					items.append(item)
			return
	data.transactions.append({"label": payment_label, "items": [item]})

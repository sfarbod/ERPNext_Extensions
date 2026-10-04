# Copyright (c) 2026, ERPNext Extensions contributors
"""Immutable audit log for Job Card Stock Rebuild runs."""

from __future__ import annotations

import frappe
from frappe.model.document import Document


class JobCardStockRebuildLog(Document):
	def on_trash(self):
		frappe.throw(frappe._("Job Card Stock Rebuild Log entries cannot be deleted."))

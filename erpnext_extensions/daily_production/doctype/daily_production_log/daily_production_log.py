# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Daily Production Log — one line per stage per day.

The document only carries the operator's input and the audit trail; everything
that touches manufacturing happens in ``erpnext_extensions.daily_production.runner``.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, get_datetime

LINK_FIELDS = ("job_card", "transfer_stock_entry", "manufacture_stock_entry")


class DailyProductionLog(Document):
	def validate(self):
		self._block_edit_after_done()
		self._validate_inputs()
		self._set_operation_row_id()
		self._default_dimensions()

	def on_trash(self):
		linked = [self.get(f) for f in LINK_FIELDS if self.get(f)]
		if linked or self.get("created_documents"):
			frappe.throw(
				_("This log already created documents ({0}) and cannot be deleted.").format(
					", ".join(linked) or _("see Created Documents")
				)
			)

	# ------------------------------------------------------------------ rules
	def _block_edit_after_done(self):
		"""A Done log is a record of what was posted; it is never edited by hand.
		The runner updates status/links with ``flags.from_runner``."""
		if self.is_new() or self.flags.from_runner:
			return
		before = self.get_doc_before_save()
		if before and before.status == "Done":
			frappe.throw(_("Log {0} is Done and cannot be edited.").format(self.name))

	def _validate_inputs(self):
		if flt(self.qty) <= 0:
			frappe.throw(_("Qty must be greater than zero."))
		if self.from_time and self.to_time and get_datetime(self.to_time) <= get_datetime(self.from_time):
			frappe.throw(_("To time must be after From time."))
		if not self.status:
			self.status = "Draft"

	def _set_operation_row_id(self):
		"""Bind the log to the Work Order's operation row (idx), not just the operation name —
		that is what Track Semi Finished Goods uses to attach materials to Job Cards."""
		if not (self.work_order and self.operation):
			return
		rows = frappe.get_all(
			"Work Order Operation",
			filters={"parent": self.work_order, "parenttype": "Work Order", "operation": self.operation},
			fields=["idx", "name"],
			order_by="idx asc",
		)
		if not rows:
			frappe.throw(
				_("Operation {0} is not part of Work Order {1}.").format(self.operation, self.work_order)
			)
		if len(rows) > 1 and self.operation_row_id not in [r.idx for r in rows]:
			frappe.throw(
				_("Operation {0} appears more than once in {1}; set Operation Row ID explicitly.").format(
					self.operation, self.work_order
				)
			)
		if self.operation_row_id not in [r.idx for r in rows]:
			self.operation_row_id = rows[0].idx

	def _default_dimensions(self):
		"""Department is mandatory on every Stock Entry row on this site; fall back to the
		operator's department / payroll cost center when the user left them empty."""
		if self.employee and not (self.department and self.cost_center):
			dept, cc = frappe.db.get_value("Employee", self.employee, ["department", "payroll_cost_center"])
			self.department = self.department or dept
			self.cost_center = self.cost_center or cc

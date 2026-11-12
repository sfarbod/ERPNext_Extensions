# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt


class MFGCostBatchExpenseAllocation(Document):
	def validate(self):
		self._recalc_allocated_amount()

	def before_save(self):
		self._recalc_allocated_amount()

	def on_update(self):
		"""Route Applied Batch Share changes through the share engine.

		Root cause of the defect: editing Applied Batch Share on the form and Saving
		updated the share field without recalculating Allocated Amount / parent totals
		via set_applied_batch_share.
		"""
		if frappe.flags.get("costing_in_share_engine"):
			return
		prev = self.get_doc_before_save()
		if not prev:
			return
		if flt(prev.applied_batch_share) == flt(self.applied_batch_share):
			# Still ensure formula holds for this row
			expected = flt(flt(self.pool_monthly_net_expense) * flt(self.applied_batch_share), self.precision("allocated_amount") or 2)
			if abs(flt(self.allocated_amount) - expected) > 0.0001:
				frappe.db.set_value(
					self.doctype,
					self.name,
					"allocated_amount",
					expected,
					update_modified=False,
				)
			return

		from erpnext_extensions.iran_accounting.fg_cost_allocation.engine import (
			set_applied_batch_share,
		)

		set_applied_batch_share(
			batch_expense_allocation=self.name,
			applied_share=flt(self.applied_batch_share),
			reason=getattr(self, "_share_override_reason", None) or "Form save Applied Batch Share change",
			commit=0,
		)

	def _recalc_allocated_amount(self):
		"""Allocated Amount = Pool Monthly Net Expense × Applied Batch Share."""
		pool_net = flt(self.pool_monthly_net_expense)
		share = flt(self.applied_batch_share)
		if share < 0:
			frappe.throw(_("Applied Batch Share cannot be negative."))
		precision = self.precision("allocated_amount") or 2
		self.allocated_amount = flt(pool_net * share, precision)


@frappe.whitelist()
def update_applied_batch_share(name: str, applied_share: float, reason: str | None = None):
	"""Desk-friendly wrapper for server-side Applied Batch Share edits."""
	from erpnext_extensions.iran_accounting.fg_cost_allocation.engine import set_applied_batch_share

	return set_applied_batch_share(
		batch_expense_allocation=name,
		applied_share=applied_share,
		reason=reason,
		commit=1,
	)

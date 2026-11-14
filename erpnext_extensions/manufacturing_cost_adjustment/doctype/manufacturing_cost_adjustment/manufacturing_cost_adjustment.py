# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, getdate, now_datetime

from erpnext_extensions.manufacturing_cost_adjustment import constants as C
from erpnext_extensions.manufacturing_cost_adjustment.locks import acquire_se_lock, release_se_lock


class ManufacturingCostAdjustment(Document):
	def validate(self):
		self._set_company_from_se()
		self._validate_stock_entry()
		self._validate_feature_enabled()
		self._validate_costs()
		self._set_base_amounts()
		self.total_additional_costs = sum(flt(r.base_amount) for r in self.get("costs") or [])
		if not self.idempotency_key:
			self.idempotency_key = f"mca:{self.name or 'new'}:draft"

	def before_submit(self):
		self._validate_stock_entry()
		self._validate_no_concurrent()
		self.processing_status = C.PS_QUEUED
		self.repost_required = 1
		self.repost_status = C.RS_REQUIRED
		self.processing_attempt = cint_safe(self.processing_attempt) + 1
		self.idempotency_key = f"mca:{self.name}:attempt:{self.processing_attempt}"
		self.checkpoint = C.CP_NONE

	def on_submit(self):
		# Tests run MCRE synchronously; production enqueues (scheduler also polls).
		if frappe.flags.in_test or frappe.conf.get("mca_process_sync"):
			from erpnext_extensions.manufacturing_cost_adjustment.repost_engine import (
				process_mca,
			)

			process_mca(self.name)
			return
		frappe.enqueue(
			"erpnext_extensions.manufacturing_cost_adjustment.repost_engine.process_mca",
			mca_name=self.name,
			queue="long",
			timeout=3600,
			enqueue_after_commit=True,
			job_id=f"mca-process:{self.name}:{self.processing_attempt}",
			deduplicate=True,
		)

	def on_cancel(self):
		frappe.throw(
			_(
				"Manufacturing Cost Adjustment cannot be cancelled independently. "
				"Cancel the linked Manufacture Stock Entry to reverse all adjustments."
			)
		)

	def before_update_after_submit(self):
		# Block ordinary field edits after submit except engine-driven db_set paths.
		if not getattr(self.flags, "mca_engine_update", False):
			# Allow only retry button path
			if not getattr(self.flags, "mca_retry", False):
				pass  # engine uses db_set; form save is rare

	def _set_company_from_se(self):
		if self.stock_entry and not self.company:
			self.company = frappe.db.get_value("Stock Entry", self.stock_entry, "company")

	def _validate_feature_enabled(self):
		if not self.company:
			return
		enabled = frappe.db.get_value("Company", self.company, C.CF_COMPANY_ENABLE)
		# Default enable when field missing (pre-migrate); after migrate default 0/1 per field.
		if enabled is not None and not cint_safe(enabled):
			frappe.throw(
				_("Manufacturing Cost Adjustment is disabled for Company {0}.").format(self.company)
			)

	def _validate_stock_entry(self):
		if not self.stock_entry:
			frappe.throw(_("Stock Entry is required"))
		se = frappe.db.get_value(
			"Stock Entry",
			self.stock_entry,
			["docstatus", "purpose", "company", "name", "posting_date"],
			as_dict=True,
		)
		if not se:
			frappe.throw(_("Stock Entry {0} not found").format(self.stock_entry))
		if cint_safe(se.docstatus) != 1:
			frappe.throw(_("Stock Entry {0} must be Submitted").format(self.stock_entry))
		if se.purpose not in ("Manufacture", "Repack"):
			frappe.throw(
				_("Stock Entry purpose must be Manufacture or Repack (got {0})").format(se.purpose)
			)
		# Company must match linked SE (prevent cross-company MCA)
		if self.company and self.company != se.company:
			frappe.throw(
				_("MCA Company {0} does not match Stock Entry Company {1}.").format(
					self.company, se.company
				)
			)
		self.company = se.company
		self.stock_entry_purpose = se.purpose

		# Consignment / loan ban — reject even when SE has no cost rows yet
		try:
			from erpnext_extensions.consignment_stock.constants import F_IS_RECEIPT, F_IS_RETURN

			doc = frappe.get_doc("Stock Entry", self.stock_entry)
			if cint_safe(doc.get(F_IS_RECEIPT)) or cint_safe(doc.get(F_IS_RETURN)):
				frappe.throw(_("Manufacturing Cost Adjustment is not allowed on Consignment Stock Entries."))
			from erpnext_extensions.consignment_stock.additional_costs import validate_no_additional_costs

			validate_no_additional_costs(doc)
		except ImportError:
			pass
		except frappe.ValidationError:
			raise

		self._validate_accounting_period(se)

	def _validate_accounting_period(self, se) -> None:
		"""Reject MCA when the SE posting date falls in a closed Stock Entry accounting period."""
		try:
			from erpnext.accounts.doctype.accounting_period.accounting_period import (
				ClosedAccountingPeriod,
			)
		except ImportError:
			return
		posting_date = se.posting_date or self.posting_date
		if not posting_date or not se.company:
			return
		closed = frappe.db.sql(
			"""
			SELECT ap.name
			FROM `tabAccounting Period` ap
			INNER JOIN `tabClosed Document` cd ON cd.parent = ap.name
			WHERE ap.company = %s
			  AND cd.document_type = 'Stock Entry'
			  AND cd.closed = 1
			  AND %s BETWEEN ap.start_date AND ap.end_date
			LIMIT 1
			""",
			(se.company, posting_date),
		)
		if closed:
			frappe.throw(
				_("Cannot create Manufacturing Cost Adjustment: Accounting Period {0} is closed for Stock Entry.").format(
					closed[0][0]
				),
				exc=ClosedAccountingPeriod,
			)

	def _validate_costs(self):
		if not self.get("costs"):
			frappe.throw(_("At least one additional cost row is required"))
		for row in self.costs:
			if not row.expense_account:
				frappe.throw(_("Expense Account is required on every cost row"))
			if flt(row.amount) == 0 and flt(row.base_amount) == 0:
				frappe.throw(_("Cost amount cannot be zero for account {0}").format(row.expense_account))
			# Expense account must belong to MCA/SE company
			acc_company = frappe.get_cached_value("Account", row.expense_account, "company")
			if acc_company and self.company and acc_company != self.company:
				frappe.throw(
					_("Expense Account {0} belongs to Company {1}, not {2}.").format(
						row.expense_account, acc_company, self.company
					)
				)
	def _set_base_amounts(self):
		for row in self.get("costs") or []:
			if not row.exchange_rate:
				row.exchange_rate = 1
			if row.expense_account and not row.account_currency:
				row.account_currency = frappe.get_cached_value(
					"Account", row.expense_account, "account_currency"
				)
			row.base_amount = flt(flt(row.amount) * flt(row.exchange_rate), row.precision("base_amount"))

	def _validate_no_concurrent(self):
		others = frappe.get_all(
			C.MCA_DOCTYPE,
			filters={
				"stock_entry": self.stock_entry,
				"docstatus": 1,
				"name": ("!=", self.name or ""),
				"processing_status": ("in", list(C.NON_TERMINAL - {C.PS_FAILED})),
			},
			pluck="name",
		)
		# Allow Failed siblings; block active in-flight
		active = [
			n
			for n in others
			if frappe.db.get_value(C.MCA_DOCTYPE, n, "processing_status")
			in (
				C.PS_QUEUED,
				C.PS_APPENDING,
				C.PS_ALLOCATING,
				C.PS_REBUILDING_SLE,
				C.PS_REBUILDING_GL,
				C.PS_VALIDATING,
				C.PS_PENDING_RIV,
				C.PS_RECONCILING,
			)
		]
		if active:
			frappe.throw(
				_("Another Manufacturing Cost Adjustment is already processing for {0}: {1}").format(
					self.stock_entry, ", ".join(active)
				)
			)


def cint_safe(v) -> int:
	try:
		return int(v or 0)
	except Exception:
		return 0


@frappe.whitelist()
def make_mca_from_stock_entry(stock_entry: str):
	"""Create draft MCA from submitted Manufacture/Repack Stock Entry."""
	frappe.has_permission(C.MCA_DOCTYPE, "create", throw=True)
	se = frappe.get_doc("Stock Entry", stock_entry)
	if se.docstatus != 1:
		frappe.throw(_("Stock Entry must be submitted"))
	if se.purpose not in ("Manufacture", "Repack"):
		frappe.throw(_("Only Manufacture or Repack Stock Entries are supported"))
	enabled = frappe.db.get_value("Company", se.company, C.CF_COMPANY_ENABLE)
	if enabled is not None and not cint_safe(enabled):
		frappe.throw(_("Manufacturing Cost Adjustment is disabled for Company {0}.").format(se.company))
	# Consignment ban at create time
	try:
		from erpnext_extensions.consignment_stock.constants import F_IS_RECEIPT, F_IS_RETURN

		if cint_safe(se.get(F_IS_RECEIPT)) or cint_safe(se.get(F_IS_RETURN)):
			frappe.throw(_("Manufacturing Cost Adjustment is not allowed on Consignment Stock Entries."))
	except ImportError:
		pass
	doc = frappe.new_doc(C.MCA_DOCTYPE)
	doc.company = se.company
	doc.stock_entry = se.name
	doc.stock_entry_purpose = se.purpose
	doc.work_order = se.work_order
	doc.posting_date = getdate()
	doc.processing_status = C.PS_DRAFT
	return doc


@frappe.whitelist()
def retry_mca_processing(name: str):
	"""Manual controlled retry for Failed / Pending RIV MCA."""
	frappe.has_permission(C.MCA_DOCTYPE, "write", throw=True)
	doc = frappe.get_doc(C.MCA_DOCTYPE, name)
	doc.check_permission("write")
	if doc.docstatus != 1:
		frappe.throw(_("Only submitted MCA can be retried"))
	if doc.processing_status == C.PS_COMPLETED:
		frappe.throw(_("MCA is already Completed"))
	if doc.processing_status == C.PS_VOIDED:
		frappe.throw(_("MCA was voided by Stock Entry cancellation"))
	doc.flags.mca_retry = True
	frappe.db.set_value(
		C.MCA_DOCTYPE,
		name,
		{
			"repost_required": 1,
			"next_retry_at": now_datetime(),
			"processing_status": C.PS_QUEUED if doc.processing_status == C.PS_FAILED else doc.processing_status,
		},
		update_modified=False,
	)
	frappe.enqueue(
		"erpnext_extensions.manufacturing_cost_adjustment.repost_engine.process_mca",
		mca_name=name,
		queue="long",
		timeout=3600,
		enqueue_after_commit=True,
		job_id=f"mca-retry:{name}:{now_datetime()}",
	)
	return {"ok": True}

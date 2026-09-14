from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt, getdate, today

from erpnext_extensions.petty_management.services.accounting_party import (
	journal_entry_party_for_petty_cash_credit,
	journal_entry_party_for_supplier_line,
	resolve_clearance_employee,
	validate_petty_cash_credit_party,
)
from erpnext_extensions.petty_management.services.clearance_service import (
	clearance_is_approved,
	validate_clearance,
)
from erpnext_extensions.petty_management.services.constants import SETTLEMENT_PI, SETTLEMENT_SA
from erpnext_extensions.petty_management.services.holder_service import clearance_petty_cash_account
from erpnext_extensions.petty_management.utils import get_pm_settings


def build_clearance_je_accounts(doc: Document) -> list[dict]:
	lines: list[dict] = []
	total_petty_credit = 0.0

	for row in doc.details:
		settlement_type = (getattr(row, "settlement_type", None) or SETTLEMENT_PI).strip()
		alloc = flt(row.allocated_amount)
		cost_center = row.cost_center or None
		project = row.project or doc.project or None

		if settlement_type == SETTLEMENT_SA:
			line = build_supplier_advance_debit_line(row, alloc)
		else:
			line = build_purchase_invoice_debit_line(row, alloc)

		if cost_center:
			line["cost_center"] = cost_center
		if project:
			line["project"] = project
		lines.append(line)
		total_petty_credit += alloc

	lines.append(build_petty_cash_credit_line(doc, total_petty_credit))
	return lines


def build_supplier_advance_debit_line(row: Document, amount: float) -> dict:
	if not row.supplier_advance_account:
		frappe.throw(_("Row {0}: Supplier Advance Account is required for Supplier Advance.").format(row.idx))
	line = {
		"account": row.supplier_advance_account,
		"party_type": "Supplier",
		"party": row.supplier,
		"reference_type": "Purchase Order",
		"reference_name": row.purchase_order,
		"debit_in_account_currency": amount,
		"credit_in_account_currency": 0,
	}
	line.update(journal_entry_party_for_supplier_line(row.supplier_advance_account, row.supplier))
	return line


def build_purchase_invoice_debit_line(row: Document, amount: float) -> dict:
	from erpnext_extensions.petty_management.services.purchase_invoice_readiness import (
		assert_purchase_invoice_submitted_for_je,
	)

	assert_purchase_invoice_submitted_for_je(row.purchase_invoice)
	pi = frappe.get_doc("Purchase Invoice", row.purchase_invoice)
	line = {
		"account": pi.credit_to,
		"party_type": "Supplier",
		"party": pi.supplier,
		"reference_type": "Purchase Invoice",
		"reference_name": pi.name,
		"debit_in_account_currency": amount,
		"credit_in_account_currency": 0,
	}
	line.update(journal_entry_party_for_supplier_line(pi.credit_to, pi.supplier))
	return line


def build_petty_cash_credit_line(doc: Document, amount: float) -> dict:
	petty = clearance_petty_cash_account(doc)
	if not petty:
		frappe.throw(_("Petty Cash Account is missing on this clearance."))
	line = {
		"account": petty,
		"debit_in_account_currency": 0,
		"credit_in_account_currency": amount,
	}
	line.update(
		journal_entry_party_for_petty_cash_credit(
			petty,
			company=doc.company,
			employee=resolve_clearance_employee(doc),
			holder=(getattr(doc, "holder", None) or "").strip() or None,
		)
	)
	return line


def create_clearance_journal_entry(doc: Document) -> Document:
	from erpnext_extensions.petty_management.services.purchase_invoice_readiness import (
		validate_purchase_invoices_for_settlement,
	)

	validate_purchase_invoices_for_settlement(doc)
	settings = get_pm_settings()
	je = frappe.new_doc("Journal Entry")
	je.company = doc.company
	je.voucher_type = "Journal Entry"
	je.posting_date = getdate(doc.je_clearance_date or doc.transaction_date or today())
	from erpnext_extensions.petty_management.services.narration_service import (
		apply_settlement_journal_entry_remarks,
	)

	meta = frappe.get_meta("Journal Entry")
	if meta.has_field("custom_pm_clearance"):
		je.custom_pm_clearance = doc.name
	if meta.has_field("custom_pm_holder") and doc.holder:
		je.custom_pm_holder = doc.holder

	for line in build_clearance_je_accounts(doc):
		if flt(line.get("credit_in_account_currency")) > 0:
			validate_petty_cash_credit_party(doc, line)
		je.append("accounts", line)

	apply_settlement_journal_entry_remarks(je, doc)

	je.insert(ignore_permissions=True)
	apply_settlement_journal_entry_remarks(je, doc)
	je.db_set("user_remark", je.user_remark, update_modified=False)
	if meta.has_field("remark") and je.remark:
		je.db_set("remark", je.remark, update_modified=False)

	if settings and settings.auto_submit_journal_entry:
		apply_settlement_journal_entry_remarks(je, doc)
		je.db_set("user_remark", je.user_remark, update_modified=False)
		je.submit()
	return je


def settle_petty_cash(pm_clearance: str) -> dict[str, str]:
	doc = frappe.get_doc("PM Clearance", pm_clearance, for_update=True)
	if not frappe.has_permission("PM Clearance", "read", doc=doc):
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	doc.check_permission("write")
	if doc.docstatus != 1:
		frappe.throw(_("Please submit PM Clearance before settling."), title=_("Submit required"))
	if (getattr(doc, "status", None) or "").strip() in ("Rejected", "Cancelled"):
		frappe.throw(_("A rejected or cancelled clearance cannot be settled."), title=_("Not allowed"))

	existing_je = frappe.db.get_value("PM Clearance", pm_clearance, "journal_entry")
	if existing_je:
		je_ds = None
		if frappe.db.exists("Journal Entry", existing_je):
			je_ds = cint(frappe.db.get_value("Journal Entry", existing_je, "docstatus"))
		# Active (draft or submitted) JE: idempotent return. Cancelled/missing: clear stale link.
		if je_ds in (0, 1):
			st = frappe.db.get_value("PM Clearance", pm_clearance, "status") or ""
			return {"journal_entry": existing_je, "status": st}
		frappe.db.set_value(
			"PM Clearance",
			pm_clearance,
			{"journal_entry": None},
			update_modified=False,
		)
		doc.journal_entry = None

	if not clearance_is_approved(doc):
		frappe.throw(_("Settle is only allowed when PM Clearance is Approved."), title=_("Approval required"))
	from erpnext_extensions.petty_management.services.purchase_invoice_readiness import (
		validate_purchase_invoices_for_settlement,
	)

	validate_purchase_invoices_for_settlement(doc)

	doc.reload()
	if not clearance_is_approved(doc):
		frappe.throw(_("Settle is only allowed when PM Clearance is Approved."), title=_("Approval required"))
	validate_clearance(doc)

	try:
		je = create_clearance_journal_entry(doc)
		doc.db_set("journal_entry", je.name, update_modified=False)
		je.reload()
		from erpnext_extensions.petty_management.services.clearance_action_policy import (
			sync_clearance_lifecycle,
		)

		doc.reload()
		next_status = sync_clearance_lifecycle(doc, persist=True)
		for row in doc.details:
			frappe.db.set_value(
				row.doctype,
				row.name,
				{"generated_doctype": "Journal Entry", "generated_document": je.name},
				update_modified=False,
			)
		try:
			from erpnext_extensions.petty_management import petty_audit

			petty_audit.log_event(
				"pm_clearance_settled",
				pm_clearance=doc.name,
				journal_entry=je.name,
				holder=doc.holder,
				employee=doc.employee,
				amount=sum(float(getattr(r, "allocated_amount", 0) or 0) for r in (doc.details or [])),
				company=doc.company,
				je_docstatus=je.docstatus,
			)
		except Exception:
			pass
	except Exception as e:
		frappe.db.rollback()
		frappe.throw(_("Could not create settlement Journal Entry: {0}").format(str(e)))

	return {"journal_entry": je.name, "status": next_status}

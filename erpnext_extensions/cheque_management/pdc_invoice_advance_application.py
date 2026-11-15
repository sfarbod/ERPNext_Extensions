from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt, getdate

from erpnext_extensions.cheque_management import pdc_advance_application_service as adv_svc


def _advance_application_remarks(dt: str, invoice_name: str) -> str:
	"""Human-readable remark (keep short to avoid title truncation)."""
	return _("Apply Advance Post Dated Cheque to {0} {1}").format(dt, invoice_name)


def _advance_application_idempotency_key(dt: str, invoice_name: str) -> str:
	"""Deterministic idempotency key stored on Journal Entry (custom field)."""
	return f"apply_pdc_advance|{dt}|{invoice_name}"


def _advance_reversal_remarks(dt: str, invoice_name: str) -> str:
	"""Human-readable reversal remark (keep short to avoid title truncation)."""
	return _("Reverse Advance Post Dated Cheque application on {0} {1}").format(dt, invoice_name)


def _advance_reversal_idempotency_key(dt: str, invoice_name: str) -> str:
	"""Deterministic idempotency key stored on Journal Entry for reversals."""
	return f"reverse_pdc_advance|{dt}|{invoice_name}"


def _find_existing_submitted_reversal_je(dt: str, invoice_name: str) -> str | None:
	"""Return existing submitted JE name for this invoice advance-reversal action, if any."""
	key = _advance_reversal_idempotency_key(dt, invoice_name)
	rows = frappe.db.sql(
		"""
		SELECT name
		FROM `tabJournal Entry`
		WHERE docstatus = 1
		  AND COALESCE(pdc_advance_idempotency_key, '') = %s
		ORDER BY modified DESC
		LIMIT 2
		""",
		(key,),
		as_dict=True,
	)
	if not rows:
		return None
	if len(rows) > 1:
		frappe.throw(
			_(
				"Multiple submitted Journal Entries exist for the same PDC advance reversal on {0} {1}."
			).format(dt, invoice_name),
			title=_("PDC Advance"),
		)
	return (rows[0] or {}).get("name")


def _find_existing_submitted_application_je(dt: str, invoice_name: str) -> str | None:
	"""Return existing submitted JE name for this invoice advance-apply action, if any."""
	key = _advance_application_idempotency_key(dt, invoice_name)
	rows = frappe.db.sql(
		"""
		SELECT name
		FROM `tabJournal Entry`
		WHERE docstatus = 1
		  AND COALESCE(pdc_advance_idempotency_key, '') = %s
		ORDER BY modified DESC
		LIMIT 2
		""",
		(key,),
		as_dict=True,
	)
	if not rows:
		return None
	if len(rows) > 1:
		frappe.throw(
			_(
				"Multiple submitted Journal Entries exist for the same PDC advance application on {0} {1}."
			).format(dt, invoice_name),
			title=_("PDC Advance"),
		)
	return (rows[0] or {}).get("name")


def _validate_existing_je_matches_expected_total(
	dt: str, invoice_name: str, je_name: str, expected_total: float
) -> None:
	"""Safety check: ensure existing JE matches the expected invoice reference amount."""
	if not je_name:
		return
	row = frappe.db.sql(
		"""
		SELECT
		  SUM(COALESCE(debit_in_account_currency, 0) + COALESCE(credit_in_account_currency, 0)) AS ref_amt
		FROM `tabJournal Entry Account`
		WHERE parent = %s
		  AND reference_type = %s
		  AND reference_name = %s
		""",
		(je_name, dt, invoice_name),
		as_dict=True,
	)
	ref_amt = flt(((row[0] or {}) if row else {}).get("ref_amt"))
	if ref_amt <= 1e-9:
		# Reversal JE may intentionally omit invoice reference fields (cancelled-doc restriction).
		# Fallback: validate by net movement on eligible advance accounts (v5.7.42 multi-account).
		company = (frappe.db.get_value(dt, invoice_name, "company") or "").strip()
		adv_accounts: list[str] = []
		try:
			from erpnext_extensions.advance_accounts.eligibility import (
				DIRECTION_PAID,
				DIRECTION_RECEIVED,
				get_eligible_advance_accounts,
			)

			direction = DIRECTION_PAID if dt == "Purchase Invoice" else DIRECTION_RECEIVED
			adv_accounts = get_eligible_advance_accounts(company, direction) if company else []
		except Exception:
			adv_accounts = []
		if not adv_accounts:
			if dt == "Purchase Invoice":
				adv = _company_default_advance_paid_account(company) or ""
			else:
				adv = _company_default_advance_received_account(company) or ""
			if adv:
				adv_accounts = [adv]
		if not adv_accounts:
			# If accounts are missing we cannot validate; fail safe.
			frappe.throw(
				_(
					"Could not validate existing Journal Entry {0} against advance account (missing company defaults)."
				).format(je_name),
				title=_("PDC Advance"),
			)
		acc = frappe.db.sql(
			"""
			SELECT
			  SUM(COALESCE(debit_in_account_currency, 0)) AS dr,
			  SUM(COALESCE(credit_in_account_currency, 0)) AS cr
			FROM `tabJournal Entry Account`
			WHERE parent = %s
			  AND account IN %s
			""",
			(je_name, tuple(adv_accounts)),
			as_dict=True,
		)
		r = (acc[0] if acc else {}) or {}
		net = abs(flt(r.get("dr")) - flt(r.get("cr")))
		ref_amt = net

	if abs(ref_amt - flt(expected_total)) > 1e-6:
		frappe.throw(
			_(
				"Existing PDC advance application Journal Entry {0} does not match the current draft application total. "
				"Expected {1}, found {2}."
			).format(je_name, expected_total, ref_amt),
			title=_("PDC Advance"),
		)


def _company_default_advance_paid_account(company: str) -> str | None:
	return (frappe.db.get_value("Company", company, "default_advance_paid_account") or "").strip() or None


def _company_default_advance_received_account(company: str) -> str | None:
	return (frappe.db.get_value("Company", company, "default_advance_received_account") or "").strip() or None


def _invoice_order_link(dt: str, doc) -> tuple[str | None, str | None]:
	if dt == "Purchase Invoice":
		for it in getattr(doc, "items", None) or []:
			po = (getattr(it, "purchase_order", None) or "").strip()
			if po:
				return "Purchase Order", po
		return None, None
	if dt == "Sales Invoice":
		for it in getattr(doc, "items", None) or []:
			so = (getattr(it, "sales_order", None) or "").strip()
			if so:
				return "Sales Order", so
		return None, None
	return None, None


def _row_advance_scope(row) -> str:
	"""Infer scope for legacy rows.

	New rows are expected to be explicit, but old rows may have empty `advance_scope`.
	For safety:
	- if order fields exist => order_based
	- else => general
	"""
	raw = (getattr(row, "advance_scope", None) or "").strip()
	if raw in ("order_based", "general"):
		return raw
	odt = (getattr(row, "order_doctype", None) or "").strip()
	onm = (getattr(row, "order_name", None) or "").strip()
	return "order_based" if (odt or onm) else "general"


def _validate_order_based_rows_match_invoice(doc, dt: str) -> tuple[str, str]:
	order_dt, order_nm = _invoice_order_link(dt, doc)
	if not (order_dt and order_nm):
		frappe.throw(
			_("This invoice is not linked to a {0}; order-based Advance PDC cannot be applied.").format(
				"Purchase Order" if dt == "Purchase Invoice" else "Sales Order"
			),
			title=_("PDC Advance"),
		)

	for row in getattr(doc, "pdc_invoice_applications", None) or []:
		if (getattr(row, "application_status", None) or "draft").strip() != "draft":
			continue
		if _row_advance_scope(row) != "order_based":
			continue
		if (getattr(row, "order_doctype", None) or "").strip() != order_dt or (
			getattr(row, "order_name", None) or ""
		).strip() != order_nm:
			frappe.throw(
				_("PDC Invoice Application rows must match the invoice order link."), title=_("PDC Advance")
			)
	return order_dt, order_nm


def _build_application_je_payload(doc, dt: str, total_pdc_amt: float) -> dict:
	company = (getattr(doc, "company", None) or "").strip()
	posting_date = getdate(getattr(doc, "posting_date", None) or frappe.utils.today())
	remarks = _advance_application_remarks(dt, doc.name)
	idem_key = _advance_application_idempotency_key(dt, doc.name)

	if dt == "Purchase Invoice":
		ap = (getattr(doc, "credit_to", None) or "").strip()
		adv = _company_default_advance_paid_account(company)
		if not ap or not adv:
			return {}
		return {
			"voucher_type": "Journal Entry",
			"posting_date": posting_date,
			"remarks": remarks,
			"pdc_advance_idempotency_key": idem_key,
			"accounts": [
				{
					"account": ap,
					"debit_in_account_currency": flt(total_pdc_amt),
					"party_type": "Supplier",
					"party": (getattr(doc, "supplier", None) or "").strip(),
					"reference_type": "Purchase Invoice",
					"reference_name": doc.name,
				},
				{
					"account": adv,
					"credit_in_account_currency": flt(total_pdc_amt),
					"party_type": "Supplier",
					"party": (getattr(doc, "supplier", None) or "").strip(),
				},
			],
		}

	# Sales Invoice
	ar = (getattr(doc, "debit_to", None) or "").strip()
	adv = _company_default_advance_received_account(company)
	if not ar or not adv:
		return {}
	return {
		"voucher_type": "Journal Entry",
		"posting_date": posting_date,
		"remarks": remarks,
		"pdc_advance_idempotency_key": idem_key,
		"accounts": [
			{
				"account": adv,
				"debit_in_account_currency": flt(total_pdc_amt),
				"party_type": "Customer",
				"party": (getattr(doc, "customer", None) or "").strip(),
			},
			{
				"account": ar,
				"credit_in_account_currency": flt(total_pdc_amt),
				"party_type": "Customer",
				"party": (getattr(doc, "customer", None) or "").strip(),
				"reference_type": "Sales Invoice",
				"reference_name": doc.name,
			},
		],
	}


def _post_je(payload: dict) -> str:
	je = frappe.new_doc("Journal Entry")
	je.flags.ignore_permissions = True
	je.voucher_type = payload.get("voucher_type") or "Journal Entry"
	je.posting_date = payload.get("posting_date")
	je.company = payload.get("company") or None
	je.user_remark = payload.get("remarks") or ""
	je.pdc_advance_idempotency_key = payload.get("pdc_advance_idempotency_key") or ""
	for row in payload.get("accounts") or []:
		je.append("accounts", dict(row))
	je.submit()
	return je.name


def on_invoice_submit(doc, method=None) -> None:
	dt = (getattr(doc, "doctype", None) or "").strip()
	if dt not in ("Purchase Invoice", "Sales Invoice"):
		return
	if not (getattr(doc, "pdc_invoice_applications", None) or []):
		return
	has_order_based_draft = any(
		(
			(getattr(r, "application_status", None) or "draft").strip() == "draft"
			and _row_advance_scope(r) == "order_based"
		)
		for r in (doc.pdc_invoice_applications or [])
	)
	order_dt, order_nm = (None, None)
	if has_order_based_draft:
		order_dt, order_nm = _validate_order_based_rows_match_invoice(doc, dt)

	# v1: require invoice currency == PDC currency; application row stores amount_in_pdc_currency as canonical.
	any_posted = any(
		(r.application_status or "").strip() == "posted" for r in (doc.pdc_invoice_applications or [])
	)
	total = 0.0
	for row in doc.pdc_invoice_applications or []:
		if (row.application_status or "draft").strip() != "draft":
			continue
		amt = flt(getattr(row, "amount_in_pdc_currency", None) or 0)
		if amt <= 0:
			frappe.throw(_("Advance PDC application amount must be > 0."), title=_("PDC Advance"))
		total += amt

	if total <= 1e-9:
		return
	if any_posted:
		frappe.throw(
			_(
				"This invoice already has posted PDC advance application rows. "
				"Posting additional advance application on the same submit is not supported."
			),
			title=_("PDC Advance"),
		)

	# Idempotency: reuse existing submitted JE for this invoice+action.
	existing = _find_existing_submitted_application_je(dt, doc.name)
	if existing:
		_validate_existing_je_matches_expected_total(dt, doc.name, existing, total)
		je_name = existing
	else:
		payload = _build_application_je_payload(doc, dt, total)
		if not payload:
			frappe.throw(
				_("Could not build Journal Entry for PDC advance application (missing accounts)."),
				title=_("PDC Advance"),
			)
		je_name = _post_je(payload)

	for row in doc.pdc_invoice_applications or []:
		if (row.application_status or "draft").strip() != "draft":
			continue
		scope = _row_advance_scope(row)
		# Persist after-submit changes explicitly: modifying child rows in-memory during on_submit
		# does not reliably write back to DB.
		values = {
			"application_status": "posted",
			"posted_je": je_name,
		}
		if scope == "order_based":
			values["order_doctype"] = order_dt
			values["order_name"] = order_nm
		else:
			# Keep general rows order-less.
			values["order_doctype"] = ""
			values["order_name"] = ""
			if not (getattr(row, "source_bucket_label", None) or "").strip():
				values["source_bucket_label"] = "General Pool"
		frappe.db.set_value(
			"PDC Invoice Application",
			row.name,
			values,
			update_modified=False,
		)
		row.application_status = "posted"
		row.posted_je = je_name
		if scope == "order_based":
			row.order_doctype = order_dt
			row.order_name = order_nm
		else:
			row.order_doctype = ""
			row.order_name = ""
			if values.get("source_bucket_label"):
				row.source_bucket_label = values["source_bucket_label"]


def on_invoice_validate(doc, method=None) -> None:
	"""Draft-save structural validation hook for PI/SI.

	Implementation will be moved to the service layer (`normalize_invoice_pdc_application_rows`).
	This hook runs only lightweight structural checks; no open-balance math, no JE posting,
	no DB mutations.
	"""
	adv_svc.validate_pdc_invoice_application_rows_structural(doc)
	return None


def before_invoice_submit(doc, method=None) -> None:
	"""Submit-time validation hook for PI/SI (no writes).

	Will eventually:
	- normalize rows
	- build consumption plan
	- lock resources
	- recompute opens
	- validate invariants & conflicts

	Validation-only in this step:
	- structural + eligibility checks
	- financial checks against current open advance helpers

	No JE posting. No DB mutation. No locking/concurrency yet.
	"""
	adv_svc.validate_before_invoice_submit(doc)
	return None


def on_invoice_cancel(doc, method=None) -> None:
	dt = (getattr(doc, "doctype", None) or "").strip()
	if dt not in ("Purchase Invoice", "Sales Invoice"):
		return
	rows = list(getattr(doc, "pdc_invoice_applications", None) or [])
	if not rows:
		return

	# Reverse only once.
	to_reverse = [
		r
		for r in rows
		if (r.application_status or "").strip() == "posted" and not (r.reversal_je or "").strip()
	]
	if not to_reverse:
		return

	# Safety: posted rows must have posted_je.
	for r in to_reverse:
		if not (getattr(r, "posted_je", None) or "").strip():
			frappe.throw(
				_("PDC Invoice Application row {0} is posted but has no Posted Journal Entry.").format(
					getattr(r, "name", None) or ""
				),
				title=_("PDC Advance"),
			)

	total = flt(sum(flt(getattr(r, "amount_in_pdc_currency", None) or 0) for r in to_reverse))
	if total <= 1e-9:
		return

	# Idempotency: reuse existing submitted reversal JE for this invoice+action.
	existing = _find_existing_submitted_reversal_je(dt, doc.name)
	if existing:
		_validate_existing_je_matches_expected_total(dt, doc.name, existing, total)
		je_name = existing
	else:
		# Build reversal payload by swapping debits/credits of the apply payload.
		payload = _build_application_je_payload(doc, dt, total)
		if not payload:
			# Do not silently skip reversal: if we cannot build a reversal JE, invoice cancel must fail
			# to avoid leaving posted application rows without a reversal.
			frappe.throw(
				_("Could not build reversal Journal Entry for PDC advance application (missing accounts)."),
				title=_("PDC Advance"),
			)
		for a in payload.get("accounts") or []:
			d = flt(a.get("debit_in_account_currency"))
			c = flt(a.get("credit_in_account_currency"))
			a["debit_in_account_currency"] = c
			a["credit_in_account_currency"] = d
			# Cancelled invoice cannot be referenced in JE rows in ERPNext. Keep traceability via:
			# - JE user_remark
			# - JE.pdc_advance_idempotency_key
			# - PDC Invoice Application.reversal_je
			a.pop("reference_type", None)
			a.pop("reference_name", None)
		payload["remarks"] = _advance_reversal_remarks(dt, doc.name)
		payload["pdc_advance_idempotency_key"] = _advance_reversal_idempotency_key(dt, doc.name)
		je_name = _post_je(payload)

	# If some rows already point to a different reversal JE, do not silently override.
	for r in rows:
		if (
			(r.application_status or "").strip() == "reversed"
			and (r.reversal_je or "").strip()
			and (r.reversal_je or "").strip() != je_name
		):
			frappe.throw(
				_(
					"Invoice already has reversed rows pointing to a different reversal Journal Entry ({0})."
				).format(r.reversal_je),
				title=_("PDC Advance"),
			)

	for r in to_reverse:
		frappe.db.set_value(
			"PDC Invoice Application",
			r.name,
			{"application_status": "reversed", "reversal_je": je_name},
			update_modified=False,
		)
		r.application_status = "reversed"
		r.reversal_je = je_name


__all__ = [
	"on_invoice_validate",
	"before_invoice_submit",
	"on_invoice_submit",
	"on_invoice_cancel",
]

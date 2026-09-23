# Copyright (c) 2026, Farbod Siyahpoosh and contributors
# For license information, please see license.txt

"""Post Dated Cheque **allocation** layer (child table ``allocations``).

Allocations tie cheque amounts to orders (advance) or invoices / payment requests (direct settlement)
for **reporting and planning**. They do **not** post GL vouchers — workflow-driven movement uses
:class:`~erpnext_extensions.cheque_management.doctype.post_dated_cheque.post_dated_cheque.PostDatedCheque`
and :func:`~erpnext_extensions.cheque_management.doctype.post_dated_cheque.post_dated_cheque.build_pdc_journal_entry_data` only.

Settlement capacity for Sales / Purchase Invoice and Payment Request is enforced against
``outstanding_amount`` (net of submitted Payment Entry) and effective PDC reservations; see
:mod:`~erpnext_extensions.cheque_management.pdc_settlement_capacity`.

See :meth:`erpnext_extensions.cheque_management.doctype.post_dated_cheque.post_dated_cheque.PostDatedCheque.validate` for call order.
"""

from __future__ import annotations

import re

import frappe
from frappe import _
from frappe.utils import cint, flt

from erpnext_extensions.cheque_management.pdc_advance_order_capacity import (
	get_order_remaining_advance_capacity,
)
from erpnext_extensions.cheque_management.pdc_settlement_capacity import (
	SETTLEMENT_REFERENCE_DOCTYPES,
	_effective_exclude_pdc_name,
	get_invoice_ledger_outstanding,
	get_invoice_remaining_capacity,
	get_pr_remaining_capacity,
	get_receivable_sales_invoice_direct_settlement_remaining_capacity,
	sum_effective_pdc_direct_to_invoice,
	sum_effective_pdc_via_pr_to_invoice,
)
from erpnext_extensions.cheque_management.pdc_workflow_state_machine import (
	CHEQUE_DIRECTION_PAYABLE,
	CHEQUE_DIRECTION_RECEIVABLE,
	WORKFLOW_CANCELLED,
	WORKFLOW_CLEARED,
	WORKFLOW_DRAFT,
	WORKFLOW_ISSUED,
	WORKFLOW_REGISTERED,
	WORKFLOW_REPLACED,
	WORKFLOW_RETURNED,
	normalize_workflow_state_value,
)

_EPS = 1e-6

_PR_INWARD = "Inward"
_PR_OUTWARD = "Outward"

ALLOCATION_MODE_ADVANCE = "advance"
ALLOCATION_MODE_DIRECT = "direct_settlement"

# direct_settlement: allocate to invoice-side refs (PI/SI) or Payment Request rows that settle those refs.
_DIRECT_SETTLEMENT_REF_DOCTYPES = frozenset({"Purchase Invoice", "Sales Invoice", "Payment Request"})
_ADVANCE_REF_DOCTYPES = frozenset({"Purchase Order", "Sales Order"})


def _pdc_diag_trace_enabled() -> bool:
	"""Enable verbose runtime tracing for PDC allocation validation.

	Temporary diagnostics are gated behind a flag to avoid noisy logs in production:
	- set `frappe.flags.pdc_diag_trace = True` (e.g. from console / a one-off hook), or
	- set `pdc_diag_trace: true` in `site_config.json`.
	"""
	try:
		if bool(getattr(frappe.flags, "pdc_diag_trace", False)):
			return True
	except Exception:
		# frappe.flags may be unbound in some test contexts
		pass
	try:
		# `frappe.conf` is not always the site config in all execution contexts; ask the site config directly.
		site_cfg = frappe.get_site_config(silent=True) if hasattr(frappe, "get_site_config") else None
		if site_cfg and site_cfg.get("pdc_diag_trace"):
			return True
		return bool(getattr(frappe, "conf", None) and frappe.conf.get("pdc_diag_trace"))
	except Exception:
		return False


def _pdc_diag_emit(title: str, payload: dict) -> None:
	"""Emit diagnostics to both a logger and Error Log (when enabled)."""
	if not _pdc_diag_trace_enabled():
		return
	try:
		frappe.logger("erpnext_extensions.cheque.pdc_diag").warning("%s %s", title, payload)
	except Exception:
		pass
	try:
		# Error Log is easiest to inspect on a live site without filesystem access.
		frappe.log_error(message=frappe.as_json(payload), title=title)
	except Exception:
		pass


def _pdc_diag_emit_force(title: str, payload: dict) -> None:
	"""Always try to emit to Error Log (best-effort).

	This is intentionally *not* gated: it is used only on failure paths to guarantee
	we can capture runtime evidence of the executed branch.
	"""
	try:
		frappe.log_error(message=frappe.as_json(payload), title=title)
	except Exception:
		pass


def _norm_lower(v) -> str:
	return (v or "").strip().lower()


def _norm_dir(v) -> str:
	"""Normalize cheque_direction for robust branching across installations."""
	s = _norm_lower(v)
	if s.startswith("receivable"):
		return "receivable"
	if s.startswith("payable"):
		return "payable"
	return s


def validate_post_dated_cheque_allocation_mode_immutability(doc) -> None:
	"""If ``allocation_mode_locked`` was set, ``allocation_mode`` must not change."""
	before = doc.get_doc_before_save() if hasattr(doc, "get_doc_before_save") else None
	if not before or not getattr(before, "name", None):
		return
	if not cint(getattr(before, "allocation_mode_locked", 0)):
		return
	prev = (getattr(before, "allocation_mode", None) or "").strip()
	cur = (getattr(doc, "allocation_mode", None) or "").strip()
	if prev != cur:
		frappe.throw(
			_("Allocation Mode cannot change because Allocation Mode Locked is set."),
			title=_("PDC Allocation"),
		)


def _payable_skip_pdc_settlement_capacity_validation(doc) -> bool:
	"""True when PI/PR rows must not be gated on PDC capacity (native register JE settles in ERPNext)."""
	direction = (getattr(doc, "cheque_direction", None) or "").strip()
	if direction != CHEQUE_DIRECTION_PAYABLE:
		return False
	ws = getattr(doc, "workflow_state", None)
	if is_pdc_allocation_effective(direction, ws):
		return True
	before = doc.get_doc_before_save() if hasattr(doc, "get_doc_before_save") else None
	if before is None:
		return False
	prev_raw = (
		before.get("workflow_state") if hasattr(before, "get") else getattr(before, "workflow_state", None)
	)
	prev = normalize_workflow_state_value(prev_raw)
	curr = normalize_workflow_state_value(ws)
	return prev == WORKFLOW_DRAFT and curr == WORKFLOW_REGISTERED


def _resolve_previous_pdc_workflow_state_raw(doc) -> tuple[str | None, str]:
	"""Return (previous_workflow_state_raw, source) where source is ``doc_before_save``, ``db``, or ``none``.

	ERPNext workflow actions often validate **without** a reliable :meth:`get_doc_before_save` snapshot; in
	that case the persisted row in ``tabPost Dated Cheque`` still holds the **previous** workflow state
	during :meth:`validate` (in-memory target state is updated, DB not yet committed).
	"""
	before = doc.get_doc_before_save() if hasattr(doc, "get_doc_before_save") else None
	if before is not None:
		raw = (
			before.get("workflow_state")
			if hasattr(before, "get")
			else getattr(before, "workflow_state", None)
		)
		if raw is not None and str(raw).strip() != "":
			return (str(raw).strip(), "doc_before_save")
	nm = getattr(doc, "name", None)
	if nm:
		try:
			if frappe.db.exists("Post Dated Cheque", nm):
				dbv = frappe.db.get_value("Post Dated Cheque", nm, "workflow_state")
				return (dbv, "db") if dbv is not None else (None, "db")
		except Exception:
			pass
	return (None, "none")


def _is_transition_draft_to_registered(doc) -> bool:
	"""True when this save targets Registered from a stored previous state of Draft."""
	if not getattr(doc, "name", None):
		return False
	curr = normalize_workflow_state_value(getattr(doc, "workflow_state", None))
	if curr != WORKFLOW_REGISTERED:
		return False
	prev_raw, _src = _resolve_previous_pdc_workflow_state_raw(doc)
	if prev_raw is None:
		return False
	return normalize_workflow_state_value(prev_raw) == WORKFLOW_DRAFT


def _log_pdc_si_direct_settlement_capacity_trace(
	doc,
	row_index: int,
	ref_nm: str,
	snap: dict,
	*,
	transition_draft_to_registered: bool,
	exclude_arg: str | None,
	exclude_resolved: str,
	snap_os: float,
	ledger_os: float,
	pending_direct: float,
	pending_via: float,
	available: float,
) -> None:
	"""Temporary structured trace for runtime diagnosis (grep ``[PDC_SI_CAPACITY]`` in logs)."""
	try:
		log = frappe.logger("erpnext_extensions.cheque.pdc_allocation")
		prev_raw, prev_src = _resolve_previous_pdc_workflow_state_raw(doc)
		log.info(
			"[PDC_SI_CAPACITY] pdc=%s direction=%s allocation_mode=%s row=%s ref=Sales Invoice/%s "
			"prev_ws_raw=%r prev_src=%s curr_ws=%r draft_to_registered=%s "
			"snap_outstanding=%s ledger_os=%s pending_direct=%s pending_via=%s exclude_arg=%r exclude_resolved=%r "
			"available=%s",
			getattr(doc, "name", None),
			(getattr(doc, "cheque_direction", None) or "").strip(),
			(getattr(doc, "allocation_mode", None) or "").strip(),
			row_index,
			ref_nm,
			prev_raw,
			prev_src,
			getattr(doc, "workflow_state", None),
			transition_draft_to_registered,
			snap_os,
			ledger_os,
			pending_direct,
			pending_via,
			exclude_arg,
			exclude_resolved,
			available,
		)
	except Exception:
		pass


def pdc_allocation_effective_milestone_workflow_state(cheque_direction: str | None) -> str | None:
	if cheque_direction == CHEQUE_DIRECTION_RECEIVABLE:
		return WORKFLOW_REGISTERED
	if cheque_direction == CHEQUE_DIRECTION_PAYABLE:
		return WORKFLOW_REGISTERED
	return None


def is_pdc_allocation_effective(cheque_direction: str | None, workflow_state: str | None) -> bool:
	ws = normalize_workflow_state_value(workflow_state)
	if cheque_direction == CHEQUE_DIRECTION_RECEIVABLE:
		return ws != WORKFLOW_DRAFT
	if cheque_direction == CHEQUE_DIRECTION_PAYABLE:
		return ws in (
			WORKFLOW_REGISTERED,
			WORKFLOW_ISSUED,
			WORKFLOW_CLEARED,
			WORKFLOW_RETURNED,
			WORKFLOW_REPLACED,
			WORKFLOW_CANCELLED,
		)
	return False


def is_pdc_allocation_draft_only(cheque_direction: str | None, workflow_state: str | None) -> bool:
	if not pdc_allocation_effective_milestone_workflow_state(cheque_direction):
		return True
	return not is_pdc_allocation_effective(cheque_direction, workflow_state)


def apply_pdc_allocation_row_defaults_from_parent(doc) -> None:
	"""Fill missing company / party on child rows from the Post Dated Cheque header."""
	co = (getattr(doc, "company", None) or "").strip()
	pt = getattr(doc, "party_type", None)
	py = getattr(doc, "party", None)
	for row in getattr(doc, "allocations", None) or []:
		if co and not (getattr(row, "company", None) or "").strip():
			row.company = co
		if pt and not (getattr(row, "party_type", None) or "").strip():
			row.party_type = pt
		if py and not (getattr(row, "party", None) or "").strip():
			row.party = py


def sync_single_pdc_allocation_on_reduced_cheque_amount(doc) -> bool:
	"""Fix stale single-row allocation when Draft ``cheque_amount`` is reduced below that row.

	Safe because a single allocation row has an unambiguous target: clamp it to the new
	cheque amount. Multi-row allocations are **not** auto-redistributed — choosing which
	row to shrink is a business decision and must remain user-driven; the existing
	``Allocated Amount cannot exceed Cheque Amount`` guard still rejects those cases.

	Does **not**:
	- run on submitted/cancelled PDCs (``docstatus != 0``);
	- increase allocation when cheque amount rises (preserves manual under-allocation);
	- touch rows when there is more than one allocation.

	Returns True when a row amount was adjusted.
	"""
	if cint(getattr(doc, "docstatus", 0) or 0) != 0:
		return False

	rows = list(getattr(doc, "allocations", None) or [])
	if len(rows) != 1:
		return False

	cheque_amt = flt(getattr(doc, "cheque_amount", None))
	if cheque_amt <= 0:
		return False

	row = rows[0]
	row_amt = flt(getattr(row, "amount", None))
	# Stale over-allocation only (cheque reduced below existing single allocation).
	if row_amt <= cheque_amt + _EPS:
		return False

	row.amount = cheque_amt
	return True


def sync_pdc_allocation_summary_amounts(doc) -> None:
	"""Set ``allocated_amount`` / ``unallocated_amount`` from child ``amount``; enforce sum ≤ ``cheque_amount``."""
	total = 0.0
	for row in doc.allocations or []:
		total += flt(getattr(row, "amount", None))

	cheque_amt = float(getattr(doc, "cheque_amount", None) or 0)
	doc.allocated_amount = total
	doc.unallocated_amount = cheque_amt - total

	if cheque_amt and total > cheque_amt + _EPS:
		frappe.throw(
			_("Allocated Amount ({0}) cannot exceed Cheque Amount ({1}).").format(
				doc.allocated_amount, doc.cheque_amount
			),
			title=_("PDC Allocation"),
		)


def _parse_other_settlement_allowlist(raw: str | None) -> frozenset[str]:
	if not raw:
		return frozenset()
	parts = re.split(r"[\s,]+", raw.strip())
	return frozenset(p.strip() for p in parts if p.strip())


def get_pdc_other_settlement_allowlist(company: str | None) -> frozenset[str]:
	if not (company or "").strip():
		return frozenset()
	try:
		raw = frappe.db.get_value("PDC Settings", company, "other_settlement_allowed_doctypes")
	except RuntimeError:
		return frozenset()
	return _parse_other_settlement_allowlist(raw)


def _pdc_effective_currency(doc) -> str | None:
	cur = (getattr(doc, "currency", None) or "").strip()
	if cur:
		return cur
	co = (getattr(doc, "company", None) or "").strip()
	if not co:
		return None
	return frappe.db.get_value("Company", co, "default_currency")


def _read_sales_invoice_for_pdc_allocation(name: str) -> dict | None:
	if not name or not frappe.db.exists("Sales Invoice", name):
		return None
	return frappe.db.get_value(
		"Sales Invoice",
		name,
		["company", "currency", "customer", "docstatus", "outstanding_amount"],
		as_dict=True,
	)


def _read_purchase_invoice_for_pdc_allocation(name: str) -> dict | None:
	if not name or not frappe.db.exists("Purchase Invoice", name):
		return None
	return frappe.db.get_value(
		"Purchase Invoice",
		name,
		["company", "currency", "supplier", "docstatus", "outstanding_amount"],
		as_dict=True,
	)


def _read_sales_order_for_pdc_allocation(name: str) -> dict | None:
	if not name or not frappe.db.exists("Sales Order", name):
		return None
	return frappe.db.get_value(
		"Sales Order",
		name,
		["company", "currency", "customer", "docstatus", "status"],
		as_dict=True,
	)


def _read_purchase_order_for_pdc_allocation(name: str) -> dict | None:
	if not name or not frappe.db.exists("Purchase Order", name):
		return None
	return frappe.db.get_value(
		"Purchase Order",
		name,
		["company", "currency", "supplier", "docstatus", "status"],
		as_dict=True,
	)


def _read_payment_request_for_pdc_allocation(name: str) -> dict | None:
	if not name or not frappe.db.exists("Payment Request", name):
		return None
	return frappe.db.get_value(
		"Payment Request",
		name,
		[
			"company",
			"currency",
			"party_type",
			"party",
			"payment_request_type",
			"docstatus",
			"workflow_state",
			"outstanding_amount",
			"status",
		],
		as_dict=True,
	)


def _party_matches_pdc_snapshot(
	cheque_direction: str,
	party_type: str | None,
	party: str | None,
	ref_doctype: str,
	snap: dict,
) -> bool:
	pt = (party_type or "").strip()
	pp = (party or "").strip()
	if ref_doctype == "Sales Invoice":
		return snap.get("customer") == pp
	if ref_doctype == "Purchase Invoice":
		return snap.get("supplier") == pp
	if ref_doctype == "Sales Order":
		return snap.get("customer") == pp
	if ref_doctype == "Purchase Order":
		return snap.get("supplier") == pp
	if ref_doctype == "Payment Request":
		return snap.get("party_type") == pt and snap.get("party") == pp
	if snap.get("party_type") is not None and snap.get("party") is not None:
		return snap.get("party_type") == pt and snap.get("party") == pp
	if snap.get("party_anchor") == "customer" and cheque_direction == CHEQUE_DIRECTION_RECEIVABLE:
		return snap.get("customer") == pp
	if snap.get("party_anchor") == "supplier" and cheque_direction == CHEQUE_DIRECTION_PAYABLE:
		return snap.get("supplier") == pp
	return False


def _currency_matches(pdc_currency: str | None, ref_currency: str | None) -> bool:
	a = (pdc_currency or "").strip()
	b = (ref_currency or "").strip()
	return bool(a and b and a == b)


def _ref_submitted_not_cancelled(ref_dt: str, snap: dict) -> bool:
	ds = int(snap.get("docstatus") or 0)
	if ds == 2:
		return False
	if ds != 1:
		return False
	st = (snap.get("status") or "").strip()
	if ref_dt in ("Purchase Order", "Sales Order") and st in ("Cancelled", "Closed"):
		# Closed orders: not allocatable for new advances (stricter policy can be added later)
		if st == "Cancelled":
			return False
	return True


def sanitize_pdc_allocation_child_rows(doc) -> None:
	"""Remove allocation rows that are completely empty."""
	alloc = getattr(doc, "allocations", None) or []
	if not alloc:
		return
	for row in list(alloc):
		amt = flt(getattr(row, "amount", None))
		mode = (getattr(row, "allocation_mode", None) or "").strip()
		rdt = (getattr(row, "reference_doctype", None) or "").strip()
		rnm = (getattr(row, "reference_name", None) or "").strip()
		if amt <= 0 and not mode and not rdt and not rnm:
			doc.remove(row)


def autofill_pdc_allocations_from_parent_reference(doc) -> None:
	"""Prefill incomplete allocation rows when PDC is created from SI/PI (parent ``reference_doctype`` / ``reference_name``)."""
	pdt = (getattr(doc, "reference_doctype", None) or "").strip()
	pnm = (getattr(doc, "reference_name", None) or "").strip()
	if pdt not in ("Sales Invoice", "Purchase Invoice") or not pnm:
		return
	if not (getattr(doc, "allocation_mode", None) or "").strip():
		doc.allocation_mode = ALLOCATION_MODE_DIRECT
	ch = flt(getattr(doc, "cheque_amount", None))
	for row in getattr(doc, "allocations", None) or []:
		rdt = (getattr(row, "reference_doctype", None) or "").strip()
		rnm = (getattr(row, "reference_name", None) or "").strip()
		if rdt and rnm:
			continue
		if not (getattr(row, "allocation_mode", None) or "").strip():
			row.allocation_mode = ALLOCATION_MODE_DIRECT
		row.reference_doctype = pdt
		row.reference_name = pnm
		if ch > 0 and flt(getattr(row, "amount", None)) <= 0:
			row.amount = ch


def _validate_pdc_allocation_source_trace(row, idx: int) -> None:
	sdt = (getattr(row, "source_doctype", None) or "").strip()
	snm = (getattr(row, "source_name", None) or "").strip()
	if bool(sdt) ^ bool(snm):
		frappe.throw(
			_("Allocation row {0}: Source DocType and Source Name must both be set or both empty.").format(
				idx
			),
			title=_("PDC Allocation"),
		)
	if not sdt and not snm:
		return
	if sdt != "Payment Request":
		frappe.throw(
			_("Allocation row {0}: Only Payment Request is allowed as Source DocType when set.").format(idx),
			title=_("PDC Allocation"),
		)
	try:
		exists = frappe.db.exists("Payment Request", snm)
	except RuntimeError:
		# Unit tests may run without bound frappe.local / DB context.
		exists = True
	if not exists:
		frappe.throw(
			_("Allocation row {0}: Payment Request {1} was not found.").format(idx, snm),
			title=_("PDC Allocation"),
		)


def _validate_direction_vs_reference_doctype(direction: str, ref_dt: str, idx: int) -> None:
	if ref_dt == "Sales Invoice" and direction == CHEQUE_DIRECTION_PAYABLE:
		frappe.throw(
			_("Allocation row {0}: Payable PDC cannot allocate to Sales Invoice.").format(idx),
			title=_("PDC Allocation"),
		)
	if ref_dt == "Purchase Invoice" and direction == CHEQUE_DIRECTION_RECEIVABLE:
		frappe.throw(
			_("Allocation row {0}: Receivable PDC cannot allocate to Purchase Invoice.").format(idx),
			title=_("PDC Allocation"),
		)
	if ref_dt == "Sales Order" and direction == CHEQUE_DIRECTION_PAYABLE:
		frappe.throw(
			_("Allocation row {0}: Payable PDC cannot allocate to Sales Order.").format(idx),
			title=_("PDC Allocation"),
		)
	if ref_dt == "Purchase Order" and direction == CHEQUE_DIRECTION_RECEIVABLE:
		frappe.throw(
			_("Allocation row {0}: Receivable PDC cannot allocate to Purchase Order.").format(idx),
			title=_("PDC Allocation"),
		)


def validate_pdc_allocation_rows(doc) -> None:
	"""Validate ``allocations`` child rows (allocation_mode, references, amounts, party/currency, trace)."""
	if not (doc.allocations or []):
		return

	header_mode = (getattr(doc, "allocation_mode", None) or "").strip()
	if not header_mode:
		frappe.throw(
			_("Set Allocation Mode on Post Dated Cheque before saving allocation rows."),
			title=_("PDC Allocation"),
		)
	if header_mode not in (ALLOCATION_MODE_ADVANCE, ALLOCATION_MODE_DIRECT):
		frappe.throw(
			_("Invalid Allocation Mode {0}.").format(header_mode),
			title=_("PDC Allocation"),
		)

	company = (getattr(doc, "company", None) or "").strip()
	party_type = (getattr(doc, "party_type", None) or "").strip()
	party = (getattr(doc, "party", None) or "").strip()
	direction = (getattr(doc, "cheque_direction", None) or "").strip()
	pdc_currency = _pdc_effective_currency(doc)
	parent_name = getattr(doc, "name", None)
	allocation_mode = (getattr(doc, "allocation_mode", None) or "").strip()
	diag_base = {
		"pdc_name": (parent_name or "").strip() or None,
		"cheque_direction": direction,
		"cheque_direction_norm": _norm_dir(direction),
		"allocation_mode": allocation_mode,
		"workflow_state": (getattr(doc, "workflow_state", None) or "").strip(),
	}

	if not company:
		frappe.throw(_("Company is required before validating allocations."), title=_("PDC Allocation"))
	if not pdc_currency:
		frappe.throw(
			_("Currency could not be resolved for this Post Dated Cheque; set Currency or Company."),
			title=_("PDC Allocation"),
		)

	total = 0.0
	seen_ref_pairs: set[tuple[str, str]] = set()

	for i, row in enumerate(doc.allocations or [], start=1):
		row_mode = (getattr(row, "allocation_mode", None) or "").strip()
		if row_mode != header_mode:
			frappe.throw(
				_("Allocation row {0}: Allocation Mode must match Post Dated Cheque ({1}).").format(
					i, header_mode
				),
				title=_("PDC Allocation"),
			)

		amt = flt(getattr(row, "amount", None))
		if amt <= 0:
			frappe.throw(
				_("Allocation row {0}: Amount must be greater than zero.").format(i),
				title=_("PDC Allocation"),
			)
		total += amt

		ref_dt = (getattr(row, "reference_doctype", None) or "").strip()
		ref_nm = (getattr(row, "reference_name", None) or "").strip()
		if not ref_dt or not ref_nm:
			frappe.throw(
				_("Allocation row {0}: Reference DocType and Reference Name are required.").format(i),
				title=_("PDC Allocation"),
			)

		if header_mode == ALLOCATION_MODE_ADVANCE:
			if ref_dt not in _ADVANCE_REF_DOCTYPES:
				frappe.throw(
					_(
						"Allocation row {0}: Advance mode allows only Purchase Order or Sales Order references."
					).format(i),
					title=_("PDC Allocation"),
				)
		else:
			if ref_dt not in _DIRECT_SETTLEMENT_REF_DOCTYPES:
				frappe.throw(
					_(
						"Allocation row {0}: Direct settlement mode allows only Purchase Invoice, Sales Invoice, or Payment Request references."
					).format(i),
					title=_("PDC Allocation"),
				)

		_validate_direction_vs_reference_doctype(direction, ref_dt, i)
		_validate_pdc_allocation_source_trace(row, i)

		row_co = (getattr(row, "company", None) or "").strip()
		row_pt = (getattr(row, "party_type", None) or "").strip()
		row_py = (getattr(row, "party", None) or "").strip()
		if row_co != company:
			frappe.throw(
				_("Allocation row {0}: Company must match Post Dated Cheque.").format(i),
				title=_("PDC Allocation"),
			)
		if row_pt != party_type:
			frappe.throw(
				_("Allocation row {0}: Party Type must match Post Dated Cheque.").format(i),
				title=_("PDC Allocation"),
			)
		if row_py != party:
			frappe.throw(
				_("Allocation row {0}: Party must match Post Dated Cheque.").format(i),
				title=_("PDC Allocation"),
			)

		cur_row = (getattr(row, "currency", None) or "").strip()
		if cur_row and cur_row != pdc_currency:
			frappe.throw(
				_("Allocation row {0}: Row currency must match Post Dated Cheque currency ({1}).").format(
					i, pdc_currency
				),
				title=_("PDC Allocation"),
			)

		key = (ref_dt, ref_nm)
		if key in seen_ref_pairs:
			frappe.throw(
				_("Allocation row {0}: Duplicate reference to {1} {2}.").format(i, ref_dt, ref_nm),
				title=_("PDC Allocation"),
			)
		seen_ref_pairs.add(key)

	cheque_amt = float(getattr(doc, "cheque_amount", None) or 0)
	if cheque_amt and total > cheque_amt + _EPS:
		frappe.throw(
			_("Total allocated amount ({0}) cannot exceed Cheque Amount ({1}).").format(
				total, doc.cheque_amount
			),
			title=_("PDC Allocation"),
		)

	# Second pass: referenced documents exist, submitted, currency/party, optional settlement capacity
	for i, row in enumerate(doc.allocations or [], start=1):
		header_mode = (getattr(doc, "allocation_mode", None) or "").strip()
		ref_dt = (getattr(row, "reference_doctype", None) or "").strip()
		ref_nm = (getattr(row, "reference_name", None) or "").strip()
		amt = flt(getattr(row, "amount", None))

		if header_mode == ALLOCATION_MODE_ADVANCE:
			if ref_dt == "Sales Order":
				snap = _read_sales_order_for_pdc_allocation(ref_nm)
			elif ref_dt == "Purchase Order":
				snap = _read_purchase_order_for_pdc_allocation(ref_nm)
			else:
				snap = None
			if not snap:
				frappe.throw(
					_("Allocation row {0}: {1} {2} was not found.").format(i, ref_dt, ref_nm),
					title=_("PDC Allocation"),
				)
			if not _ref_submitted_not_cancelled(ref_dt, snap):
				frappe.throw(
					_("Allocation row {0}: {1} {2} must be submitted and not cancelled.").format(
						i, ref_dt, ref_nm
					),
					title=_("PDC Allocation"),
				)
			if (snap.get("company") or "").strip() != company:
				frappe.throw(
					_("Allocation row {0}: Company on {1} does not match this Post Dated Cheque.").format(
						i, ref_dt
					),
					title=_("PDC Allocation"),
				)
			if not _currency_matches(pdc_currency, snap.get("currency")):
				frappe.throw(
					_(
						"Allocation row {0}: Currency on {1} must match the Post Dated Cheque currency ({2})."
					).format(i, ref_dt, pdc_currency),
					title=_("PDC Allocation"),
				)
			if not _party_matches_pdc_snapshot(direction, party_type, party, ref_dt, snap):
				frappe.throw(
					_("Allocation row {0}: Party on {1} does not match this Post Dated Cheque.").format(
						i, ref_dt
					),
					title=_("PDC Allocation"),
				)
			# v1 Advance ceiling: reserve order capacity across multiple PDCs (draft included).
			# Compare total allocated to this order in THIS doc vs remaining capacity excluding this PDC.
			order_total_in_doc = 0.0
			for r2 in doc.allocations or []:
				if (getattr(r2, "allocation_mode", None) or "").strip() != ALLOCATION_MODE_ADVANCE:
					continue
				if (getattr(r2, "reference_doctype", None) or "").strip() != ref_dt:
					continue
				if (getattr(r2, "reference_name", None) or "").strip() != ref_nm:
					continue
				order_total_in_doc += flt(getattr(r2, "amount", None))
			remaining = get_order_remaining_advance_capacity(ref_dt, ref_nm, exclude_pdc=parent_name)
			if order_total_in_doc > remaining + _EPS:
				frappe.throw(
					_(
						"Advance allocation exceeds remaining order capacity on {0} {1}. "
						"Remaining advance capacity is {2}."
					).format(ref_dt, ref_nm, remaining),
					title=_("PDC Allocation"),
				)
			continue

		# direct_settlement
		snap: dict | None = None
		if ref_dt == "Sales Invoice":
			snap = _read_sales_invoice_for_pdc_allocation(ref_nm)
		elif ref_dt == "Purchase Invoice":
			snap = _read_purchase_invoice_for_pdc_allocation(ref_nm)
		elif ref_dt == "Payment Request":
			snap = _read_payment_request_for_pdc_allocation(ref_nm)
		else:
			frappe.throw(
				_("Allocation row {0}: Unexpected reference DocType {1} for direct settlement.").format(
					i, ref_dt
				),
				title=_("PDC Allocation"),
			)

		if not snap:
			frappe.throw(
				_("Allocation row {0}: {1} {2} was not found.").format(i, ref_dt, ref_nm),
				title=_("PDC Allocation"),
			)

		if snap.get("docstatus") == 2:
			frappe.throw(
				_("Allocation row {0}: {1} {2} is cancelled.").format(i, ref_dt, ref_nm),
				title=_("PDC Allocation"),
			)

		if ref_dt == "Payment Request":
			from erpnext_extensions.cheque_management.pdc_payment_request_eligibility import (
				is_payment_request_settlement_eligible,
			)

			if not is_payment_request_settlement_eligible(snap):
				frappe.throw(
					_(
						"Allocation row {0}: Payment Request {1} must be in an approved / settlement-eligible workflow state (or submitted when no workflow applies)."
					).format(i, ref_nm),
					title=_("PDC Allocation"),
				)
		elif ref_dt in ("Sales Invoice", "Purchase Invoice") and int(snap.get("docstatus") or 0) != 1:
			frappe.throw(
				_("Allocation row {0}: {1} {2} must be submitted.").format(i, ref_dt, ref_nm),
				title=_("PDC Allocation"),
			)

		if (snap.get("company") or "").strip() != company:
			frappe.throw(
				_("Allocation row {0}: Company on {1} does not match this Post Dated Cheque.").format(
					i, ref_dt
				),
				title=_("PDC Allocation"),
			)

		if not _currency_matches(pdc_currency, snap.get("currency")):
			frappe.throw(
				_(
					"Allocation row {0}: Currency on {1} must match the Post Dated Cheque currency ({2})."
				).format(i, ref_dt, pdc_currency),
				title=_("PDC Allocation"),
			)

		if not _party_matches_pdc_snapshot(direction, party_type, party, ref_dt, snap):
			frappe.throw(
				_("Allocation row {0}: Party on {1} does not match this Post Dated Cheque.").format(
					i, ref_dt
				),
				title=_("PDC Allocation"),
			)

		if ref_dt == "Payment Request":
			pr_type = (snap.get("payment_request_type") or "").strip()
			if direction == CHEQUE_DIRECTION_RECEIVABLE and pr_type != _PR_INWARD:
				frappe.throw(
					_(
						"Allocation row {0}: Receivable PDC may only allocate to Inward Payment Requests."
					).format(i),
					title=_("PDC Allocation"),
				)
			if direction == CHEQUE_DIRECTION_PAYABLE and pr_type != _PR_OUTWARD:
				frappe.throw(
					_(
						"Allocation row {0}: Payable PDC may only allocate to Outward Payment Requests."
					).format(i),
					title=_("PDC Allocation"),
				)

		outstanding = flt(snap.get("outstanding_amount"))
		if ref_dt in SETTLEMENT_REFERENCE_DOCTYPES:
			# Re-entrant internal PDC self-save after posting a JE should not re-block the same transition.
			# Scoped flag is set only around that internal save in pdc_journal_entry_service.
			try:
				skip_for = getattr(frappe.flags, "skip_pdc_allocation_capacity_validation_for_pdc", None)
			except Exception:
				skip_for = None
			if skip_for and (str(skip_for).strip() == str(parent_name or "").strip()):
				_pdc_diag_emit(
					"[PDC_VALIDATE] capacity_skipped_reentrant_save",
					{
						**diag_base,
						"allocation_row": i,
						"reference_doctype": ref_dt,
						"reference_name": ref_nm,
						"amount": amt,
						"skip_flag": "skip_pdc_allocation_capacity_validation_for_pdc",
						"skip_for": str(skip_for).strip(),
					},
				)
				continue
			if direction == CHEQUE_DIRECTION_PAYABLE and _payable_skip_pdc_settlement_capacity_validation(
				doc
			):
				continue
			# Receivable + Sales Invoice: use invoice snapshot outstanding (desk field), not Payment Ledger.
			# Runtime: QueryPaymentLedger can be 0 before Register JE posts while tabSales Invoice.outstanding_amount is correct.
			is_si_receivable = (ref_dt == "Sales Invoice") and (_norm_dir(direction) == "receivable")
			if is_si_receivable:
				prev_ws_raw, prev_src = _resolve_previous_pdc_workflow_state_raw(doc)
				cur_ws_raw = (getattr(doc, "workflow_state", None) or "").strip()
				transition = _is_transition_draft_to_registered(doc)
				_pdc_diag_emit(
					"[PDC_SI_VALIDATE] enter",
					{
						**diag_base,
						"allocation_row": i,
						"reference_doctype": ref_dt,
						"reference_name": ref_nm,
						"amount": amt,
						"previous_workflow_state_raw": prev_ws_raw,
						"previous_workflow_state_source": prev_src,
						"current_workflow_state_raw": cur_ws_raw,
						"transition_draft_to_registered": transition,
						"branch": "SI_RECEIVABLE_SNAPSHOT_CAPACITY",
						"outstanding_provider": "Sales Invoice.outstanding_amount (snapshot via frappe.db.get_value)",
					},
				)
				snap_os = flt(snap.get("outstanding_amount"))
				ledger_os = flt(get_invoice_ledger_outstanding(ref_dt, ref_nm))
				excl_resolved = _effective_exclude_pdc_name(parent_name)
				pending_direct = flt(
					sum_effective_pdc_direct_to_invoice(ref_dt, ref_nm, exclude_pdc=parent_name)
				)
				pending_via = flt(
					sum_effective_pdc_via_pr_to_invoice(ref_dt, ref_nm, exclude_pdc=parent_name)
				)
				available = get_receivable_sales_invoice_direct_settlement_remaining_capacity(
					ref_nm, invoice_outstanding_amount=snap_os, exclude_pdc=parent_name
				)
				ref_os_display = snap_os
				_pdc_diag_emit(
					"[PDC_SI_VALIDATE] values",
					{
						**diag_base,
						"allocation_row": i,
						"reference_doctype": ref_dt,
						"reference_name": ref_nm,
						"exclude_pdc_arg": (parent_name or "").strip() or None,
						"exclude_pdc_resolved": excl_resolved,
						"sales_invoice_snapshot_outstanding_amount": snap_os,
						"ledger_outstanding_helper": ledger_os,
						"pending_direct_effective_sum": pending_direct,
						"pending_via_pr_effective_sum": pending_via,
						"remaining_capacity_helper": available,
					},
				)
				_log_pdc_si_direct_settlement_capacity_trace(
					doc,
					i,
					ref_nm,
					snap,
					transition_draft_to_registered=_is_transition_draft_to_registered(doc),
					exclude_arg=parent_name,
					exclude_resolved=excl_resolved,
					snap_os=snap_os,
					ledger_os=ledger_os,
					pending_direct=pending_direct,
					pending_via=pending_via,
					available=available,
				)
				if amt > available + _EPS:
					_pdc_diag_emit(
						"[PDC_SI_VALIDATE] throw",
						{
							**diag_base,
							"allocation_row": i,
							"reference_doctype": ref_dt,
							"reference_name": ref_nm,
							"amount": amt,
							"sales_invoice_snapshot_outstanding_amount": snap_os,
							"ledger_outstanding_helper": ledger_os,
							"remaining_capacity_helper": available,
							"exclude_pdc_arg": (parent_name or "").strip() or None,
							"exclude_pdc_resolved": excl_resolved,
							"throw_ref_outstanding_display": ref_os_display,
							"throw_remaining_capacity": available,
						},
					)
					# GUARANTEED runtime trace for the failing request (Error Log).
					_pdc_diag_emit_force(
						"[PDC_SI_VALIDATE_FORCE] throw",
						{
							**diag_base,
							"allocation_row": i,
							"reference_doctype": ref_dt,
							"reference_name": ref_nm,
							"branch": "SI_RECEIVABLE_SNAPSHOT_CAPACITY",
							"transition_draft_to_registered": _is_transition_draft_to_registered(doc),
							"sales_invoice_snapshot_outstanding_amount": snap_os,
							"ledger_outstanding_helper": ledger_os,
							"remaining_capacity_helper": available,
							"exclude_pdc_resolved": excl_resolved,
						},
					)
					frappe.throw(
						_(
							"Allocation row {0}: Amount ({1}) exceeds remaining settlement capacity on {2} {3}. "
							"Reference outstanding is {4}; remaining capacity for this cheque is {5}."
						).format(i, amt, ref_dt, ref_nm, ref_os_display, available),
						title=_("PDC Allocation"),
					)
				continue
			if ref_dt == "Payment Request":
				available = get_pr_remaining_capacity(ref_nm, exclude_pdc=parent_name)
				ref_os_display = outstanding
			elif ref_dt == "Purchase Invoice":
				available = get_invoice_remaining_capacity(ref_dt, ref_nm, exclude_pdc=parent_name)
				ref_os_display = flt(get_invoice_ledger_outstanding(ref_dt, ref_nm))
			else:
				# Sales Invoice with unexpected direction (should be blocked earlier); keep ledger path defensively.
				available = get_invoice_remaining_capacity(ref_dt, ref_nm, exclude_pdc=parent_name)
				ref_os_display = flt(get_invoice_ledger_outstanding(ref_dt, ref_nm))
			_pdc_diag_emit(
				"[PDC_VALIDATE] generic_capacity_branch",
				{
					**diag_base,
					"allocation_row": i,
					"reference_doctype": ref_dt,
					"reference_name": ref_nm,
					"amount": amt,
					"branch": "GENERIC_LEDGER_CAPACITY",
					"reference_outstanding_display": ref_os_display,
					"remaining_capacity_helper": available,
					"exclude_pdc_arg": (parent_name or "").strip() or None,
					"exclude_pdc_resolved": _effective_exclude_pdc_name(parent_name),
				},
			)
			if amt > available + _EPS:
				frappe.throw(
					_(
						"Allocation row {0}: Amount ({1}) exceeds remaining settlement capacity on {2} {3}. "
						"Reference outstanding is {4}; remaining capacity for this cheque is {5}."
					).format(i, amt, ref_dt, ref_nm, ref_os_display, available),
					title=_("PDC Allocation"),
				)


def validate_pdc_allocation_workflow_milestone(doc) -> None:
	if not (doc.allocations or []):
		return
	ws = normalize_workflow_state_value(getattr(doc, "workflow_state", None))
	if (
		doc.cheque_direction == CHEQUE_DIRECTION_RECEIVABLE
		and ws == WORKFLOW_DRAFT
		and is_pdc_allocation_effective(doc.cheque_direction, doc.workflow_state)
	):
		frappe.throw(
			_("Receivable cheque allocation cannot be effective before Registered."),
			title=_("PDC Allocation"),
		)
	if (
		doc.cheque_direction == CHEQUE_DIRECTION_PAYABLE
		and ws == WORKFLOW_DRAFT
		and is_pdc_allocation_effective(doc.cheque_direction, doc.workflow_state)
	):
		frappe.throw(
			_("Payable cheque allocation cannot be effective before Registered."),
			title=_("PDC Allocation"),
		)


__all__ = [
	"ALLOCATION_MODE_ADVANCE",
	"ALLOCATION_MODE_DIRECT",
	"autofill_pdc_allocations_from_parent_reference",
	"apply_pdc_allocation_row_defaults_from_parent",
	"get_pdc_other_settlement_allowlist",
	"is_pdc_allocation_draft_only",
	"is_pdc_allocation_effective",
	"pdc_allocation_effective_milestone_workflow_state",
	"sanitize_pdc_allocation_child_rows",
	"sync_single_pdc_allocation_on_reduced_cheque_amount",
	"sync_pdc_allocation_summary_amounts",
	"validate_post_dated_cheque_allocation_mode_immutability",
	"validate_pdc_allocation_rows",
	"validate_pdc_allocation_workflow_milestone",
]

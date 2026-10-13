# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.5.42 — block Purchase Invoice cancel while referenced by an active PM Clearance.

Also locks referenced PIs during Clearance validate so cancel-vs-save races cannot
commit both a cancelled PI and an active Clearance link.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt

from erpnext_extensions.petty_management.services.clearance_reservation import (
	clearance_reserves_pm_request_balance_sql,
)
from erpnext_extensions.petty_management.services.constants import SETTLEMENT_PI

# Named lock prefix for PI ↔ Clearance reference races (deterministic with sorted names).
_PI_CLR_LOCK_PREFIX = "pm_pi_clr:"
_PI_CLR_LOCK_TIMEOUT_SEC = 30


def _iter_pi_names_from_clearance(doc: Document | dict) -> list[str]:
	details = doc.get("details") if hasattr(doc, "get") else (doc or {}).get("details")
	names: list[str] = []
	seen: set[str] = set()
	for row in details or []:
		settlement = (getattr(row, "settlement_type", None) or row.get("settlement_type") or SETTLEMENT_PI).strip()
		if settlement != SETTLEMENT_PI:
			continue
		pi = (getattr(row, "purchase_invoice", None) or row.get("purchase_invoice") or "").strip()
		if pi and pi not in seen:
			seen.add(pi)
			names.append(pi)
	return names


def _acquire_pi_named_locks(pi_names: list[str]) -> None:
	"""Acquire MariaDB named locks in sorted order; released on commit/rollback."""
	keys = [f"{_PI_CLR_LOCK_PREFIX}{n}" for n in sorted({(n or "").strip() for n in pi_names if n})]
	if not keys:
		return
	held: list[str] = []
	try:
		for key in keys:
			got = frappe.db.sql("SELECT GET_LOCK(%s, %s)", (key, _PI_CLR_LOCK_TIMEOUT_SEC))
			if not got or int(got[0][0] or 0) != 1:
				raise frappe.ValidationError(
					_("Could not acquire Purchase Invoice clearance lock ({0}). Please retry.").format(key)
				)
			held.append(key)
	except Exception:
		for key in held:
			try:
				frappe.db.sql("SELECT RELEASE_LOCK(%s)", (key,))
			except Exception:
				pass
		raise

	def _release() -> None:
		for key in held:
			try:
				frappe.db.sql("SELECT RELEASE_LOCK(%s)", (key,))
			except Exception:
				pass

	frappe.db.after_commit.add(_release)
	frappe.db.after_rollback.add(_release)


def find_active_clearances_referencing_pi(
	purchase_invoice: str, *, for_update: bool = False
) -> list[str]:
	"""Return distinct active PM Clearance names that reference the Purchase Invoice.

	Active = same lifecycle as funding reservation (docstatus < 2 and status not
	Cancelled/Rejected). Deleted rows are absent. Cancelled/Rejected clearances
	do not block.

	When ``for_update`` is True (cancel path), use locking reads so REPEATABLE READ
	snapshots cannot hide a Clearance that just committed on another connection.
	"""
	pi = (purchase_invoice or "").strip()
	if not pi:
		return []
	active = clearance_reserves_pm_request_balance_sql("p")
	lock_sql = " for update" if for_update else ""
	rows = frappe.db.sql(
		f"""
		select distinct p.name
		from `tabPM Clearance` p
		inner join `tabPM Clearance Detail` d
			on d.parent = p.name and d.parenttype = 'PM Clearance'
		where d.purchase_invoice = %s
			and ifnull(d.settlement_type, %s) = %s
			and ({active})
		order by p.name
		{lock_sql}
		""",
		(pi, SETTLEMENT_PI, SETTLEMENT_PI),
		as_dict=True,
	)
	return [r.name for r in rows if r.name]


def _format_cancel_block_message(pi_name: str, clearance_names: list[str]) -> str:
	# Permission-aware listing: only include Clearances the user can read.
	visible: list[str] = []
	for name in clearance_names:
		if frappe.has_permission("PM Clearance", "read", doc=name):
			visible.append(name)
	if not visible:
		# Still block; do not leak unauthorized names.
		return _(
			"Cannot cancel Purchase Invoice {0}. It is referenced by one or more "
			"active PM Clearances you cannot access. Remove or replace the Purchase "
			"Invoice reference in those Clearances before cancelling."
		).format(pi_name)
	listed = ", ".join(visible)
	if len(visible) == 1:
		return _(
			"Cannot cancel Purchase Invoice {0}.\n\n"
			"It is referenced by active PM Clearance:\n{1}\n\n"
			"Remove or replace the Purchase Invoice in the PM Clearance before cancelling it."
		).format(pi_name, listed)
	return _(
		"Cannot cancel Purchase Invoice {0}.\n\n"
		"It is referenced by active PM Clearances:\n{1}\n\n"
		"Remove or replace the Purchase Invoice in those Clearances before cancelling it."
	).format(pi_name, listed)


def before_cancel_purchase_invoice(doc: Document, method: str | None = None) -> None:
	"""Server-side guard: block PI cancel while any active PM Clearance references it.

	Named lock + row FOR UPDATE are held until the cancel transaction commits so a
	concurrent Clearance insert cannot sneak in after the emptiness check.
	"""
	pi_name = (getattr(doc, "name", None) or "").strip()
	if not pi_name:
		return
	# Serialize cancel vs Clearance link/save on the same PI (sorted single key).
	_acquire_pi_named_locks([pi_name])
	# Row lock reinforces InnoDB ordering with Clearance validate path.
	frappe.db.sql(
		"select name, docstatus from `tabPurchase Invoice` where name = %s for update",
		(pi_name,),
	)
	# Locking read — must see Clearances committed after this transaction began.
	active = find_active_clearances_referencing_pi(pi_name, for_update=True)
	if active:
		frappe.throw(
			_format_cancel_block_message(pi_name, active),
			title=_("Purchase Invoice Linked to PM Clearance"),
		)


def assert_linked_purchase_invoices_cancellable_state(doc: Document) -> None:
	"""During Clearance validate: lock PIs and refuse cancelled links (except Return).

	Corrective Return (``pm_return_for_correction``) skips — legacy cancelled links
	must remain so the document can reach Draft for correction.
	"""
	if getattr(frappe.flags, "pm_return_for_correction", False):
		return
	pi_names = _iter_pi_names_from_clearance(doc)
	if not pi_names:
		return
	_acquire_pi_named_locks(pi_names)
	# Deterministic lock order: sorted PI names.
	for pi in sorted(pi_names):
		rows = frappe.db.sql(
			"select name, docstatus from `tabPurchase Invoice` where name = %s for update",
			(pi,),
			as_dict=True,
		)
		if not rows:
			frappe.throw(_("Purchase Invoice {0} was not found.").format(pi))
		if cint(rows[0].docstatus) == 2:
			frappe.throw(
				_("Purchase Invoice {0} is cancelled and cannot be referenced by PM Clearance.").format(pi),
				title=_("Invalid Purchase Invoice"),
			)


def report_legacy_cancelled_pi_on_clearances() -> list[dict]:
	"""Diagnostic only — list Clearances that still reference cancelled PIs.

	Does not mutate documents. Recommended action is always corrective Return
	(if Pending*) then remove/replace the cancelled PI in Draft.
	"""
	active = clearance_reserves_pm_request_balance_sql("p")
	rows = frappe.db.sql(
		f"""
		select
			p.name as pm_clearance,
			p.workflow_state,
			p.status as clearance_status,
			p.docstatus as clearance_docstatus,
			d.purchase_invoice,
			d.allocated_amount,
			d.idx as row_idx,
			pi.docstatus as pi_docstatus,
			pi.status as pi_status,
			pi.grand_total,
			pi.outstanding_amount
		from `tabPM Clearance` p
		inner join `tabPM Clearance Detail` d
			on d.parent = p.name and d.parenttype = 'PM Clearance'
		inner join `tabPurchase Invoice` pi
			on pi.name = d.purchase_invoice
		where ifnull(d.settlement_type, %s) = %s
			and pi.docstatus = 2
			and ({active})
		order by p.name, d.idx
		""",
		(SETTLEMENT_PI, SETTLEMENT_PI),
		as_dict=True,
	)
	out: list[dict] = []
	for r in rows:
		ws = r.workflow_state
		ws_title = (
			frappe.db.get_value("Workflow State", ws, "workflow_state_name") or ws or ""
		).strip()
		action = (
			"PM Return for Correction (if Pending*), then remove or replace cancelled PI in Draft"
			if cint(r.clearance_docstatus) == 0
			else "Do not mutate financial history automatically; review manually"
		)
		out.append(
			{
				"pm_clearance": r.pm_clearance,
				"workflow_state": ws_title,
				"status": r.clearance_status,
				"clearance_docstatus": cint(r.clearance_docstatus),
				"purchase_invoice": r.purchase_invoice,
				"pi_docstatus": cint(r.pi_docstatus),
				"pi_status": r.pi_status,
				"allocated_amount": flt(r.allocated_amount),
				"row_idx": cint(r.row_idx),
				"recommended_action": action,
			}
		)
	return out

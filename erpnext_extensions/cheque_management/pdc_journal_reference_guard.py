# Copyright (c) 2026, ERPNext Extensions contributors
"""Protect PDC Journal References as exclusively system-managed rows.

Manual Desk/API create, edit, and delete must be rejected. Trusted create/removal
is allowed only while a scoped server-side ``frappe.flags`` authorization is active
(PDC lifecycle JE posting, workflow rollback JE detach, Facility Debt Purchase
settlement append/remove).
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

import frappe
from frappe import _

PROTECTED_JR_FIELDS: tuple[str, ...] = (
	"journal_entry",
	"purpose",
	"pdc_transition_key",
	"posting_date",
	"amount",
)

_AUTH_FLAG = "pdc_jr_authorized_mutation"

_MSG_MUTATE = _(
	"Journal References are system-managed. "
	"Manual deletion or modification is not permitted. "
	"Use Rollback Workflow State to reverse the related accounting transition."
)

_MSG_CREATE = _(
	"Journal References are system-managed. "
	"Manual creation is not permitted. "
	"References are created only by PDC accounting transitions and authorized Facility workflows."
)


def _auth_payload() -> dict[str, Any] | None:
	payload = getattr(frappe.flags, _AUTH_FLAG, None)
	return payload if isinstance(payload, dict) else None


def _payload_matches_pdc(auth: dict[str, Any], pdc_name: str) -> bool:
	return str(auth.get("pdc") or "").strip() == pdc_name


def journal_reference_creation_authorized(
	*,
	pdc_name: str,
	transition_key: str | None = None,
) -> bool:
	"""Return True when trusted server code authorized Journal Reference creation."""
	pdc_name = (pdc_name or "").strip()
	if not pdc_name:
		return False
	auth = _auth_payload()
	if not auth or not _payload_matches_pdc(auth, pdc_name):
		return False
	if not auth.get("allow_create"):
		return False
	allowed_keys = {str(x) for x in (auth.get("transition_keys") or []) if x}
	if allowed_keys:
		key = (transition_key or "").strip()
		if not key or key not in allowed_keys:
			return False
	return True


def journal_reference_removal_authorized(
	*,
	pdc_name: str,
	row_name: str | None = None,
	transition_key: str | None = None,
) -> bool:
	"""Return True when trusted server code authorized this PDC/row removal."""
	pdc_name = (pdc_name or "").strip()
	if not pdc_name:
		return False

	auth = _auth_payload()
	if auth and _payload_matches_pdc(auth, pdc_name) and auth.get("allow_remove"):
		allowed_rows = {str(x) for x in (auth.get("rows") or []) if x}
		allowed_keys = {str(x) for x in (auth.get("transition_keys") or []) if x}

		if row_name and allowed_rows and row_name not in allowed_rows:
			return False
		if transition_key and allowed_keys and transition_key not in allowed_keys:
			return False
		if (row_name or transition_key) and not allowed_rows and not allowed_keys:
			if not auth.get("allow_any_row_on_pdc"):
				return False
		return True

	# Narrow legacy path: instrument restore may hold in_pdc_workflow_rollback without
	# an explicit JR removal payload. Only when no concrete row/key is being checked.
	rb = getattr(frappe.flags, "in_pdc_workflow_rollback", None)
	if rb and str(rb) == pdc_name and not row_name and not transition_key:
		return True
	return False


@contextmanager
def authorize_journal_reference_creation(
	pdc_name: str,
	*,
	transition_keys: list[str] | None = None,
) -> Iterator[None]:
	"""Scoped authorization for trusted Journal Reference creation."""
	pdc_name = (pdc_name or "").strip()
	if not pdc_name:
		raise frappe.ValidationError(_("PDC name is required to authorize Journal Reference creation."))

	previous = getattr(frappe.flags, _AUTH_FLAG, None)
	frappe.flags.pdc_jr_authorized_mutation = {
		"pdc": pdc_name,
		"allow_create": True,
		"allow_remove": False,
		"rows": [],
		"transition_keys": [str(k) for k in (transition_keys or []) if k],
		"allow_any_row_on_pdc": False,
	}
	try:
		yield
	finally:
		frappe.flags.pdc_jr_authorized_mutation = previous


@contextmanager
def authorize_journal_reference_removal(
	pdc_name: str,
	*,
	rows: list[str] | None = None,
	transition_keys: list[str] | None = None,
	allow_any_row_on_pdc: bool = False,
) -> Iterator[None]:
	"""Scoped authorization for trusted Journal Reference removal.

	Not readable or settable from client API payloads — lives only on ``frappe.flags``.
	"""
	pdc_name = (pdc_name or "").strip()
	if not pdc_name:
		raise frappe.ValidationError(_("PDC name is required to authorize Journal Reference removal."))

	previous = getattr(frappe.flags, _AUTH_FLAG, None)
	frappe.flags.pdc_jr_authorized_mutation = {
		"pdc": pdc_name,
		"allow_create": False,
		"allow_remove": True,
		"rows": [str(r) for r in (rows or []) if r],
		"transition_keys": [str(k) for k in (transition_keys or []) if k],
		"allow_any_row_on_pdc": bool(allow_any_row_on_pdc),
	}
	try:
		yield
	finally:
		frappe.flags.pdc_jr_authorized_mutation = previous


def _row_is_new_journal_reference(row, existing_names: set[str]) -> bool:
	name = (getattr(row, "name", None) or "").strip()
	if not name:
		return True
	if getattr(row, "get", None) and row.get("__islocal"):
		return True
	if getattr(row, "__islocal", None):
		return True
	return name not in existing_names


def validate_journal_references_immutable(doc) -> None:
	"""Reject unauthorized Journal Reference create/delete/edit on PDC save."""
	pdc_name = doc.name
	if not pdc_name and not doc.is_new():
		return

	prev_rows: list[Any] = []
	if not doc.is_new() and pdc_name:
		prev_rows = frappe.get_all(
			"PDC Journal Reference",
			filters={"parent": pdc_name, "parenttype": "Post Dated Cheque"},
			fields=["name", *PROTECTED_JR_FIELDS],
		)
	prev_by_name = {r.name: r for r in prev_rows}
	existing_names = set(prev_by_name)

	current_all = list(doc.get("journal_references") or [])

	# --- create ---
	for row in current_all:
		if not _row_is_new_journal_reference(row, existing_names):
			continue
		parent_for_auth = pdc_name or (getattr(row, "parent", None) or "")
		tkey = (row.get("pdc_transition_key") if hasattr(row, "get") else getattr(row, "pdc_transition_key", None)) or None
		if not journal_reference_creation_authorized(
			pdc_name=str(parent_for_auth or ""),
			transition_key=(str(tkey).strip() or None) if tkey else None,
		):
			frappe.throw(_MSG_CREATE, title=_("Journal References Protected"))

	if doc.is_new():
		return

	# --- delete ---
	current_named = [r for r in current_all if (getattr(r, "name", None) or "").strip()]
	current_names = {r.name for r in current_named if r.name in existing_names}
	removed = existing_names - current_names
	for row_name in removed:
		old = prev_by_name[row_name]
		if not journal_reference_removal_authorized(
			pdc_name=pdc_name,
			row_name=row_name,
			transition_key=(old.pdc_transition_key or None),
		):
			frappe.throw(_MSG_MUTATE, title=_("Journal References Protected"))

	# --- edit protected fields ---
	for row in current_named:
		old = prev_by_name.get(row.name)
		if not old:
			continue
		for field in PROTECTED_JR_FIELDS:
			new_val = row.get(field) if hasattr(row, "get") else getattr(row, field, None)
			old_val = old.get(field) if hasattr(old, "get") else getattr(old, field, None)
			if field == "amount":
				from frappe.utils import flt

				if flt(new_val) == flt(old_val):
					continue
			elif str(new_val or "") == str(old_val or ""):
				continue
			frappe.throw(_MSG_MUTATE, title=_("Journal References Protected"))


def reject_unauthorized_journal_reference_trash(row) -> None:
	"""Child-table ``on_trash`` guard for direct delete_doc / API child deletes."""
	parent = (getattr(row, "parent", None) or "").strip()
	name = (getattr(row, "name", None) or "").strip()
	key = (getattr(row, "pdc_transition_key", None) or "").strip() or None
	if journal_reference_removal_authorized(pdc_name=parent, row_name=name, transition_key=key):
		return
	frappe.throw(_MSG_MUTATE, title=_("Journal References Protected"))


def reject_unauthorized_journal_reference_insert(row) -> None:
	"""Child-table ``before_insert`` / new-row guard."""
	parent = (getattr(row, "parent", None) or "").strip()
	key = (getattr(row, "pdc_transition_key", None) or "").strip() or None
	if journal_reference_creation_authorized(pdc_name=parent, transition_key=key):
		return
	frappe.throw(_MSG_CREATE, title=_("Journal References Protected"))


def reject_unauthorized_journal_reference_field_edit(row) -> None:
	"""Child-table ``validate`` guard against protected-field mutation."""
	if row.is_new():
		# Creation is enforced in before_insert / parent validate; avoid double-throw wording.
		reject_unauthorized_journal_reference_insert(row)
		return
	old = frappe.db.get_value(
		"PDC Journal Reference",
		row.name,
		PROTECTED_JR_FIELDS,
		as_dict=True,
	)
	if not old:
		return
	for field in PROTECTED_JR_FIELDS:
		new_val = row.get(field)
		old_val = old.get(field)
		if field == "amount":
			from frappe.utils import flt

			if flt(new_val) == flt(old_val):
				continue
		elif str(new_val or "") == str(old_val or ""):
			continue
		frappe.throw(_MSG_MUTATE, title=_("Journal References Protected"))


__all__ = [
	"PROTECTED_JR_FIELDS",
	"authorize_journal_reference_creation",
	"authorize_journal_reference_removal",
	"journal_reference_creation_authorized",
	"journal_reference_removal_authorized",
	"reject_unauthorized_journal_reference_field_edit",
	"reject_unauthorized_journal_reference_insert",
	"reject_unauthorized_journal_reference_trash",
	"validate_journal_references_immutable",
]

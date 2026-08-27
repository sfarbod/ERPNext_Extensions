"""Petty Management workflow — canonical Workflow State names and safe transitions."""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.model.workflow import get_transitions


def resolve_workflow_state_link(title_or_name: str | None) -> str | None:
	"""Return canonical ``Workflow State.name`` for workflow rows and link fields."""
	if not title_or_name:
		return title_or_name
	s = (title_or_name or "").strip()
	if frappe.db.exists("Workflow State", s):
		return s
	row = frappe.db.sql(
		"""
		SELECT name FROM `tabWorkflow State`
		WHERE LOWER(name) = LOWER(%s) OR LOWER(workflow_state_name) = LOWER(%s)
		LIMIT 1
		""",
		(s, s),
	)
	if row:
		return row[0][0]
	doc = frappe.new_doc("Workflow State")
	doc.workflow_state_name = s
	doc.insert(ignore_permissions=True)
	return doc.name


def normalize_workflow_definition(workflow_doc) -> bool:
	"""Align Workflow states/transitions with canonical Workflow State document names."""
	changed = False
	legacy_map = {
		"pending approval": "Pending Approval",
	}
	for row in workflow_doc.states:
		raw = (row.state or "").strip()
		canonical = legacy_map.get(raw.lower()) or resolve_workflow_state_link(raw)
		if canonical and canonical != row.state:
			row.state = canonical
			changed = True
	for row in workflow_doc.transitions:
		for field in ("state", "next_state"):
			raw = (getattr(row, field, None) or "").strip()
			canonical = legacy_map.get(raw.lower()) or resolve_workflow_state_link(raw)
			if canonical and canonical != getattr(row, field):
				setattr(row, field, canonical)
				changed = True
	return changed


def realign_doctype_workflow_states(doctype: str, *, workflow_state_field: str = "workflow_state") -> int:
	"""Fix stored link values that differ only by casing/spelling from canonical names."""
	if not frappe.db.has_column(doctype, workflow_state_field):
		return 0
	updated = 0
	for row in frappe.get_all(doctype, fields=["name", workflow_state_field], limit=0):
		raw = (row.get(workflow_state_field) or "").strip()
		if not raw:
			continue
		canonical = resolve_workflow_state_link(raw)
		if canonical and canonical != raw:
			frappe.db.set_value(doctype, row.name, workflow_state_field, canonical, update_modified=False)
			updated += 1
	return updated


def get_allowed_workflow_actions(doc: Document) -> list[dict]:
	"""Server mirror of Desk workflow action menu (action labels only)."""
	if doc.is_new():
		return []
	try:
		return get_transitions(doc)
	except Exception:
		return []


def apply_pm_workflow(doc: Document | str, action: str) -> Document:
	"""Apply workflow by **action label** (never by target state name).

	Routes through ``workflow_hooks.apply_workflow`` so Desk and tests share
	v4.7.2 stamp / Return-for-Correction / auto-skip behaviour.
	"""
	if isinstance(doc, str):
		doctype, name = doc.split(",", 1) if "," in doc else (None, doc)
		if not doctype:
			raise frappe.ValidationError(_("Pass doctype,name or a Document"))
		doc = frappe.get_doc(doctype, name)
	action = (action or "").strip()
	if not action:
		frappe.throw(_("Workflow action is required"))
	allowed = {t.get("action") for t in get_allowed_workflow_actions(doc)}
	if action not in allowed:
		frappe.throw(
			_("Workflow action {0} is not allowed from the current state ({1}).").format(
				action, doc.get("workflow_state") or _("Draft")
			),
			title=_("Workflow"),
		)
	from erpnext_extensions.petty_management.workflow_hooks import apply_workflow as hooked_apply

	result = hooked_apply(doc.as_dict(), action)
	if isinstance(result, dict):
		return frappe.get_doc(result)
	if hasattr(result, "reload"):
		result.reload()
	return result


def workflow_action_table(doctype: str) -> list[dict]:
	"""Build state → actions matrix from active workflow definition."""
	wf_name = frappe.db.get_value("Workflow", {"document_type": doctype, "is_active": 1}, "name")
	if not wf_name:
		return []
	wf = frappe.get_doc("Workflow", wf_name)
	by_state: dict[str, list[dict]] = {}
	for t in wf.transitions:
		by_state.setdefault(t.state, []).append(
			{
				"action": t.action,
				"next_state": resolve_workflow_state_link(t.next_state),
				"allowed_role": t.allowed,
			}
		)
	rows = []
	for s in wf.states:
		rows.append(
			{
				"state": resolve_workflow_state_link(s.state),
				"doc_status": s.doc_status,
				"actions": by_state.get(s.state, []),
			}
		)
	return rows

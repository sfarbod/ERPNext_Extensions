"""Idempotent Cheque Management links under Payments / Accounts navigation.

Frappe ≤16.36 used ``Workspace Sidebar`` (module Payments). Frappe 16.52+ serves
runtime navigation from ``Sidebar`` (ERPNext ships ``Accounts``) plus optional
site ``Custom Sidebar`` layers. Extension code must **not** mutate ERPNext's
standard ``Sidebar Accounts`` document (that exports into the erpnext app when
developer_mode is on). Cheque / Guarantee links are applied via:

1. Legacy ``Workspace Sidebar`` archive when still writable (migrate-safe), and/or
2. Site-wide ``Custom Sidebar`` additions on module ``Accounts`` (16.52+).
"""

from __future__ import annotations

import frappe

from erpnext_extensions.desk_navigation import has_modern_sidebar, workspace_sidebar_writable


def _get_legacy_payments_sidebar():
	if not frappe.db.exists("DocType", "Workspace Sidebar"):
		return None
	name = frappe.db.get_value("Workspace Sidebar", {"module": "Payments", "for_user": None}, "name")
	if name:
		return frappe.get_doc("Workspace Sidebar", name)
	if frappe.db.exists("Workspace Sidebar", "Payments"):
		return frappe.get_doc("Workspace Sidebar", "Payments")
	return None


def _is_section_break(it) -> bool:
	return (it.get("type") or "") == "Section Break"


def _is_link(it) -> bool:
	return (it.get("type") or "") == "Link"


def _link_key(it) -> tuple[str, str]:
	return ((it.get("link_type") or "") or "").strip(), ((it.get("link_to") or "") or "").strip()


def _remove_existing_links(sidebar, targets: set[tuple[str, str]]) -> bool:
	items = list(sidebar.get("items") or [])
	new_items = []
	changed = False
	for it in items:
		if _is_link(it) and _link_key(it) in targets:
			changed = True
			continue
		new_items.append(it)
	if changed:
		sidebar.set("items", new_items)
	return changed


def _remove_section_breaks(sidebar, labels: set[str]) -> bool:
	items = list(sidebar.get("items") or [])
	new_items = []
	changed = False
	for it in items:
		if _is_section_break(it) and (it.get("label") or "") in labels:
			changed = True
			continue
		new_items.append(it)
	if changed:
		sidebar.set("items", new_items)
	return changed


def _insert_block_before_first_section(sidebar, before_label: str, block_items: list[dict]) -> bool:
	items = list(sidebar.get("items") or [])
	idx = None
	for i, it in enumerate(items):
		if _is_section_break(it) and (it.get("label") or "") == before_label:
			idx = i
			break
	if idx is None:
		for bi in block_items:
			sidebar.append("items", bi)
		return True
	sidebar.set("items", items[:idx] + block_items + items[idx:])
	return True


def _native_section_break(*, label: str, icon: str, keep_closed: int = 0) -> dict:
	return {
		"label": label,
		"type": "Section Break",
		"link_type": "DocType",
		"link_to": None,
		"child": 0,
		"indent": 1,
		"collapsible": 1,
		"keep_closed": keep_closed,
		"icon": icon,
		"show_arrow": 0,
	}


def _native_child_link(*, link_type: str, link_to: str, label: str | None = None) -> dict:
	return {
		"label": label or link_to,
		"type": "Link",
		"link_type": link_type,
		"link_to": link_to,
		"child": 1,
		"collapsible": 1,
		"indent": 0,
		"keep_closed": 0,
		"show_arrow": 0,
	}


def _reindex_items(sidebar) -> None:
	for i, it in enumerate(sidebar.get("items") or []):
		it.idx = i + 1


def _cheque_block_items() -> list[dict]:
	target_links: list[tuple[str, str, str]] = [
		("DocType", "Post Dated Cheque", "Post Dated Cheque"),
		("DocType", "Cheque Book", "Cheque Book"),
		("DocType", "Cheque Leaf", "Cheque Leaf"),
		("DocType", "Cheque Opening Import", "Cheque Opening Import"),
		("Report", "Cheque Maturity Report", "Cheque Maturity Report"),
		("DocType", "PDC Settings", "PDC Settings"),
	]
	block: list[dict] = []
	op_links: list[dict] = []
	for lt, to, lbl in target_links[:4]:
		if lt == "DocType" and frappe.db.exists("DocType", to):
			op_links.append(_native_child_link(link_type=lt, link_to=to, label=lbl))
	if op_links:
		block.append(_native_section_break(label="Cheque Operations", icon="banknote", keep_closed=0))
		block.extend(op_links)
	if frappe.db.exists("Report", "Cheque Maturity Report"):
		block.append(_native_section_break(label="Cheque Reports", icon="sheet", keep_closed=1))
		block.append(
			_native_child_link(
				link_type="Report",
				link_to="Cheque Maturity Report",
				label="Cheque Maturity Report",
			)
		)
	if frappe.db.exists("DocType", "PDC Settings"):
		block.append(_native_section_break(label="Cheque Configuration", icon="settings", keep_closed=0))
		block.append(_native_child_link(link_type="DocType", link_to="PDC Settings", label="PDC Settings"))
	return block


def _guarantee_block_items() -> list[dict]:
	if not frappe.db.exists("DocType", "Guarantee Document"):
		return []
	return [
		_native_section_break(label="Guarantee Documents", icon="shield-check", keep_closed=0),
		_native_child_link(link_type="DocType", link_to="Guarantee Document", label="Guarantee Document"),
	]


def _apply_block_to_legacy_sidebar(sidebar, block: list[dict], *, section_labels: set[str], target_set: set) -> bool:
	if not sidebar or not block:
		return False
	if not workspace_sidebar_writable():
		return False
	changed = False
	changed |= bool(_remove_existing_links(sidebar, target_set))
	changed |= bool(_remove_section_breaks(sidebar, section_labels))
	changed |= bool(_insert_block_before_first_section(sidebar, "Reports", block))
	if changed:
		_reindex_items(sidebar)
		sidebar.save(ignore_permissions=True)
		frappe.db.commit()
	return changed


def _ensure_site_custom_sidebar_items(module: str, items: list[dict]) -> bool:
	"""Append missing rows to the site-wide Custom Sidebar (never touch base Sidebar)."""
	if not has_modern_sidebar() or not items:
		return False
	from frappe.desk.doctype.custom_sidebar.custom_sidebar import add_site_sidebar_item

	for item in items:
		# Idempotent: skips when the linked identity is already present.
		add_site_sidebar_item(module, dict(item))
	return True


def apply_payments_cheque_sidebar_groups() -> bool:
	"""Ensure Cheque Operations / Reports / Configuration groups exist on Payments navigation."""
	block = _cheque_block_items()
	if not block:
		return False

	target_set = {
		("DocType", "Post Dated Cheque"),
		("DocType", "Cheque Book"),
		("DocType", "Cheque Leaf"),
		("DocType", "Cheque Opening Import"),
		("Report", "Cheque Maturity Report"),
		("DocType", "PDC Settings"),
	}
	section_labels = {"Cheque Operations", "Cheque Reports", "Cheque Configuration"}

	changed = False
	legacy = _get_legacy_payments_sidebar()
	if legacy:
		changed |= _apply_block_to_legacy_sidebar(
			legacy, block, section_labels=section_labels, target_set=target_set
		)
	elif not has_modern_sidebar():
		frappe.logger("erpnext_extensions.cheque_management").warning(
			"No public Workspace Sidebar for module Payments; skipping Cheque Management sidebar grouping "
			"(ensure ERPNext Payments sidebar is installed/synced)."
		)

	# Frappe 16.52+: site Custom Sidebar on Accounts (ERPNext base module).
	changed |= _ensure_site_custom_sidebar_items("Accounts", block)
	return changed


def apply_payments_guarantee_document_sidebar() -> bool:
	"""Idempotent Guarantee Documents section + list link on Payments / Accounts navigation."""
	block = _guarantee_block_items()
	if not block:
		return False

	target_set = {("DocType", "Guarantee Document")}
	section_labels = {"Guarantee Documents"}

	changed = False
	legacy = _get_legacy_payments_sidebar()
	if legacy:
		changed |= _apply_block_to_legacy_sidebar(
			legacy, block, section_labels=section_labels, target_set=target_set
		)
	elif not has_modern_sidebar():
		frappe.logger("erpnext_extensions.cheque_management").warning(
			"No public Workspace Sidebar for module Payments; skipping Guarantee Document sidebar link."
		)

	changed |= _ensure_site_custom_sidebar_items("Accounts", block)
	return changed


def after_migrate() -> None:
	"""Frappe hook: re-apply sidebar grouping after migrate (post orphan Workspace cleanup)."""
	try:
		apply_payments_cheque_sidebar_groups()
	except Exception:
		frappe.log_error(
			title="Cheque Management: after_migrate sidebar sync failed",
			message=frappe.get_traceback(),
		)
	try:
		apply_payments_guarantee_document_sidebar()
	except Exception:
		frappe.log_error(
			title="Guarantee Management: after_migrate sidebar sync failed",
			message=frappe.get_traceback(),
		)

# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

"""v5.5.8: remove known-invalid custom Asset → Asset Movement DocType Link.

Site customization (example name ``m2j1c70lrr``) added a second Connections
group labeled "Asset Movement" with ``link_fieldname=asset_name``. That
duplicated ERPNext's native Movement → Asset Movement connection and overwrote
the correct ``asset`` filter so Connections counts showed 0.

Custom DocType Links created via Customize Form are stored with
``parenttype='Customize Form'`` (not ``DocType``). Matching accepts either
parenttype while requiring ``parent='Asset'``.

Idempotent. Narrow match only — does not touch Equipment Profile or other
custom Asset links. Safe when the link is absent or the site never had it.
"""

from __future__ import annotations

import frappe
from frappe.utils import cint


BAD_LINK_DOCTYPE = "Asset Movement"
BAD_LINK_GROUP = "Asset Movement"
BAD_LINK_FIELDNAME = "asset_name"
ALLOWED_PARENTTYPES = ("DocType", "Customize Form")


def execute():
	if not frappe.db.exists("DocType", "Asset"):
		return
	if not frappe.db.table_exists("DocType Link"):
		return

	removed = remove_matching_bad_links()
	if removed:
		frappe.clear_cache(doctype="Asset")
		frappe.logger("erpnext_extensions").info(
			"v5.5.8: removed invalid custom Asset Movement DocType Link(s): %s",
			", ".join(removed),
		)
	else:
		frappe.logger("erpnext_extensions").info(
			"v5.5.8: no invalid custom Asset Movement DocType Link found (noop)"
		)


def _matches_bad_link(link) -> bool:
	"""Exact semantic identity of the proven-bad customization."""
	custom = link.get("custom") if hasattr(link, "get") else getattr(link, "custom", 0)
	if not cint(custom):
		return False
	parent = link.get("parent") if hasattr(link, "get") else getattr(link, "parent", None)
	parenttype = (
		link.get("parenttype") if hasattr(link, "get") else getattr(link, "parenttype", None)
	)
	link_doctype = (
		link.get("link_doctype") if hasattr(link, "get") else getattr(link, "link_doctype", None)
	)
	group = link.get("group") if hasattr(link, "get") else getattr(link, "group", None)
	fieldname = (
		link.get("link_fieldname")
		if hasattr(link, "get")
		else getattr(link, "link_fieldname", None)
	)
	return (
		(parent or "") == "Asset"
		and (parenttype or "") in ALLOWED_PARENTTYPES
		and (link_doctype or "") == BAD_LINK_DOCTYPE
		and (group or "") == BAD_LINK_GROUP
		and (fieldname or "") == BAD_LINK_FIELDNAME
	)


def remove_matching_bad_links() -> list[str]:
	"""Delete DocType Link rows that match the known-invalid signature.

	Returns deleted names. Callable from tests for isolation.
	"""
	rows = frappe.get_all(
		"DocType Link",
		filters={
			"parent": "Asset",
			"custom": 1,
			"link_doctype": BAD_LINK_DOCTYPE,
			"group": BAD_LINK_GROUP,
			"link_fieldname": BAD_LINK_FIELDNAME,
		},
		fields=["name", "custom", "link_doctype", "group", "link_fieldname", "parent", "parenttype"],
	)
	removed: list[str] = []
	for row in rows:
		if not _matches_bad_link(row):
			continue
		frappe.db.delete("DocType Link", {"name": row.name})
		removed.append(row.name)
	return removed

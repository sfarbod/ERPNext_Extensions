# Copyright (c) 2026, ERPNext Extensions contributors
"""QA-only helpers for PM Request cancel / clearance tests (not production)."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

import frappe

PM_CLEARANCE_PI_REMARKS = "PM cancel/clearance test fixture"


@contextmanager
def temporary_buying_settings(**overrides: Any) -> Iterator[frappe.model.document.Document]:
	"""Apply Buying Settings for a test scope and restore previous values."""
	frappe.set_user("Administrator")
	doc = frappe.get_single("Buying Settings")
	original = {field: doc.get(field) for field in overrides}
	try:
		for field, value in overrides.items():
			doc.set(field, value)
		doc.save(ignore_permissions=True)
		frappe.db.commit()
		yield doc
	finally:
		doc = frappe.get_single("Buying Settings")
		for field, value in original.items():
			doc.set(field, value)
		doc.save(ignore_permissions=True)
		frappe.db.commit()


@contextmanager
def pm_clearance_pi_site() -> Iterator[None]:
	"""Site settings required to insert/submit Purchase Invoice clearance fixtures."""
	with temporary_buying_settings(po_required="No"):
		yield


def get_doctype_json_role_perm(doctype: str, role: str):
	"""DocType JSON Role Permissions (excludes site Custom DocPerm overrides)."""
	dt = frappe.get_doc("DocType", doctype)
	row = next((perm for perm in dt.permissions if perm.role == role), None)
	if not row:
		raise AssertionError(f"No DocType JSON permission row for role {role!r} on {doctype!r}")
	return row


def prepare_pi_for_clearance_fixture(pi) -> None:
	"""Insert + submit a draft PI built by test_pm_clearance helpers."""
	if not (pi.get("remarks") or "").strip():
		pi.remarks = PM_CLEARANCE_PI_REMARKS
	with pm_clearance_pi_site():
		pi.insert()
		pi.submit()

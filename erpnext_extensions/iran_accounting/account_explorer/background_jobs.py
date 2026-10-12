# Copyright (c) 2026, Farbod Siyahpoosh and contributors
"""Background workers for Account Explorer prepared results."""

from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import cint

from erpnext_extensions.iran_accounting.account_explorer.cache_revision import get_accounting_revision
from erpnext_extensions.iran_accounting.account_explorer.prepared_report import (
	PREPARED_DOCTYPE,
	RESULT_SCHEMA_VERSION,
	build_materialized_result,
	store_artifact,
)
from erpnext_extensions.iran_accounting.account_explorer.query_fingerprint import build_fingerprint
from erpnext_extensions.iran_accounting.account_explorer.query_spec import (
	AccountExplorerQuerySpec_from_client,
)


def workers_available() -> bool:
	try:
		from rq import Worker

		from frappe.utils.background_jobs import get_redis_conn

		return bool(Worker.all(connection=get_redis_conn()))
	except Exception:
		return False


def build_account_explorer_prepared_result(prepared_result_name: str = None, payload: Any = None, job_name: str = None) -> None:
	"""Worker entry: materialize Account Explorer result and attach gzip JSON.

	`prepared_result_name` is the Account Explorer Prepared Result docname.
	`job_name` is accepted as a deprecated alias (must not be passed via frappe.enqueue's
	reserved `job_name=` kwarg — that is the RQ label only).
	"""
	docname = prepared_result_name or job_name
	if not docname:
		frappe.throw("prepared_result_name is required")
	doc = frappe.get_doc(PREPARED_DOCTYPE, docname)
	doc.status = "Started"
	doc.job_id = frappe.local.job.id if getattr(frappe.local, "job", None) else doc.job_id
	doc.flags.ignore_permissions = True
	doc.save(ignore_permissions=True)
	frappe.db.commit()

	try:
		spec = AccountExplorerQuerySpec_from_client(payload, require_dates=True)
		revision = get_accounting_revision(spec.company)
		if cint(doc.accounting_revision) != revision:
			doc.status = "Stale"
			doc.error_message = "Accounting revision changed before build completed."
			doc.save(ignore_permissions=True)
			frappe.db.commit()
			return

		materialized = build_materialized_result(spec)
		fingerprint = build_fingerprint(spec, revision)
		artifact = {
			"schema_version": RESULT_SCHEMA_VERSION,
			"fingerprint": fingerprint,
			"execution_id": getattr(doc, "execution_id", None),
			"accounting_revision": revision,
			"view_axis": spec.view_axis,
			"company": spec.company,
			"rows": materialized.get("rows") or [],
			"warnings": materialized.get("warnings") or [],
			"extras": materialized.get("extras") or {},
		}
		# Re-check revision after expensive build.
		latest = get_accounting_revision(spec.company)
		if latest != revision:
			doc.status = "Stale"
			doc.error_message = "Accounting revision changed during build."
			doc.save(ignore_permissions=True)
			frappe.db.commit()
			return

		store_artifact(doc, artifact)
		frappe.db.commit()
	except Exception as exc:
		frappe.db.rollback()
		doc = frappe.get_doc(PREPARED_DOCTYPE, docname)
		doc.status = "Error"
		doc.error_message = frappe.get_traceback() if frappe.conf.developer_mode else str(exc)
		doc.flags.ignore_permissions = True
		doc.save(ignore_permissions=True)
		frappe.db.commit()
		raise

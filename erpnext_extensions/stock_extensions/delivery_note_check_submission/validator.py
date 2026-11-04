# Copyright (c) 2026, ERPNext Extensions contributors
"""Non-destructive Delivery Note submission validation.

Reuses ERPNext's actual submit lifecycle as far as it is safe:

1. Pre-submit: ``before_validate`` / ``validate`` / ``before_submit`` / ``_validate``
2. Submit-time checks from ``DeliveryNote.on_submit`` (packed qty, approving
   authority, credit limit, prevdoc status, serial/batch bundles, stock ledger,
   GL entries) — executed inside a guarded savepoint that is always rolled back.

Intentionally skipped (cannot safely dry-run without irreversible effects):
- ``repost_future_sle_and_gle`` (creates / enqueues Repost Item Valuation)
- ``update_pick_list_status`` (persists pick-list state; low validation value)
- ``make_return_invoice`` (creates Sales Invoice for return credit notes)
- Emails / webhooks / automations (muted via ``in_import`` + ``mute_emails``)
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from typing import Any, Iterator

import frappe
from frappe import _
from frappe.utils import cint, strip_html


SUCCESS_MESSAGE = _(
	"All submission validations passed. This Delivery Note is ready for submission."
)
SUCCESS_CAVEAT = _(
	"This result only confirms validation at the time of checking and does not "
	"guarantee that stock or other conditions will remain unchanged until actual submission."
)

SKIPPED_CHECKS = (
	"repost_future_sle_and_gle",
	"update_pick_list_status",
	"make_return_invoice",
)


@contextmanager
def _guarded_validation_savepoint(sp: str) -> Iterator[None]:
	"""Own the DB boundary for the dry-run so nested commit/rollback cannot escape.

	Nested ``frappe.db.rollback()`` (full) or ``frappe.db.commit()`` destroys
	MySQL/MariaDB SAVEPOINTs. Demote full rollback to our savepoint and block commit.
	"""
	frappe.db.savepoint(sp)
	orig_rollback = frappe.db.rollback
	orig_commit = frappe.db.commit
	frappe.db._disable_transaction_control = cint(getattr(frappe.db, "_disable_transaction_control", 0)) + 1

	def _guarded_commit(*_a, **_k):
		frappe.throw(
			_("Commit blocked during Delivery Note Check Submission dry-run."),
			frappe.ValidationError,
		)

	def _guarded_rollback(*, save_point=None, chain=False):
		if save_point:
			return orig_rollback(save_point=save_point, chain=chain)
		return orig_rollback(save_point=sp)

	frappe.db.commit = _guarded_commit  # type: ignore[method-assign]
	frappe.db.rollback = _guarded_rollback  # type: ignore[method-assign]
	try:
		yield
	finally:
		frappe.db.commit = orig_commit  # type: ignore[method-assign]
		frappe.db.rollback = orig_rollback  # type: ignore[method-assign]
		frappe.db._disable_transaction_control = max(
			0, cint(getattr(frappe.db, "_disable_transaction_control", 0)) - 1
		)
		try:
			orig_rollback(save_point=sp)
		except Exception:
			# Savepoint may already be gone if a nested demoted rollback ran.
			pass


def _normalize_error_message(exc: BaseException) -> str:
	msg = frappe.as_unicode(exc)
	if not msg or msg == type(exc).__name__:
		# Prefer last message_log entry when exception string is empty/generic.
		for entry in reversed(getattr(frappe.local, "message_log", None) or []):
			if isinstance(entry, dict) and entry.get("message"):
				msg = frappe.as_unicode(entry.get("message"))
				break
			if isinstance(entry, str) and entry:
				msg = entry
				break
	return (strip_html(msg) or type(exc).__name__).strip()


def _permission_ok(doc) -> None:
	"""Require Write (or Read+Write). Explicitly do NOT require Submit."""
	if not frappe.has_permission("Delivery Note", "write", doc=doc):
		frappe.throw(
			_("You need Write permission on Delivery Note to run Check Submission."),
			frappe.PermissionError,
		)


def _assert_draft_delivery_note(name: str):
	if not name:
		frappe.throw(_("Delivery Note name is required."), frappe.ValidationError)

	meta = frappe.db.get_value("Delivery Note", name, ["name", "docstatus"], as_dict=True)
	if not meta:
		frappe.throw(_("Delivery Note {0} does not exist.").format(name), frappe.DoesNotExistError)

	if cint(meta.docstatus) != 0:
		frappe.throw(
			_("Check Submission is only available for Draft Delivery Notes."),
			frappe.ValidationError,
		)

	doc = frappe.get_doc("Delivery Note", name)
	_permission_ok(doc)
	return doc


def _mute_side_effects() -> dict[str, Any]:
	"""Mute emails/webhooks/notifications for the dry-run window."""
	prior = {
		"in_import": getattr(frappe.flags, "in_import", False),
		"mute_emails": getattr(frappe.flags, "mute_emails", False),
		"in_patch": getattr(frappe.flags, "in_patch", False),
	}
	frappe.flags.in_import = True
	frappe.flags.mute_emails = True
	frappe.flags.in_patch = True
	return prior


def _restore_side_effects(prior: dict[str, Any]) -> None:
	for key, value in prior.items():
		setattr(frappe.flags, key, value)


def _run_presubmit_lifecycle(doc) -> None:
	"""Mirror Document.save submit-branch validations without persisting."""
	doc._action = "submit"
	# Keep in-memory docstatus as Draft for _validate / child consistency until
	# the submit-time phase explicitly flips it for stock/GL paths.
	doc.flags.ignore_permissions = False
	doc.flags.notifications_executed = []

	# StatusUpdater / stock paths expect the pre-save snapshot (as during save()).
	doc.load_doc_before_save()
	doc.set_parent_in_children()
	doc._validate_links()
	doc.run_method("before_validate")
	doc.run_method("validate")
	doc.run_method("before_submit")
	doc._validate()


def _force_submitted_in_memory(doc) -> None:
	"""Keep in-memory submitted state for SLE/GL builders.

	``update_billing_status`` / status helpers may reload the draft from DB and
	reset ``docstatus`` to 0. Stock ledger builders only emit outward SLEs when
	``docstatus == 1``, so we must re-assert submitted state before those paths.
	"""
	doc.docstatus = 1
	doc.set_docstatus()
	doc._action = "submit"


def _run_submit_time_validations(doc) -> None:
	"""Execute the validation-bearing portion of DeliveryNote.on_submit.

	Mutations are confined to the caller's savepoint and must be rolled back.
	"""
	_force_submitted_in_memory(doc)

	doc.validate_packed_qty()

	frappe.get_cached_doc("Authorization Control").validate_approving_authority(
		doc.doctype, doc.company, doc.base_grand_total, doc
	)

	# Reserved-qty accuracy depends on delivered qty on linked Sales Orders.
	doc.update_prevdoc_status()
	doc.update_billing_status()

	if not doc.is_return:
		doc.check_credit_limit()

	for table_name in ("items", "packed_items"):
		if not doc.get(table_name):
			continue
		doc.make_bundle_for_sales_purchase_return(table_name)
		doc.make_bundle_using_old_serial_batch_fields(table_name)

	doc.validate_standalone_serial_nos_customer()
	doc.update_stock_reservation_entries()

	# Re-assert after status/billing helpers which can reload draft docstatus.
	_force_submitted_in_memory(doc)
	doc.update_stock_ledger()
	doc.validate_produced_serial_nos_against_reservation()
	_force_submitted_in_memory(doc)
	doc.make_gl_entries()


def _snapshot_side_effects(name: str) -> dict[str, Any]:
	bin_rows = frappe.db.sql(
		"""
		select b.name, b.item_code, b.warehouse, b.actual_qty
		from `tabBin` b
		inner join `tabDelivery Note Item` dni
			on dni.item_code = b.item_code and dni.warehouse = b.warehouse
		where dni.parent = %s
		""",
		name,
		as_dict=True,
	)
	return {
		"docstatus": cint(frappe.db.get_value("Delivery Note", name, "docstatus")),
		"modified": frappe.db.get_value("Delivery Note", name, "modified"),
		"sle_count": frappe.db.count(
			"Stock Ledger Entry", {"voucher_type": "Delivery Note", "voucher_no": name}
		),
		"gle_count": frappe.db.count(
			"GL Entry", {"voucher_type": "Delivery Note", "voucher_no": name}
		),
		"bins": {row.name: flt_or_zero(row.actual_qty) for row in bin_rows},
	}


def flt_or_zero(value) -> float:
	from frappe.utils import flt

	return flt(value)


def validate_delivery_note_submission(name: str) -> dict[str, Any]:
	"""Run non-destructive submission validations for a Draft Delivery Note.

	Returns a structured result. Never leaves the Delivery Note submitted and
	never retains SLE/GLE/Bin mutations from the dry-run.
	"""
	doc = _assert_draft_delivery_note(name)
	before = _snapshot_side_effects(name)

	prior_messages = list(getattr(frappe.local, "message_log", None) or [])
	prior_flags = _mute_side_effects()
	sp = f"dn_check_sub_{uuid.uuid4().hex[:12]}"
	errors: list[dict[str, str]] = []
	ok = False

	try:
		with _guarded_validation_savepoint(sp):
			# Fresh load inside the savepoint so object mutations cannot leak.
			work = frappe.get_doc("Delivery Note", name)
			_run_presubmit_lifecycle(work)
			_run_submit_time_validations(work)
			ok = True
	except frappe.PermissionError:
		raise
	except Exception as exc:
		errors.append(
			{
				"exc_type": type(exc).__name__,
				"message": _normalize_error_message(exc),
			}
		)
		ok = False
	finally:
		_restore_side_effects(prior_flags)
		frappe.local.message_log = prior_messages

	after = _snapshot_side_effects(name)
	side_effects_clean = (
		after["docstatus"] == before["docstatus"] == 0
		and after["sle_count"] == before["sle_count"]
		and after["gle_count"] == before["gle_count"]
		and after["bins"] == before["bins"]
		and after["modified"] == before["modified"]
	)

	if not side_effects_clean:
		frappe.log_error(
			title="DN Check Submission side-effect leak",
			message=frappe.as_json({"name": name, "before": before, "after": after}),
		)
		frappe.throw(
			_(
				"Check Submission aborted because the dry-run could not guarantee a clean rollback. "
				"No submission was performed; please contact your system administrator."
			),
			frappe.ValidationError,
		)

	result: dict[str, Any] = {
		"ok": ok,
		"name": name,
		"docstatus": after["docstatus"],
		"errors": errors,
		"skipped_checks": list(SKIPPED_CHECKS),
		"side_effects_clean": side_effects_clean,
	}
	if ok:
		result["message"] = SUCCESS_MESSAGE
		result["caveat"] = SUCCESS_CAVEAT
	else:
		result["message"] = _("Submission validation failed.")
	return result

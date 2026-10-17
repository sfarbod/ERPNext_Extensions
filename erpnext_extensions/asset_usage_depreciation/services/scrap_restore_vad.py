# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

"""Asset Scrap/Restore valuation + JE integrity (v5.5.53).

ERPNext core ``restore_asset`` has two defects relevant to repeated Scrap/Restore:

1. **Asset VAD leak** — reverse disposal-day depreciation restores Finance Book
   ``value_after_depreciation`` but not Asset-level VAD (expense credit on the
   reverse JE never hits ``Journal Entry.update_asset_on_depreciation``).

2. **Orphan reverse JE pairs** — core creates a *new* reverse Depreciation Entry
   (``create_reverse_depreciation_entry``, posting ``nowdate()``) instead of
   cancelling the scrap-day depreciation JE. The scrap Asset Disposal JE *is*
   cancelled. Repeated cycles therefore accumulate submitted scrap-day + reverse
   pairs. Net GL for a completed cycle is zero, but period P&L is distorted
   (expense on scrap date, credit on restore date) and JE lists grow.

Design (compatibility-first):

- Call **core** ``restore_asset`` for permissions, schedule reset, scrap JE cancel,
  status, and activity.
- Temporarily replace only ``reverse_depreciation_entry_made_on_disposal`` with a
  cancel-first unwind that uses the same eligibility rules as core.
- If cancel is blocked by a closed/frozen accounting period, fall back to core
  reverse for that JE (documented, not silent) — never bypass period locks.
- Never use ``ignore_permissions`` on Journal Entry cancel.
- Sync Asset header VAD from the **default** Finance Book after restore.

No ERPNext core patch.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt

from erpnext.assets.doctype.asset.depreciation import (
	create_reverse_depreciation_entry,
	disposal_happens_in_the_future,
	disposal_was_made_on_original_schedule_date,
	update_value_after_depreciation_on_asset_restore,
)
from erpnext.assets.doctype.asset_depreciation_schedule.asset_depreciation_schedule import (
	get_asset_depr_schedule_doc,
)

# Exception text markers used by ERPNext/Frappe when cancel is blocked by books.
_PERIOD_LOCK_MARKERS = (
	"period closing",
	"period has been closed",
	"closed period",
	"frozen account",
	"account is frozen",
	"cannot cancel",
	"cannot be cancelled",
	"cannot be canceled",
	"books have been closed",
	"fiscal year",
)


def _default_finance_book_row(asset):
	"""Respect ERPNext default Finance Book semantics (not always row 0)."""
	if not asset.get("finance_books"):
		return None
	idx = asset.get_default_finance_book_idx()
	if idx is None:
		idx = 0
	return asset.finance_books[idx]


def sync_asset_vad_from_finance_book(asset_name: str) -> dict:
	"""Set Asset.value_after_depreciation from the default Finance Book VAD.

	Idempotent. Mirrors upstream develop ``Asset.sync_value_after_depreciation``
	(a1ff01724d) so a future core fix remains compatible (absolute assign).
	"""
	if not asset_name or not frappe.db.exists("Asset", asset_name):
		return {"asset": asset_name, "skipped": "missing_asset"}
	asset = frappe.get_doc("Asset", asset_name)
	if not asset.calculate_depreciation:
		return {"asset": asset_name, "skipped": "no_calculate_depreciation"}
	row = _default_finance_book_row(asset)
	if not row:
		return {"asset": asset_name, "skipped": "no_finance_books"}

	# Re-read FB VAD from DB (cancel/reverse may have updated the row).
	fb_vad = flt(
		frappe.db.get_value("Asset Finance Book", row.name, "value_after_depreciation"),
		asset.precision("value_after_depreciation"),
	)
	before = flt(asset.value_after_depreciation)
	changed = abs(before - fb_vad) > 1e-9
	if changed:
		asset.db_set("value_after_depreciation", fb_vad, update_modified=False)
	asset.set_total_booked_depreciations()
	return {
		"asset": asset_name,
		"changed": changed,
		"asset_vad_before": before,
		"asset_vad_after": fb_vad,
		"fb_vad": fb_vad,
		"finance_book": row.finance_book,
		"finance_book_row": row.name,
	}


def assert_asset_fb_vad_parity(asset_name: str, *, context: str = "") -> dict:
	"""Return parity report against the default Finance Book; does not throw."""
	asset = frappe.get_doc("Asset", asset_name)
	row = _default_finance_book_row(asset)
	fb_vad = (
		flt(frappe.db.get_value("Asset Finance Book", row.name, "value_after_depreciation"))
		if row
		else None
	)
	asset_vad = flt(asset.value_after_depreciation)
	return {
		"asset": asset_name,
		"context": context,
		"asset_vad": asset_vad,
		"fb_vad": fb_vad,
		"in_sync": fb_vad is not None and abs(asset_vad - fb_vad) < 1e-9,
	}


def _is_period_or_books_lock_error(exc: BaseException) -> bool:
	"""True when cancel failed because books/period are locked (not permissions)."""
	if isinstance(exc, frappe.PermissionError | frappe.AuthenticationError):
		return False
	msg = f"{exc}".lower()
	return any(marker in msg for marker in _PERIOD_LOCK_MARKERS)


def _should_unwind_schedule_row(schedule_idx, row, disposal_date) -> bool:
	"""Same eligibility as core ``reverse_depreciation_entry_made_on_disposal``."""
	return (not disposal_was_made_on_original_schedule_date(schedule_idx, row, disposal_date)) or (
		disposal_happens_in_the_future(disposal_date)
	)


def cancel_or_reverse_disposal_day_depreciation(asset) -> list[dict]:
	"""Unwind scrap-day depreciation: prefer cancel, fall back to core reverse.

	Accounting policy
	-----------------
	* **Cancel** when permitted — restores GL on the original posting date and
	  lets ``Journal Entry.unlink_asset_reference`` restore **both** Asset and
	  Finance Book VAD.
	* **Reverse** (core) when cancel is blocked by a closed/frozen period —
	  justified fallback matching ERPNext default; posts reverse on ``nowdate()``.
	  Asset VAD is repaired afterwards by ``sync_asset_vad_from_finance_book``.
	* Permission failures always raise (no ``ignore_permissions``).
	"""
	actions: list[dict] = []
	# Match core: attribute access (not asset.get), so Document and plain objects work.
	disposal_date = getattr(asset, "disposal_date", None)
	if not disposal_date:
		return actions

	for row in asset.get("finance_books") or []:
		schedule_doc = get_asset_depr_schedule_doc(asset.name, "Active", row.finance_book)
		if not schedule_doc or not schedule_doc.get("depreciation_schedule"):
			continue

		for schedule_idx, schedule in enumerate(schedule_doc.get("depreciation_schedule")):
			if schedule.schedule_date != disposal_date or not schedule.journal_entry:
				continue

			je_name = schedule.journal_entry
			if not _should_unwind_schedule_row(schedule_idx, row, disposal_date):
				actions.append(
					{
						"journal_entry": je_name,
						"mode": "skipped",
						"reason": "original_schedule_date",
					}
				)
				continue

			je = frappe.get_doc("Journal Entry", je_name)
			if je.docstatus != 1:
				actions.append(
					{"journal_entry": je_name, "mode": "skipped", "reason": f"docstatus={je.docstatus}"}
				)
				continue
			if je.voucher_type != "Depreciation Entry":
				actions.append(
					{
						"journal_entry": je_name,
						"mode": "skipped",
						"reason": f"voucher_type={je.voucher_type}",
					}
				)
				continue
			if je.reversal_of:
				actions.append(
					{"journal_entry": je_name, "mode": "skipped", "reason": "is_reverse_entry"}
				)
				continue

			try:
				# Same permission model as core cancel_journal_entry_for_scrap:
				# no ignore_permissions.
				je.cancel()
				actions.append({"journal_entry": je_name, "mode": "cancel"})
				if frappe.db.get_value("Depreciation Schedule", schedule.name, "journal_entry"):
					frappe.db.set_value(
						"Depreciation Schedule",
						schedule.name,
						"journal_entry",
						None,
						update_modified=False,
					)
			except frappe.PermissionError:
				raise
			except Exception as exc:
				if not _is_period_or_books_lock_error(exc):
					raise
				# Justified fallback: core reverse when cancel is period-blocked.
				frappe.msgprint(
					_(
						"Could not cancel scrap-day depreciation {0} ({1}). "
						"Falling back to a reversing Journal Entry (ERPNext default)."
					).format(frappe.bold(je_name), str(exc)[:180]),
					indicator="orange",
					alert=True,
				)
				reverse_je = create_reverse_depreciation_entry(asset.name, je_name)
				update_value_after_depreciation_on_asset_restore(schedule, row, reverse_je)
				actions.append(
					{
						"journal_entry": je_name,
						"mode": "reverse_fallback",
						"reverse_journal_entry": reverse_je.name if reverse_je else None,
						"reason": str(exc)[:240],
					}
				)

	return actions


def cancel_scrap_day_depreciation_entries(asset) -> dict:
	"""Cancel-only helper (no reverse fallback). Prefer ``cancel_or_reverse_*``.

	Raises on period lock — callers that need fallback should use
	``cancel_or_reverse_disposal_day_depreciation`` instead.
	"""
	cancelled: list[str] = []
	skipped: list[dict] = []
	disposal_date = getattr(asset, "disposal_date", None)
	if not disposal_date:
		return {"cancelled": cancelled, "skipped": skipped, "reason": "no_disposal_date"}

	for row in asset.get("finance_books") or []:
		schedule_doc = get_asset_depr_schedule_doc(asset.name, "Active", row.finance_book)
		if not schedule_doc or not schedule_doc.get("depreciation_schedule"):
			continue
		for schedule_idx, schedule in enumerate(schedule_doc.get("depreciation_schedule")):
			if schedule.schedule_date != disposal_date or not schedule.journal_entry:
				continue
			je_name = schedule.journal_entry
			if not _should_unwind_schedule_row(schedule_idx, row, disposal_date):
				skipped.append({"journal_entry": je_name, "reason": "original_schedule_date"})
				continue
			je = frappe.get_doc("Journal Entry", je_name)
			if je.docstatus != 1 or je.voucher_type != "Depreciation Entry" or je.reversal_of:
				skipped.append({"journal_entry": je_name, "reason": "not_cancellable_scrap_day"})
				continue
			je.cancel()
			cancelled.append(je_name)
			if frappe.db.get_value("Depreciation Schedule", schedule.name, "journal_entry"):
				frappe.db.set_value(
					"Depreciation Schedule",
					schedule.name,
					"journal_entry",
					None,
					update_modified=False,
				)
	return {"cancelled": cancelled, "skipped": skipped}


def restore_asset_accounting(asset_name: str) -> dict:
	"""Restore via core ``restore_asset``, replacing only the reverse step.

	1. Patch ``reverse_depreciation_entry_made_on_disposal`` → cancel-or-reverse.
	2. Call core ``restore_asset`` (permissions, schedule, scrap JE cancel, status,
	   activity).
	3. Sync Asset header VAD from the default Finance Book.
	"""
	from erpnext.assets.doctype.asset import depreciation as depr_mod

	if not asset_name:
		frappe.throw(_("Asset name is required"))
	if not frappe.db.exists("Asset", asset_name):
		frappe.throw(_("Asset {0} not found").format(frappe.bold(asset_name)))

	report: dict = {
		"asset": asset_name,
		"unwind_actions": [],
		"cancelled_scrap_day_jes": [],
		"reverse_fallback_jes": [],
		"skipped": [],
		"core_restore_called": False,
	}

	original_reverse = depr_mod.reverse_depreciation_entry_made_on_disposal

	def _unwind(asset):
		actions = cancel_or_reverse_disposal_day_depreciation(asset)
		report["unwind_actions"] = actions
		report["cancelled_scrap_day_jes"] = [a["journal_entry"] for a in actions if a.get("mode") == "cancel"]
		report["reverse_fallback_jes"] = [
			a.get("reverse_journal_entry") for a in actions if a.get("mode") == "reverse_fallback"
		]
		report["skipped"] = [a for a in actions if a.get("mode") == "skipped"]

	depr_mod.reverse_depreciation_entry_made_on_disposal = _unwind
	try:
		# Core owns: Asset write permission, schedule reset, scrap JE cancel,
		# set_status, asset activity. No mid-restore commits here.
		depr_mod.restore_asset(asset_name)
		report["core_restore_called"] = True
	finally:
		depr_mod.reverse_depreciation_entry_made_on_disposal = original_reverse

	report["vad_sync"] = sync_asset_vad_from_finance_book(asset_name)
	return report

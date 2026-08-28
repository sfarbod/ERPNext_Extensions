# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Job Card guards for the v16 Track-Semi-Finished-Goods flow (see hooks.doc_events).

Both were checked against ERPNext 16.33.0 and are still needed there:

* ``after_insert`` — ``create_job_card`` sets ``semi_fg_bom`` from the Work Order Operation
  row's ``bom_no`` (empty on this site) at Work Order submit, but from the parent BOM in the
  *Create Job Card* dialog. The Job Card Manufacture entry carries ``bom_no = semi_fg_bom`` and
  ``get_consumed_operating_cost`` filters on ``bom_no``, so lots posted from the two kinds of
  card do not see each other and the operating cost is allocated twice.
* ``validate`` — ``validate_job_card_qty`` still sums the raw ``for_quantity`` of every card of
  the operation, so a Pending Qty left on a completed card is counted as claimed quantity and
  the remainder of the operation can no longer get a card.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt

PENDING_QTY_RULE = _(
	"Qty to Manufacture in this Cycle must equal the quantity this Job Card actually produced "
	"(Completed Qty), with Pending Qty = 0. Leaving a Pending Qty on the card locks the remaining "
	"quantity of the operation: no further Job Card can be created for it (core validate_job_card_qty "
	"ignores pending_qty) and the only recovery is cancelling the Stock Entry, the transfer and the card. "
	"Re-open Complete Job and set the cycle qty to the completed qty."
)


def after_insert(doc, method=None):
	"""Cards created at Work Order submit have no ``semi_fg_bom`` while cards created from the
	WO *Create Job Card* dialog get the parent BOM. Their Manufacture entries then carry a
	different ``bom_no`` and ``get_consumed_operating_cost`` (filters on bom_no) stops seeing
	earlier lots — operating cost is allocated twice. Give every card the same BOM."""
	if doc.get("semi_fg_bom") or not doc.get("work_order"):
		return
	bom_no, track = frappe.db.get_value("Work Order", doc.work_order, ["bom_no", "track_semi_finished_goods"])
	if track and bom_no:
		doc.db_set("semi_fg_bom", bom_no, update_modified=False)


def validate(doc, method=None):
	"""Refuse the Complete-Job dialog values that would strand the remaining quantity."""
	if doc.docstatus != 0 or not doc.get("track_semi_finished_goods"):
		return
	if flt(doc.pending_qty) > 0 and flt(doc.total_completed_qty) > 0:
		frappe.throw(PENDING_QTY_RULE, title=_("Pending Qty not allowed"))

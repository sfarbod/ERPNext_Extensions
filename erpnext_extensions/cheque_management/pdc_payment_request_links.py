# Copyright (c) 2026, Farbod Siyahpoosh and contributors
# For license information, please see license.txt

"""Payment Request ↔ Post Dated Cheque **traceability** links (dashboard / list).

Authoritative relationship (same fields as PDC allocation / settlement capacity):

* ``tabPDC Allocation.reference_doctype`` / ``reference_name`` = Payment Request
* ``tabPDC Allocation.source_doctype`` / ``source_name`` = Payment Request (invoice rows sourced from PR)
* Header ``tabPost Dated Cheque.reference_doctype`` / ``reference_name`` (set by create-from-source)

This is **historical discovery**, not settlement capacity: Draft, Registered, settled (Register JE
posted), and Cancelled PDCs remain discoverable while the relationship rows still exist.

Does not change allocation, outstanding, or JE posting.
"""

from __future__ import annotations

import json

import frappe

_PDC_DOCTYPE = "Post Dated Cheque"
_PR_DOCTYPE = "Payment Request"

# Soft "open" badge: still in lifecycle (not cancelled). Total count includes cancelled.
_CANCELLED_WORKFLOW = frozenset({"Cancelled"})


def get_post_dated_cheque_names_for_payment_request(payment_request: str | None) -> list[str]:
	"""Return distinct Post Dated Cheque names historically linked to a Payment Request.

	Uses one SQL round-trip (UNION of allocation reference/source + header reference).
	Order is stable by ``creation`` then ``name``.
	"""
	pr = (payment_request or "").strip()
	if not pr:
		return []

	rows = frappe.db.sql(
		"""
		select name from (
			select p.name as name, p.creation as creation
			from `tabPost Dated Cheque` p
			where p.reference_doctype = %s and p.reference_name = %s
			union
			select p.name as name, p.creation as creation
			from `tabPDC Allocation` a
			inner join `tabPost Dated Cheque` p on p.name = a.parent
			where a.parenttype = 'Post Dated Cheque'
				and (
					(a.reference_doctype = %s and a.reference_name = %s)
					or (a.source_doctype = %s and a.source_name = %s)
				)
		) linked
		order by creation asc, name asc
		""",
		(_PR_DOCTYPE, pr, _PR_DOCTYPE, pr, _PR_DOCTYPE, pr),
	)
	return [r[0] for r in rows if r and r[0]]


def count_post_dated_cheques_for_payment_request(payment_request: str | None) -> int:
	"""Count of related PDCs — same set as :func:`get_post_dated_cheque_names_for_payment_request`."""
	return len(get_post_dated_cheque_names_for_payment_request(payment_request))


def _open_count_among_names(names: list[str]) -> int:
	"""PDCs that are not cancelled (workflow / docstatus) — for the orange open badge."""
	if not names:
		return 0
	# Chunk to keep IN lists bounded
	open_n = 0
	chunk = 100
	for i in range(0, len(names), chunk):
		part = names[i : i + chunk]
		rows = frappe.db.sql(
			"""
			select count(*)
			from `tabPost Dated Cheque`
			where name in ({})
				and ifnull(docstatus, 0) < 2
				and ifnull(workflow_state, '') not in ('Cancelled')
			""".format(",".join(["%s"] * len(part))),
			tuple(part),
		)
		open_n += int(rows[0][0]) if rows else 0
	return open_n


def build_pdc_internal_link_payload(payment_request: str) -> dict:
	"""Dashboard ``internal_links_found`` row so count and List filter use the same names."""
	names = get_post_dated_cheque_names_for_payment_request(payment_request)
	return {
		"doctype": _PDC_DOCTYPE,
		"count": len(names),
		"open_count": _open_count_among_names(names),
		"names": names,
	}


@frappe.whitelist()
def get_payment_request_open_count(
	doctype: str | None = None,
	name: str | None = None,
	items: str | list | None = None,
):
	"""Desk dashboard open-count for Payment Request — injects Post Dated Cheque as an internal link.

	Standard :func:`frappe.desk.notifications.get_open_count` cannot express child-table reverse
	links on another doctype. Returning PDC under ``internal_links_found`` with ``names`` makes the
	badge count and the List ``name in (...)`` filter share one authoritative query.
	"""
	from frappe.desk.notifications import get_open_count

	if (doctype or "").strip() != _PR_DOCTYPE:
		return get_open_count(doctype, name, items)

	# Parse items like core get_open_count
	item_list: list[str]
	if items is None:
		item_list = []
	elif isinstance(items, str):
		try:
			parsed = json.loads(items)
			item_list = list(parsed) if isinstance(parsed, list) else []
		except (TypeError, ValueError):
			item_list = []
	else:
		item_list = list(items)

	# Ask core for every linked doctype except PDC (we replace that entry).
	core_items = [i for i in item_list if i != _PDC_DOCTYPE]
	result = get_open_count(doctype, name, core_items if core_items else None) or {}
	count_block = result.get("count") or {}
	if not isinstance(count_block, dict):
		count_block = {}
		result["count"] = count_block

	ext = list(count_block.get("external_links_found") or [])
	intl = list(count_block.get("internal_links_found") or [])
	ext = [row for row in ext if (row or {}).get("doctype") != _PDC_DOCTYPE]
	intl = [row for row in intl if (row or {}).get("doctype") != _PDC_DOCTYPE]

	# Always include PDC as internal (even when count=0) so click uses names-list path, not a
	# wrong header filter such as ``payment_request`` on Post Dated Cheque.
	if _PDC_DOCTYPE in item_list or not item_list:
		intl.append(build_pdc_internal_link_payload(name or ""))

	count_block["external_links_found"] = ext
	count_block["internal_links_found"] = intl
	result["count"] = count_block
	return result


__all__ = [
	"build_pdc_internal_link_payload",
	"count_post_dated_cheques_for_payment_request",
	"get_payment_request_open_count",
	"get_post_dated_cheque_names_for_payment_request",
]

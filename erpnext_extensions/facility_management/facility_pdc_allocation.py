# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

"""Debt Purchase Facility ↔ PDC allocation (master data only).

Does not create/cancel Journal Entries, GL Entries, or Facility Repayments.
Settlement ownership remains on Post Dated Cheque.debt_purchase_* fields.
"""

from __future__ import annotations

from decimal import Decimal

import frappe
from frappe import _
from frappe.utils import cint

from erpnext_extensions.cheque_management.pdc_workflow_state_machine import (
	CHEQUE_DIRECTION_RECEIVABLE,
	WORKFLOW_ASSIGNED_DEBT_PURCHASE,
	WORKFLOW_DEBT_PURCHASE_SETTLED,
	normalize_workflow_state_value,
)
from erpnext_extensions.facility_management.facility_identity import (
	acquire_named_lock,
	_named_lock_key,
)
from erpnext_extensions.facility_management.facility_monetary import parse_facility_amount

_LOCK_PREFIX_PDC_ALLOC = "fac_pdc:"

# Cheque statuses that must never be newly allocated.
_BLOCKED_CHEQUE_STATUSES = frozenset(
	{
		"Cancelled",
		"Bounced",
		"Replaced",
		"Returned to Customer",
		"Returned from Payee",
	}
)


def facility_is_debt_purchase(facility) -> bool:
	ft = (getattr(facility, "facility_type", None) or "").strip()
	if not ft:
		return False
	return bool(cint(frappe.db.get_value("Facility Type", ft, "is_debt_purchase")))


def allocated_pdc_names(facility) -> list[str]:
	names: list[str] = []
	for row in facility.get("debt_purchase_pdcs") or []:
		pdc = (row.get("post_dated_cheque") or "").strip()
		if pdc:
			names.append(pdc)
	return names


def sum_allocated_cheque_amounts(facility) -> Decimal:
	total = Decimal("0")
	for row in facility.get("debt_purchase_pdcs") or []:
		# Prefer live PDC amount (authoritative); fall back to fetched child value.
		pdc = (row.get("post_dated_cheque") or "").strip()
		if pdc and frappe.db.exists("Post Dated Cheque", pdc):
			amt = frappe.db.get_value("Post Dated Cheque", pdc, "cheque_amount")
			total += parse_facility_amount(amt)
		else:
			total += parse_facility_amount(row.get("cheque_amount"))
	return total


def facility_contract_total(facility) -> Decimal:
	return parse_facility_amount(facility.get("principal_amount")) + parse_facility_amount(
		facility.get("profit_amount")
	)


def format_irr_amount(amount: Decimal) -> str:
	"""Human-readable integer IRR for Persian validation messages."""
	quantized = amount.quantize(Decimal("1"))
	return f"{quantized:,}".replace(",", "٬")


def _acquire_pdc_allocation_lock(pdc_name: str) -> None:
	if not pdc_name:
		return
	acquire_named_lock(_named_lock_key(_LOCK_PREFIX_PDC_ALLOC, pdc_name))


def _db_owner_of_pdc(pdc_name: str, *, exclude_facility: str | None) -> str | None:
	rows = frappe.db.sql(
		"""
		SELECT parent FROM `tabFacility PDC Allocation`
		WHERE post_dated_cheque = %s
		  AND parenttype = 'Facility'
		  AND IFNULL(parent, '') != %s
		LIMIT 1
		""",
		(pdc_name, exclude_facility or ""),
	)
	return rows[0][0] if rows else None


def _submitted_repayment_using_pdc(facility_name: str, pdc_name: str) -> str | None:
	return frappe.db.get_value(
		"Facility Repayment",
		{
			"facility": facility_name,
			"post_dated_cheque": pdc_name,
			"docstatus": 1,
		},
		"name",
	)


def _refresh_row_from_pdc(row, pdc) -> None:
	"""Overwrite display columns from the linked PDC (never trust client edits)."""
	row.cheque_no = pdc.cheque_no
	row.cheque_due_date = pdc.cheque_due_date
	row.drawer_bank_name = pdc.drawer_bank_name
	row.cheque_amount = pdc.cheque_amount
	row.cheque_status = pdc.cheque_status


def _validate_pdc_eligible_for_allocation(pdc, facility) -> None:
	# Post Dated Cheque Debt Purchase lifecycle is workflow-driven; documents often
	# remain docstatus=0 while Assigned/Settled (same as repayment eligibility).
	if cint(pdc.docstatus) == 2:
		frappe.throw(
			_("چک {0} لغو شده و قابل تخصیص نیست.").format(pdc.name),
			title=_("تخصیص چک خرید دین"),
		)
	if (pdc.cheque_direction or "").strip() != CHEQUE_DIRECTION_RECEIVABLE:
		frappe.throw(
			_("چک {0} باید دریافتنی (Receivable) باشد.").format(pdc.name),
			title=_("تخصیص چک خرید دین"),
		)
	if (pdc.company or "").strip() != (facility.company or "").strip():
		frappe.throw(
			_("شرکت چک {0} با شرکت تسهیلات یکسان نیست.").format(pdc.name),
			title=_("تخصیص چک خرید دین"),
		)
	status = (pdc.cheque_status or "").strip()
	if status in _BLOCKED_CHEQUE_STATUSES:
		frappe.throw(
			_("وضعیت چک {0} ({1}) برای تخصیص مجاز نیست.").format(pdc.name, status or "—"),
			title=_("تخصیص چک خرید دین"),
		)
	ws = normalize_workflow_state_value(pdc.workflow_state)
	# Settled PDCs may be attached for historical documentation only when they
	# already settle (or previously settled) this same Facility.
	if ws == WORKFLOW_DEBT_PURCHASE_SETTLED:
		linked_fac = (getattr(pdc, "debt_purchase_facility", None) or "").strip()
		if linked_fac and linked_fac != facility.name:
			frappe.throw(
				_("چک {0} قبلاً برای تسهیلات دیگری تسویه شده است ({1}).").format(
					pdc.name, linked_fac
				),
				title=_("تخصیص چک خرید دین"),
			)
		return
	if ws != WORKFLOW_ASSIGNED_DEBT_PURCHASE:
		frappe.throw(
			_(
				"چک {0} باید در وضعیت گردش‌کار «{1}» باشد. وضعیت فعلی: {2}."
			).format(pdc.name, WORKFLOW_ASSIGNED_DEBT_PURCHASE, ws or "—"),
			title=_("تخصیص چک خرید دین"),
		)


def assert_activation_allocation_equals_facility_total(facility) -> None:
	"""Block Draft→Active (and API activation) unless PDC sum == principal+profit."""
	if not facility_is_debt_purchase(facility):
		return
	facility_total = facility_contract_total(facility)
	allocated = sum_allocated_cheque_amounts(facility)
	diff = allocated - facility_total
	if diff != 0:
		frappe.throw(
			_(
				"برای فعال‌سازی تسهیلات خرید دین، جمع مبلغ چک‌های تخصیص‌یافته باید دقیقاً برابر "
				"جمع اصل و فرع تسهیلات باشد.\n\n"
				"جمع تسهیلات (اصل + فرع): {0} ریال\n"
				"جمع چک‌های تخصیص‌یافته: {1} ریال\n"
				"اختلاف: {2} ریال"
			).format(
				format_irr_amount(facility_total),
				format_irr_amount(allocated),
				format_irr_amount(diff),
			),
			title=_("تخصیص چک خرید دین"),
		)


def is_activating_facility(facility) -> bool:
	if (facility.status or "").strip() != "Active":
		return False
	if facility.is_new():
		return True
	prev = (frappe.db.get_value("Facility", facility.name, "status") or "").strip()
	return prev != "Active"


def validate_facility_pdc_allocations(facility) -> None:
	"""Server-side allocation rules. Safe for historical Active with empty table."""
	rows = facility.get("debt_purchase_pdcs") or []
	is_dp = facility_is_debt_purchase(facility)
	status = (facility.status or "").strip()

	if not is_dp:
		if rows:
			frappe.throw(
				_("جدول چک‌های خرید دین فقط برای تسهیلات با نوع خرید دین مجاز است."),
				title=_("تخصیص چک خرید دین"),
			)
		return

	if status == "Closed":
		# Compare against DB: any mutation of allocation rows is blocked.
		if not facility.is_new():
			db_pdcs = set(
				frappe.get_all(
					"Facility PDC Allocation",
					filters={"parent": facility.name, "parenttype": "Facility"},
					pluck="post_dated_cheque",
				)
			)
			cur_pdcs = set(allocated_pdc_names(facility))
			if db_pdcs != cur_pdcs:
				frappe.throw(
					_("ویرایش تخصیص چک برای تسهیلات بسته‌شده مجاز نیست."),
					title=_("تخصیص چک خرید دین"),
				)
		elif rows:
			frappe.throw(
				_("تسهیلات بسته‌شده نمی‌تواند تخصیص چک جدید داشته باشد."),
				title=_("تخصیص چک خرید دین"),
			)
		return

	seen: set[str] = set()
	previous_pdcs: set[str] = set()
	if not facility.is_new():
		previous_pdcs = set(
			frappe.get_all(
				"Facility PDC Allocation",
				filters={"parent": facility.name, "parenttype": "Facility"},
				pluck="post_dated_cheque",
			)
		)

	current_pdcs = set(allocated_pdc_names(facility))
	removed = previous_pdcs - current_pdcs
	for pdc_name in removed:
		rep = _submitted_repayment_using_pdc(facility.name, pdc_name)
		if rep:
			frappe.throw(
				_(
					"امکان حذف چک {0} وجود ندارد؛ در بازپرداخت ثبت‌شده {1} استفاده شده است."
				).format(pdc_name, rep),
				title=_("تخصیص چک خرید دین"),
			)

	for row in rows:
		pdc_name = (row.get("post_dated_cheque") or "").strip()
		if not pdc_name:
			frappe.throw(_("ردیف تخصیص بدون انتخاب چک مجاز نیست."), title=_("تخصیص چک خرید دین"))
		if pdc_name in seen:
			frappe.throw(
				_("چک {0} بیش از یک‌بار در همین تسهیلات تخصیص داده شده است.").format(pdc_name),
				title=_("تخصیص چک خرید دین"),
			)
		seen.add(pdc_name)

		_acquire_pdc_allocation_lock(pdc_name)
		other = _db_owner_of_pdc(pdc_name, exclude_facility=facility.name)
		if other:
			frappe.throw(
				_("چک {0} قبلاً به تسهیلات {1} تخصیص داده شده است.").format(pdc_name, other),
				title=_("تخصیص چک خرید دین"),
			)

		if not frappe.db.exists("Post Dated Cheque", pdc_name):
			frappe.throw(_("چک {0} یافت نشد.").format(pdc_name), title=_("تخصیص چک خرید دین"))
		pdc = frappe.get_doc("Post Dated Cheque", pdc_name)
		_validate_pdc_eligible_for_allocation(pdc, facility)
		_refresh_row_from_pdc(row, pdc)

	if is_activating_facility(facility):
		assert_activation_allocation_equals_facility_total(facility)


def pdc_is_allocated_to_facility(facility_name: str, pdc_name: str) -> bool:
	if not facility_name or not pdc_name:
		return False
	return bool(
		frappe.db.exists(
			"Facility PDC Allocation",
			{
				"parent": facility_name,
				"parenttype": "Facility",
				"post_dated_cheque": pdc_name,
			},
		)
	)


def assert_repayment_pdc_allocated(repayment, facility) -> None:
	"""Reject Debt Purchase Cheque repayments whose PDC is not on the Facility table.

	Called from repayment ``validate`` (draft/new submit path). Cancel restore does not
	re-run this gate, so historical submitted repayments remain cancellable.
	"""
	pdc_name = (getattr(repayment, "post_dated_cheque", None) or "").strip()
	if not pdc_name:
		return
	if pdc_is_allocated_to_facility(facility.name, pdc_name):
		return
	frappe.throw(
		_(
			"چک {0} به تسهیلات {1} تخصیص داده نشده است. "
			"فقط چک‌های موجود در جدول «چک‌های خرید دین» قابل انتخاب هستند."
		).format(pdc_name, facility.name),
		title=_("تخصیص چک خرید دین"),
	)


__all__ = [
	"facility_is_debt_purchase",
	"allocated_pdc_names",
	"sum_allocated_cheque_amounts",
	"facility_contract_total",
	"format_irr_amount",
	"assert_activation_allocation_equals_facility_total",
	"is_activating_facility",
	"validate_facility_pdc_allocations",
	"pdc_is_allocated_to_facility",
	"assert_repayment_pdc_allocated",
]

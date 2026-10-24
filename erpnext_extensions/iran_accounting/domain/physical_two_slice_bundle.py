# Copyright (c) 2026, ERPNext Extensions contributors
"""Apply Physical Two-Slice plans to Serial and Batch Bundles (Approach A).

- Rewrites SABE children after Core Moving Average valuation
- Scoped duplicate-batch exception for allocator-authorized two-slice only
- Does not modify ERPNext Core source
"""

from __future__ import annotations

from collections import Counter
from decimal import Decimal

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.domain.physical_two_slice import (
	AllocationError,
	AllocationPlan,
	allocate_physical_two_slice,
	assert_slice_identity,
)

OUTGOING_VOUCHER_TYPES = frozenset({"Delivery Note", "Sales Invoice"})
TWO_SLICE_BUNDLES_FLAG = "iran_physical_two_slice_bundles"
IRAN_TWO_SLICE_PARENT_ATTR = "_iran_physical_two_slice_bundle"


def _d(v) -> Decimal:
	return Decimal(str(v if v is not None else 0))


def _cint(v) -> int:
	try:
		return int(v or 0)
	except Exception:
		return 0


def mark_two_slice_bundle(bundle_name: str) -> None:
	if not bundle_name:
		return
	flags = getattr(frappe.local, TWO_SLICE_BUNDLES_FLAG, None)
	if flags is None:
		frappe.local.iran_physical_two_slice_bundles = set()
		flags = frappe.local.iran_physical_two_slice_bundles
	flags.add(bundle_name)


def is_two_slice_bundle_marked(bundle_name: str) -> bool:
	if not bundle_name:
		return False
	flags = getattr(frappe.local, TWO_SLICE_BUNDLES_FLAG, None) or set()
	return bundle_name in flags


def read_batch_pool(item_code: str, batch_no: str, warehouse: str, exclude_voucher_detail_no=None):
	"""Submitted SABE net qty/value for batch@warehouse, excluding current detail."""
	params = [item_code, batch_no, warehouse]
	excl = ""
	if exclude_voucher_detail_no:
		excl = " AND IFNULL(e.voucher_detail_no,'') != %s"
		params.append(exclude_voucher_detail_no)
	row = frappe.db.sql(
		f"""
		SELECT COALESCE(SUM(e.qty), 0) AS qty,
		       COALESCE(SUM(e.stock_value_difference), 0) AS svd
		FROM `tabSerial and Batch Entry` e
		WHERE e.item_code=%s AND e.batch_no=%s AND e.warehouse=%s
		  AND e.docstatus=1 AND e.is_cancelled=0
		  AND e.type_of_transaction IN ('Inward', 'Outward')
		  {excl}
		""",
		tuple(params),
		as_dict=True,
	)[0]
	return _d(row.qty), _d(row.svd)


def apply_plan_to_bundle(
	bundle_name: str,
	plan: AllocationPlan,
	*,
	warehouse: str,
	item_code: str,
	batch_no: str,
	voucher_detail_no=None,
) -> list[str]:
	"""Rewrite bundle children to physical valuation slices; mark provenance."""
	entries = frappe.get_all(
		"Serial and Batch Entry",
		filters={"parent": bundle_name},
		fields=[
			"name",
			"qty",
			"batch_no",
			"idx",
			"docstatus",
			"warehouse",
			"item_code",
			"voucher_detail_no",
			"voucher_no",
			"voucher_type",
			"type_of_transaction",
			"posting_datetime",
			"is_cancelled",
		],
		order_by="idx",
	)
	if not entries:
		raise AllocationError(f"no SABE on bundle {bundle_name}")

	# Drop extras beyond the plan length (e.g. prior two-slice → single).
	while len(entries) > len(plan.slices):
		extra = entries.pop()
		frappe.delete_doc("Serial and Batch Entry", extra.name, force=1, ignore_permissions=True)

	created: list[str] = []
	for i, sl in enumerate(plan.slices):
		assert_slice_identity(sl)
		if i < len(entries):
			primary = entries[i]
			frappe.db.set_value(
				"Serial and Batch Entry",
				primary.name,
				{
					"qty": float(sl.qty),
					"incoming_rate": float(sl.rate),
					"stock_value_difference": float(sl.amount),
					"batch_no": batch_no,
					"warehouse": warehouse,
					"item_code": item_code,
				},
				update_modified=False,
			)
			created.append(primary.name)
		else:
			template = entries[0] if entries else None
			if template is None:
				raise AllocationError(f"cannot insert slice without template on {bundle_name}")
			child = frappe.get_doc(
				{
					"doctype": "Serial and Batch Entry",
					"parent": bundle_name,
					"parenttype": "Serial and Batch Bundle",
					"parentfield": "entries",
					"idx": i + 1,
					"batch_no": batch_no,
					"qty": float(sl.qty),
					"incoming_rate": float(sl.rate),
					"stock_value_difference": float(sl.amount),
					"warehouse": warehouse,
					"item_code": item_code,
					"voucher_detail_no": template.voucher_detail_no or voucher_detail_no,
					"voucher_no": template.voucher_no,
					"voucher_type": template.voucher_type,
					"type_of_transaction": template.type_of_transaction or "Outward",
					"posting_datetime": template.posting_datetime,
					"docstatus": template.docstatus or 1,
					"is_cancelled": 0,
				}
			)
			child.flags.ignore_links = True
			child.flags.ignore_validate = True
			child.flags.ignore_permissions = True
			child.insert(ignore_permissions=True)
			if _cint(template.docstatus) == 1:
				frappe.db.set_value(
					"Serial and Batch Entry", child.name, "docstatus", 1, update_modified=False
				)
			created.append(child.name)

	total_qty = float(sum((s.qty for s in plan.slices), Decimal(0)))
	total_amt = float(sum((s.amount for s in plan.slices), Decimal(0)))
	# Derived aggregate only — SABE rates remain the valuation source of truth.
	avg = abs(total_amt / total_qty) if total_qty else 0.0
	frappe.db.set_value(
		"Serial and Batch Bundle",
		bundle_name,
		{"total_qty": total_qty, "total_amount": total_amt, "avg_rate": avg},
		update_modified=False,
	)
	mark_two_slice_bundle(bundle_name)
	return created


def rewrite_outgoing_bundle_after_core(self, sle, doc) -> AllocationPlan | None:
	"""Replace Core MA fractional SABE valuation with Physical Two-Slice integers.

	Returns the plan when rewrite applied; None when this SLE is out of scope.
	"""
	from erpnext_extensions.iran_accounting.domain.currency import is_irr_company

	company = getattr(self, "company", None) or (sle.get("company") if hasattr(sle, "get") else None)
	if not company or not is_irr_company(company):
		return None

	voucher_type = sle.get("voucher_type") if hasattr(sle, "get") else None
	if voucher_type not in OUTGOING_VOUCHER_TYPES:
		return None
	if flt(sle.get("actual_qty") if hasattr(sle, "get") else 0) >= 0:
		return None

	bundle = sle.get("serial_and_batch_bundle") if hasattr(sle, "get") else None
	if not bundle:
		return None

	rows = frappe.get_all(
		"Serial and Batch Entry",
		filters={"parent": bundle},
		fields=["name", "batch_no", "qty", "warehouse", "item_code"],
	)
	if not rows:
		return None
	batch_nos = {r.batch_no for r in rows if r.batch_no}
	if len(batch_nos) != 1:
		return None

	batch_no = next(iter(batch_nos))
	warehouse = rows[0].warehouse or sle.get("warehouse")
	item_code = rows[0].item_code or sle.get("item_code")
	detail = sle.get("voucher_detail_no")

	pool_q, pool_v = read_batch_pool(
		item_code, batch_no, warehouse, exclude_voucher_detail_no=detail
	)
	out_qty = _d(abs(flt(sle.get("actual_qty"))))
	through_riv = bool(frappe.flags.get("through_repost_item_valuation"))

	# RIV may enqueue dependent Item/Warehouse pairs mid-chain so the visible
	# batch pool can be temporarily thinner than the outgoing qty. Fail closed
	# on live submit; during RIV fall through to Core MA + integerize path.
	if pool_q <= 0 or out_qty - pool_q > Decimal("1e-12"):
		if through_riv:
			return None
		raise AllocationError(
			f"empty/short pool for batch {batch_no} @ {warehouse} "
			f"(item {item_code}): pool_qty={pool_q} out_qty={out_qty}"
		)

	# Explicit auditable repair when historical SABE pool is fractional (Core MA float).
	if pool_v != pool_v.to_integral_value():
		from erpnext_extensions.iran_accounting.domain.historical_fractional_sabe_repair import (
			repair_batch_pool_fractional_irr,
		)

		company = getattr(self, "company", None) or (
			sle.get("company") if hasattr(sle, "get") else None
		)
		repair_batch_pool_fractional_irr(
			item_code,
			batch_no,
			warehouse,
			company=company,
			exclude_voucher_detail_no=detail,
			reason="pre_two_slice_allocate",
		)
		pool_q, pool_v = read_batch_pool(
			item_code, batch_no, warehouse, exclude_voucher_detail_no=detail
		)
		if pool_v != pool_v.to_integral_value():
			raise AllocationError(
				f"historical fractional pool_value={pool_v} remains after explicit "
				f"SABE repair; batch={batch_no} warehouse={warehouse}"
			)

	plan = allocate_physical_two_slice(pool_q, pool_v, out_qty)
	apply_plan_to_bundle(
		bundle,
		plan,
		warehouse=warehouse,
		item_code=item_code,
		batch_no=batch_no,
		voucher_detail_no=detail,
	)

	total_qty = float(sum((s.qty for s in plan.slices), Decimal(0)))
	total_amt = float(sum((s.amount for s in plan.slices), Decimal(0)))
	avg = abs(total_amt / total_qty) if total_qty else 0.0
	if doc is not None and hasattr(doc, "update"):
		doc.update({"total_qty": total_qty, "total_amount": total_amt, "avg_rate": avg})
	return plan


def _identity_ok(entry) -> bool:
	from erpnext_extensions.iran_accounting.domain.currency import round_row_amount_financial

	q = abs(flt(entry.qty))
	r = flt(entry.incoming_rate)
	svd = abs(flt(entry.stock_value_difference))
	if not q:
		return svd == 0
	# Prefer IRR RHU contract when company currency is available on parent.
	try:
		expected = abs(flt(round_row_amount_financial(q, r, "IRR")))
	except Exception:
		expected = abs(round(q * r))
	return abs(expected - svd) <= 0.5


def is_authorized_physical_two_slice(bundle_doc) -> bool:
	"""Narrow structural + provenance check for duplicate-batch exception."""
	if getattr(bundle_doc, "voucher_type", None) not in OUTGOING_VOUCHER_TYPES:
		return False
	if getattr(bundle_doc, "type_of_transaction", None) not in (None, "", "Outward"):
		# Explicit Inward must not use the exception
		if getattr(bundle_doc, "type_of_transaction", None) == "Inward":
			return False

	entries = list(bundle_doc.get("entries") or [])
	batch_only = [e for e in entries if e.batch_no and not e.serial_no]
	if len(batch_only) != 2:
		return False
	if batch_only[0].batch_no != batch_only[1].batch_no:
		return False

	# Same warehouse / item when set
	whs = {e.warehouse for e in batch_only if e.warehouse}
	items = {e.item_code for e in batch_only if e.item_code}
	if len(whs) > 1 or len(items) > 1:
		return False

	rates = []
	for e in batch_only:
		r = flt(e.incoming_rate)
		if abs(r - round(r)) > 1e-9:
			return False
		rates.append(int(round(r)))
		if not _identity_ok(e):
			return False
	rates_sorted = sorted(rates)
	if rates_sorted[1] - rates_sorted[0] not in (0, 1):
		return False

	# Provenance: in-flight rewrite mark, parent attr, or already-persisted integer two-slice
	# on an outgoing DN/SI bundle (needed for cancel/RIV reload without request-local flags).
	if is_two_slice_bundle_marked(bundle_doc.name):
		return True
	if getattr(bundle_doc, IRAN_TWO_SLICE_PARENT_ATTR, None):
		return True
	# Persisted authorized shape: both rates integer, identity holds, adjacent/same — accept
	# only when voucher_type is DN/SI outward (already checked) AND qty signs match outward.
	qtys = [flt(e.qty) for e in batch_only]
	if all(q <= 0 for q in qtys) or all(q >= 0 for q in qtys):
		# Totals must be consistent
		total_q = abs(sum(qtys))
		total_svd = abs(sum(flt(e.stock_value_difference) for e in batch_only))
		if total_q > 0 and total_svd == int(total_svd):
			return True
	return False


def make_validate_duplicate_wrapper(orig):
	"""Allow only allocator-authorized Physical Two-Slice duplicates; else Core."""

	def validate_duplicate_serial_and_batch_no(self):
		entries = list(self.get("entries") or [])
		batch_only = [e for e in entries if e.batch_no and not e.serial_no]
		counts = Counter(e.batch_no for e in batch_only)
		dup_batches = {b for b, c in counts.items() if c > 1}
		if not dup_batches:
			return orig(self)

		if is_authorized_physical_two_slice(self):
			# Still enforce serial uniqueness
			serial_nos = [e.serial_no for e in entries if e.serial_no]
			for key, value in Counter(serial_nos).items():
				if value > 1:
					self.throw_error_message(f"Duplicate Serial No {key} found")
			return None

		return orig(self)

	validate_duplicate_serial_and_batch_no._iran_physical_two_slice = True
	return validate_duplicate_serial_and_batch_no

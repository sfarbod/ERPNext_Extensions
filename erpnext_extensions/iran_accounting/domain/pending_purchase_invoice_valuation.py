# Copyright (c) 2026, ERPNext Extensions contributors
"""PENDING_PURCHASE_INVOICE_VALUATION — legitimate temporary zero-rate PR.

Business contract (generic, not voucher-hardcoded):

  submitted Purchase Receipt with document rate = 0
  + quantity legitimate
  + no submitted Purchase Invoice yet finalizing this receipt line
  + no contradictory finalized purchase valuation

⇒ temporary zero receipt valuation is ACCEPTED until PI posts.

Native RIV must represent this as zero-inbound (incoming_rate=0, svd=0) —
preserve warehouse stock value — without inventing a rate and without
weakening I3 for unexplained negative incoming SVD.

Lifecycle:
  PR zero / PI absent → PENDING_PURCHASE_INVOICE_VALUATION
  PI submitted        → classification disappears; native valuation resumes
"""

from __future__ import annotations

from frappe.utils import cint, flt

import frappe

CLASS_PENDING_PURCHASE_INVOICE_VALUATION = "PENDING_PURCHASE_INVOICE_VALUATION"
LEGITIMATE_PENDING_UPSTREAM_ACCOUNTING = "LEGITIMATE_PENDING_UPSTREAM_ACCOUNTING"
UNEXPLAINED_NEGATIVE_INCOMING_SVD = "UNEXPLAINED_NEGATIVE_INCOMING_SVD"

RATE_EPS = 1e-6
QTY_EPS = 1e-9


def _entry_get(obj, key, default=None):
	if obj is None:
		return default
	if isinstance(obj, dict) or hasattr(obj, "get"):
		try:
			return obj.get(key, default)
		except Exception:
			pass
	return getattr(obj, key, default)


def pr_item_document_rate(pr_name: str, *, item_code: str | None = None, pr_detail: str | None = None) -> float | None:
	"""Authoritative Purchase Receipt Item rate (document), not SLE."""
	if pr_detail:
		row = frappe.db.get_value(
			"Purchase Receipt Item",
			pr_detail,
			["parent", "item_code", "rate", "base_rate", "valuation_rate", "qty", "docstatus"],
			as_dict=True,
		)
		if not row:
			return None
		if pr_name and row.parent != pr_name:
			return None
		if item_code and row.item_code != item_code:
			return None
		# Prefer explicit rate; fall back to base_rate / valuation_rate.
		for field in ("rate", "base_rate", "valuation_rate"):
			if abs(flt(row.get(field))) > RATE_EPS:
				return flt(row.get(field))
		return 0.0

	if not pr_name or not item_code:
		return None
	rates = frappe.db.sql(
		"""
		SELECT rate, base_rate, valuation_rate
		FROM `tabPurchase Receipt Item`
		WHERE parent=%s AND item_code=%s
		""",
		(pr_name, item_code),
		as_dict=True,
	)
	if not rates:
		return None
	# All lines for this item must be zero to treat as pending-zero; any valued line rejects.
	for r in rates:
		for field in ("rate", "base_rate", "valuation_rate"):
			if abs(flt(r.get(field))) > RATE_EPS:
				return flt(r.get(field))
	return 0.0


def finalized_purchase_invoices_for_pr(
	pr_name: str,
	*,
	item_code: str | None = None,
	pr_detail: str | None = None,
) -> list[dict]:
	"""Submitted Purchase Invoices linked to this PR (optionally item / pr_detail)."""
	if not pr_name:
		return []
	conds = [
		"pii.purchase_receipt=%s",
		"pi.docstatus=1",
		"IFNULL(pi.is_return,0)=0",
	]
	args: list = [pr_name]
	if pr_detail:
		conds.append("pii.pr_detail=%s")
		args.append(pr_detail)
	if item_code:
		conds.append("pii.item_code=%s")
		args.append(item_code)
	return frappe.db.sql(
		f"""
		SELECT pi.name, pi.docstatus, pii.item_code, pii.rate, pii.base_rate,
		       pii.qty, pii.pr_detail, pii.purchase_receipt
		FROM `tabPurchase Invoice Item` pii
		INNER JOIN `tabPurchase Invoice` pi ON pi.name=pii.parent
		WHERE {" AND ".join(conds)}
		ORDER BY pi.creation
		""",
		args,
		as_dict=True,
	)


def has_finalized_purchase_valuation(
	pr_name: str,
	*,
	item_code: str | None = None,
	pr_detail: str | None = None,
) -> bool:
	"""True when a submitted PI exists for this receipt line (valuation authority available)."""
	rows = finalized_purchase_invoices_for_pr(pr_name, item_code=item_code, pr_detail=pr_detail)
	return bool(rows)


def classify_pending_purchase_invoice_valuation(
	*,
	voucher_type: str | None = None,
	voucher_no: str | None = None,
	item_code: str | None = None,
	voucher_detail_no: str | None = None,
	actual_qty: float | None = None,
) -> dict:
	"""Generic classifier — strong evidence required; never voucher-hardcoded.

	Returns status:
	  PENDING_PURCHASE_INVOICE_VALUATION — legitimate temporary state
	  NOT_PENDING — does not match / PI already final / PR valued / etc.
	"""
	out = {
		"status": "NOT_PENDING",
		"class": None,
		"repair_disposition": None,
		"pending": False,
		"reasons": [],
		"evidence": {},
	}
	if voucher_type != "Purchase Receipt" or not voucher_no:
		out["reasons"].append("not_purchase_receipt")
		return out

	pr = frappe.db.get_value(
		"Purchase Receipt",
		voucher_no,
		["name", "docstatus", "is_return", "company"],
		as_dict=True,
	)
	if not pr:
		out["reasons"].append("pr_missing")
		return out
	if cint(pr.docstatus) != 1:
		out["reasons"].append("pr_not_submitted")
		return out
	if cint(pr.is_return):
		out["reasons"].append("pr_is_return")
		return out

	doc_rate = pr_item_document_rate(
		voucher_no, item_code=item_code, pr_detail=voucher_detail_no
	)
	out["evidence"]["pr_document_rate"] = doc_rate
	if doc_rate is None:
		out["reasons"].append("pr_item_missing")
		return out
	if abs(flt(doc_rate)) > RATE_EPS:
		out["reasons"].append("pr_rate_nonzero")
		return out

	# Quantity must be a legitimate inbound
	qty = None
	if voucher_detail_no:
		qty = frappe.db.get_value("Purchase Receipt Item", voucher_detail_no, "qty")
	elif item_code:
		qty = frappe.db.sql(
			"""
			SELECT SUM(qty) FROM `tabPurchase Receipt Item`
			WHERE parent=%s AND item_code=%s
			""",
			(voucher_no, item_code),
		)[0][0]
	if actual_qty is not None:
		qty = actual_qty if qty is None else qty
	out["evidence"]["qty"] = flt(qty)
	if flt(qty) <= QTY_EPS:
		out["reasons"].append("qty_not_positive")
		return out

	pis = finalized_purchase_invoices_for_pr(
		voucher_no, item_code=item_code, pr_detail=voucher_detail_no
	)
	out["evidence"]["finalized_pi_n"] = len(pis)
	out["evidence"]["finalized_pi_names"] = [r.name for r in pis]
	if pis:
		# Any submitted PI for this receipt line → valuation is no longer pending.
		out["reasons"].append("purchase_invoice_finalized")
		return out

	# Draft / cancelled PI presence is OK — still pending finalization.
	draft_n = frappe.db.sql(
		"""
		SELECT COUNT(*) FROM `tabPurchase Invoice Item` pii
		INNER JOIN `tabPurchase Invoice` pi ON pi.name=pii.parent
		WHERE pii.purchase_receipt=%s AND pi.docstatus=0
		  AND (%s IS NULL OR pii.item_code=%s)
		  AND (%s IS NULL OR pii.pr_detail=%s)
		""",
		(voucher_no, item_code, item_code, voucher_detail_no, voucher_detail_no),
	)[0][0]
	out["evidence"]["draft_pi_n"] = int(draft_n)

	out["status"] = CLASS_PENDING_PURCHASE_INVOICE_VALUATION
	out["class"] = CLASS_PENDING_PURCHASE_INVOICE_VALUATION
	out["repair_disposition"] = LEGITIMATE_PENDING_UPSTREAM_ACCOUNTING
	out["pending"] = True
	out["reasons"].append("pr_zero_rate_pi_absent")
	out["message"] = (
		"Purchase Receipt accepted AS-IS with temporary zero rate; "
		"Purchase Invoice not yet posted — final purchase valuation pending. "
		"Do not invent a rate; do not amend PR; I3 remains enabled for unexplained SVD."
	)
	return out


def is_pending_purchase_invoice_valuation_sle(sle) -> bool:
	"""True when this SLE row is a pending-PI zero-rate Purchase Receipt inbound."""
	vt = _entry_get(sle, "voucher_type")
	if vt != "Purchase Receipt":
		return False
	qty = flt(_entry_get(sle, "actual_qty"))
	if qty <= QTY_EPS:
		return False
	res = classify_pending_purchase_invoice_valuation(
		voucher_type=vt,
		voucher_no=_entry_get(sle, "voucher_no"),
		item_code=_entry_get(sle, "item_code"),
		voucher_detail_no=_entry_get(sle, "voucher_detail_no"),
		actual_qty=qty,
	)
	return bool(res.get("pending"))


def honor_zero_inbound_for_pending_pi(voucher_type, voucher_detail_no) -> bool:
	"""ERPNext allow_zero override — PR detail only, strong classifier evidence."""
	if voucher_type != "Purchase Receipt" or not voucher_detail_no:
		return False
	row = frappe.db.get_value(
		"Purchase Receipt Item",
		voucher_detail_no,
		["parent", "item_code", "qty"],
		as_dict=True,
	)
	if not row:
		return False
	res = classify_pending_purchase_invoice_valuation(
		voucher_type="Purchase Receipt",
		voucher_no=row.parent,
		item_code=row.item_code,
		voucher_detail_no=voucher_detail_no,
		actual_qty=flt(row.qty),
	)
	return bool(res.get("pending"))


def align_pending_pi_sle_incoming_to_document_zero(sle) -> bool:
	"""Clear stale SLE incoming_rate to document PR rate 0 — never invent a rate.

	Returns True when alignment applied. Preserves pending-PI contract for MA:
	incoming_rate=0 ⇒ stock_value_change=0 ⇒ svd=0 (I3-safe).
	"""
	if not is_pending_purchase_invoice_valuation_sle(sle):
		return False
	# Document authority is rate=0. Stale positive incoming_rate is not authority.
	if hasattr(sle, "incoming_rate"):
		sle.incoming_rate = 0.0
	elif isinstance(sle, dict):
		sle["incoming_rate"] = 0.0
	return True


def apply_pending_pi_moving_average(engine, sle) -> bool:
	"""MA for pending-PI zero inbound: preserve warehouse stock_value, dilute rate.

	ERPNext ``get_moving_average_values`` uses ``qty * valuation_rate`` and drops
	any leftover ``stock_value`` (I4-style qty/value mismatch). With document
	incoming_rate=0 that leftover drop becomes a false negative incoming SVD (I3).

	Contract for PENDING_PURCHASE_INVOICE_VALUATION:
	  incoming_rate = 0 (never invent)
	  stock_value preserved (qty added, no value added)
	  stock_value_difference = 0 after process_sle assigns qty_after * valuation_rate
	"""
	if engine is None or sle is None:
		return False
	if not is_pending_purchase_invoice_valuation_sle(sle):
		return False
	actual_qty = flt(_entry_get(sle, "actual_qty"))
	if actual_qty <= QTY_EPS:
		return False

	align_pending_pi_sle_incoming_to_document_zero(sle)
	wh = getattr(engine, "wh_data", None)
	if wh is None:
		return False

	new_qty = flt(wh.qty_after_transaction) + actual_qty
	# valuation_rate so process_sle's stock_value = new_qty * rate equals prior stock_value
	if new_qty > QTY_EPS:
		wh.valuation_rate = flt(wh.stock_value) / new_qty
	else:
		wh.valuation_rate = 0.0
	return True


def apply_pending_pi_post_vanilla_economics(sle, engine=None) -> bool:
	"""After vanilla + IRR deterministic rounding: keep document-zero economics.

	``apply_irr_deterministic_sle_valuation`` may invent incoming_rate from
	prev_value/prev_qty even when SVD is already 0. Re-assert document zero
	and, if SVD drifted negative, restore value-preserving state.
	"""
	if not is_pending_purchase_invoice_valuation_sle(sle):
		return False

	align_pending_pi_sle_incoming_to_document_zero(sle)

	svd = flt(_entry_get(sle, "stock_value_difference"))
	qty_after = flt(_entry_get(sle, "qty_after_transaction"))
	actual_qty = flt(_entry_get(sle, "actual_qty"))
	stock_value = flt(_entry_get(sle, "stock_value"))

	# Already I3-safe zero inbound.
	if abs(svd) <= 1e-6:
		if hasattr(sle, "stock_value_difference"):
			sle.stock_value_difference = 0.0
		elif isinstance(sle, dict):
			sle["stock_value_difference"] = 0.0
		return True

	# Recover: restore prior warehouse value (stock_value - svd) as the post value.
	prev_value = stock_value - svd
	if hasattr(sle, "stock_value"):
		sle.stock_value = prev_value
		sle.stock_value_difference = 0.0
	elif isinstance(sle, dict):
		sle["stock_value"] = prev_value
		sle["stock_value_difference"] = 0.0

	if qty_after > QTY_EPS:
		rate = prev_value / qty_after
		if hasattr(sle, "valuation_rate"):
			sle.valuation_rate = rate
		elif isinstance(sle, dict):
			sle["valuation_rate"] = rate

	wh = getattr(engine, "wh_data", None) if engine is not None else None
	if wh is not None:
		wh.stock_value = prev_value
		wh.prev_stock_value = prev_value
		wh.qty_after_transaction = qty_after
		if qty_after > QTY_EPS:
			wh.valuation_rate = prev_value / qty_after
		# Keep prev_sle_dict pointer economics coherent for the next SLE.
		key = (_entry_get(sle, "item_code"), _entry_get(sle, "warehouse"))
		prev_dict = getattr(engine, "prev_sle_dict", None)
		if prev_dict is not None and key[0] and key[1]:
			prev_dict[key] = sle

	# silence unused in recovery path
	_ = actual_qty
	return True


def classify_i3_context(*, sle=None, detail: str | None = None, **_ctx) -> str:
	"""Distinguish unexplained I3 from pending-PI false-positive context.

	Does NOT suppress I3. Callers use this for messaging / HR disposition only.
	If negative SVD still occurs on a pending-PI row after alignment, treat as
	UNEXPLAINED (guard must still throw).
	"""
	if sle is not None and is_pending_purchase_invoice_valuation_sle(sle):
		return UNEXPLAINED_NEGATIVE_INCOMING_SVD
	return UNEXPLAINED_NEGATIVE_INCOMING_SVD


def debug_canary_pending_pi() -> dict:
	"""Dev probe for MAT-PRE-2026-00793-1 / 15010444 — not a hardcode gate."""
	from erpnext_extensions.iran_accounting.integration.bootstrap import apply as apply_iran

	apply_iran()
	import erpnext.stock.stock_ledger as sl

	cls = classify_pending_purchase_invoice_valuation(
		voucher_type="Purchase Receipt",
		voucher_no="MAT-PRE-2026-00793-1",
		item_code="15010444",
		voucher_detail_no="3ubbd27k8j",
		actual_qty=13,
	)
	sle = frappe.db.get_value(
		"Stock Ledger Entry",
		{"voucher_no": "MAT-PRE-2026-00793-1", "item_code": "15010444", "is_cancelled": 0},
		[
			"name",
			"voucher_type",
			"voucher_no",
			"voucher_detail_no",
			"item_code",
			"warehouse",
			"actual_qty",
			"incoming_rate",
			"stock_value_difference",
		],
		as_dict=True,
	)
	sle_d = frappe._dict(sle or {})
	pending = is_pending_purchase_invoice_valuation_sle(sle_d) if sle else False
	before = flt(sle_d.get("incoming_rate")) if sle else None
	aligned = align_pending_pi_sle_incoming_to_document_zero(sle_d) if sle else False
	# Probe allow_zero on a dummy engine instance
	eng = sl.update_entries_after.__new__(sl.update_entries_after)
	allow = sl.update_entries_after.check_if_allow_zero_valuation_rate(
		eng, "Purchase Receipt", "3ubbd27k8j"
	)
	return {
		"classify": cls,
		"patch": {
			"uea": bool(getattr(sl, "_iran_patched_update_entries_after", None)),
			"allow_zero": bool(getattr(sl.update_entries_after, "_iran_pending_pi_allow_zero", None)),
			"process_align": bool(
				getattr(sl.update_entries_after.process_sle, "_iran_pending_pi_align", None)
			),
			"dynamic_rate": bool(getattr(sl.update_entries_after, "_iran_pending_pi_dynamic_rate", None)),
			"ma": bool(getattr(sl.update_entries_after, "_iran_pending_pi_ma", None)),
		},
		"honor_zero": honor_zero_inbound_for_pending_pi("Purchase Receipt", "3ubbd27k8j"),
		"engine_allow_zero": cint(allow),
		"sle": {
			"name": sle_d.get("name"),
			"incoming_before": before,
			"is_pending": pending,
			"aligned": aligned,
			"incoming_after": flt(sle_d.get("incoming_rate")) if sle else None,
		},
	}


def run_canary_narrow_riv() -> dict:
	"""Regression canary: MAT-PRE-2026-00793-1 / 15010444 — not a hardcode gate."""
	from erpnext_extensions.iran_accounting.integration.bootstrap import apply as apply_iran
	from erpnext_extensions.iran_accounting.historical_stock.leftover_ma import create_and_run_narrow_riv

	apply_iran()
	cls = classify_pending_purchase_invoice_valuation(
		voucher_type="Purchase Receipt",
		voucher_no="MAT-PRE-2026-00793-1",
		item_code="15010444",
		voucher_detail_no="3ubbd27k8j",
		actual_qty=13,
	)
	sle = frappe.db.get_value(
		"Stock Ledger Entry",
		{"voucher_no": "MAT-PRE-2026-00793-1", "item_code": "15010444", "is_cancelled": 0},
		["warehouse", "posting_date", "posting_time", "incoming_rate", "stock_value_difference", "stock_value", "valuation_rate"],
		as_dict=True,
	)
	riv = create_and_run_narrow_riv(
		"15010444",
		sle.warehouse,
		posting_date=str(sle.posting_date),
		posting_time=str(sle.posting_time),
	)
	sle_after = frappe.db.get_value(
		"Stock Ledger Entry",
		{"voucher_no": "MAT-PRE-2026-00793-1", "item_code": "15010444", "is_cancelled": 0},
		["incoming_rate", "stock_value_difference", "stock_value", "valuation_rate", "qty_after_transaction"],
		as_dict=True,
	)
	pr_rate = pr_item_document_rate("MAT-PRE-2026-00793-1", item_code="15010444", pr_detail="3ubbd27k8j")
	return {
		"classify": cls,
		"riv": riv,
		"pr_document_rate": pr_rate,
		"sle_before": sle,
		"sle_after": sle_after,
		"pr_mutated": abs(flt(pr_rate or 0)) > RATE_EPS,
	}

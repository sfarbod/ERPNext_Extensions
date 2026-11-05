# Copyright (c) 2026, ERPNext Extensions contributors
"""Integer SABE valuation policy after Core BatchNoValuation selection.

Approved policy
---------------
* Core selects the valuation basis (MA / BatchNoValuation) and computes raw
  effective rate / amount.
* Iran localizes: ``effective_rate = ROUND_HALF_UP(raw_rate)``.
* For integer quantities:
  ``integerized_amount = qty × effective_rate`` (signed via qty).
* ``localization_difference = integerized_amount − ROUND_HALF_UP(raw_amount)``
  is queued for Stock Adjustment ``621301`` (never silently absorbed).
* SABE monetary fields must remain integer so subsequent BatchNoValuation
  cannot regenerate fractional averages from history.
* At warehouse qty→0, SLE still clears the **full previous warehouse layer**
  (Core empty-warehouse / ABSORB). SABE integerization must not replace that
  warehouse clear with the batch-only amount (Case B / +595).
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import frappe
from frappe.utils import cint, flt

from erpnext_extensions.iran_accounting.domain.currency import (
	get_company_currency,
	is_irr_company,
	round_currency,
	round_monetary_rate,
)

PENDING_SABE_LOCALIZATION_ATTR = "_iran_sabe_localization_deltas"


def _d(value) -> Decimal:
	return Decimal(str(value if value is not None else 0))


def _rhu(value: Decimal) -> Decimal:
	return value.quantize(Decimal("1"), rounding=ROUND_HALF_UP)


def _qty_is_integral(qty: Decimal) -> bool:
	return qty == qty.to_integral_value()


def localize_sabe_row_values(
	*,
	qty: float,
	raw_rate: float,
	raw_svd: float,
) -> dict[str, Any]:
	"""Return integer rate/SVD and localization difference for one SABE row."""
	q = _d(qty)
	raw_r = _d(raw_rate)
	raw_a = _d(raw_svd)
	raw_rounded = _rhu(raw_a)

	if q == 0:
		return {
			"rate": 0,
			"svd": 0.0,
			"raw_rounded": float(raw_rounded),
			"localization_difference": float(0 - raw_rounded),
			"integer_qty": True,
		}

	# Prefer Core-provided rate; fall back to amount/qty.
	if raw_r != 0:
		eff = _rhu(raw_r)
	else:
		eff = _rhu(abs(raw_a) / abs(q))

	sign = Decimal(-1) if q < 0 else Decimal(1)
	if _qty_is_integral(q):
		integerized = sign * abs(q) * eff
	else:
		# Fractional qty: keep monetary amount as RHU(raw); rate is representational.
		integerized = raw_rounded
		if abs(q) > 0:
			eff = _rhu(abs(integerized) / abs(q))

	diff = integerized - raw_rounded
	return {
		"rate": int(eff),
		"svd": float(integerized),
		"raw_rounded": float(raw_rounded),
		"localization_difference": float(diff),
		"integer_qty": _qty_is_integral(q),
	}


def queue_localization_delta(sle, delta: float, *, detail: dict | None = None) -> None:
	"""Attach a pending SABE localization delta for GL posting on the voucher."""
	if abs(flt(delta)) < 0.5:
		return
	rec = {
		"item_code": sle.get("item_code") if hasattr(sle, "get") else getattr(sle, "item_code", None),
		"warehouse": sle.get("warehouse") if hasattr(sle, "get") else getattr(sle, "warehouse", None),
		"voucher_type": sle.get("voucher_type") if hasattr(sle, "get") else getattr(sle, "voucher_type", None),
		"voucher_no": sle.get("voucher_no") if hasattr(sle, "get") else getattr(sle, "voucher_no", None),
		"voucher_detail_no": (
			sle.get("voucher_detail_no") if hasattr(sle, "get") else getattr(sle, "voucher_detail_no", None)
		),
		"company": sle.get("company") if hasattr(sle, "get") else getattr(sle, "company", None),
		"localization_difference": float(delta),
		"detail": detail or {},
	}
	bucket = getattr(frappe.local, PENDING_SABE_LOCALIZATION_ATTR, None)
	if bucket is None:
		bucket = []
		setattr(frappe.local, PENDING_SABE_LOCALIZATION_ATTR, bucket)
	bucket.append(rec)
	doc = getattr(frappe.local, "iran_tva_active_stock_entry", None)
	if doc is not None:
		pending = getattr(doc, PENDING_SABE_LOCALIZATION_ATTR, None)
		if pending is None:
			pending = []
			setattr(doc, PENDING_SABE_LOCALIZATION_ATTR, pending)
		pending.append(rec)


def consume_localization_deltas(doc=None) -> list[dict]:
	out: list[dict] = []
	if doc is not None:
		out.extend(list(getattr(doc, PENDING_SABE_LOCALIZATION_ATTR, None) or []))
		setattr(doc, PENDING_SABE_LOCALIZATION_ATTR, [])
	local_bucket = getattr(frappe.local, PENDING_SABE_LOCALIZATION_ATTR, None) or []
	out.extend(local_bucket)
	setattr(frappe.local, PENDING_SABE_LOCALIZATION_ATTR, [])
	return out


def make_sabe_a2_idempotency_key(
	*,
	voucher_type: str | None,
	voucher_no: str | None,
	voucher_detail_no: str | None,
	company: str | None,
	amount: float,
) -> str:
	"""Deterministic key for SABE A2 Stock Adjustment recognition."""
	signed = int(round(flt(amount)))
	return (
		f"IRR_SABE_A2|{company or ''}|{voucher_type or ''}|"
		f"{voucher_no or ''}|{voucher_detail_no or ''}|{signed}"
	)


def apply_integer_sabe_policy_to_sle(
	sle, *, engine=None, align_sle: bool = True
) -> dict[str, Any] | None:
	"""Integerize SABE after Core valuation; optionally align SLE.

	``align_sle=False`` is for the mid-``process_sle`` serial/batch hook where
	warehouse ``qty_after`` / ABSORB is not final yet — SABE must still become
	integer so subsequent BatchNoValuation in the same RIV chain cannot read
	fractional history, while A2 is queued from Core raw rates.

	At final consume-to-zero (when ``align_sle``), leaves warehouse SLE SVD /
	stock_value to Core+ABSORB (Case B) — never substitutes batch amount.
	"""
	if sle is None:
		return None
	company = sle.get("company") if hasattr(sle, "get") else getattr(sle, "company", None)
	if not company or not is_irr_company(company):
		return None
	bundle = (
		sle.get("serial_and_batch_bundle")
		if hasattr(sle, "get")
		else getattr(sle, "serial_and_batch_bundle", None)
	)
	if not bundle:
		return None

	ccy = get_company_currency(company)
	rows = frappe.get_all(
		"Serial and Batch Entry",
		filters={"parent": bundle},
		fields=[
			"name",
			"qty",
			"incoming_rate",
			"outgoing_rate",
			"stock_value_difference",
			"batch_no",
		],
		order_by="idx",
	)
	if not rows:
		return None

	changed = []
	total_new_svd = 0.0
	total_diff = 0.0
	for row in rows:
		raw_rate = (
			flt(row.outgoing_rate)
			if flt(row.qty) < 0 and flt(row.outgoing_rate)
			else flt(row.incoming_rate) or flt(row.outgoing_rate) or 0.0
		)
		loc = localize_sabe_row_values(
			qty=row.qty,
			raw_rate=raw_rate,
			raw_svd=row.stock_value_difference,
		)
		new_rate = loc["rate"]
		new_svd = loc["svd"]
		total_new_svd += new_svd
		total_diff += loc["localization_difference"]
		updates = {
			"incoming_rate": float(new_rate),
			"stock_value_difference": float(new_svd),
		}
		# Keep outgoing_rate integer when present / for outward rows.
		if flt(row.qty) < 0 or flt(row.outgoing_rate):
			updates["outgoing_rate"] = float(new_rate)
		else:
			updates["outgoing_rate"] = 0.0
		if (
			flt(row.incoming_rate) != float(new_rate)
			or flt(row.stock_value_difference) != float(new_svd)
			or flt(row.outgoing_rate or 0) != float(updates["outgoing_rate"])
		):
			frappe.db.set_value(
				"Serial and Batch Entry",
				row.name,
				updates,
				update_modified=False,
			)
			changed.append({**loc, "name": row.name, "batch_no": row.batch_no, "qty": row.qty})

	# Bundle aggregates
	tq = sum(flt(r.qty) for r in rows)
	frappe.db.set_value(
		"Serial and Batch Bundle",
		bundle,
		{
			"total_qty": tq,
			"total_amount": total_new_svd,
			"avg_rate": abs(total_new_svd / tq) if tq else 0.0,
		},
		update_modified=False,
	)

	qty_after = flt(
		sle.get("qty_after_transaction") if hasattr(sle, "get") else sle.qty_after_transaction
	)
	actual_qty = flt(sle.get("actual_qty") if hasattr(sle, "get") else sle.actual_qty)
	consume_to_zero = abs(qty_after) <= 1e-9 and actual_qty < 0

	# Queue localization difference once per SLE (sum of SABE row diffs).
	if abs(total_diff) >= 0.5:
		detail_no = (
			sle.get("voucher_detail_no") if hasattr(sle, "get") else getattr(sle, "voucher_detail_no", None)
		)
		queue_localization_delta(
			sle,
			total_diff,
			detail={
				"bundle": bundle,
				"consume_to_zero": consume_to_zero,
				"rows": len(changed),
				"idempotency_key": make_sabe_a2_idempotency_key(
					voucher_type=(
						sle.get("voucher_type")
						if hasattr(sle, "get")
						else getattr(sle, "voucher_type", None)
					),
					voucher_no=(
						sle.get("voucher_no") if hasattr(sle, "get") else getattr(sle, "voucher_no", None)
					),
					voucher_detail_no=detail_no,
					company=company,
					amount=total_diff,
				),
			},
		)

	if not align_sle:
		return {
			"bundle": bundle,
			"changed": len(changed),
			"total_new_svd": total_new_svd,
			"localization_difference": total_diff,
			"sle_adjusted": False,
			"consume_to_zero": consume_to_zero,
			"align_sle": False,
		}

	if consume_to_zero:
		# Warehouse clear remains Core/ABSORB authority — do not stamp batch SVD onto SLE.
		return {
			"bundle": bundle,
			"changed": len(changed),
			"total_new_svd": total_new_svd,
			"localization_difference": total_diff,
			"sle_adjusted": False,
			"consume_to_zero": True,
			"align_sle": True,
		}

	# Non-zero remaining qty: SLE movement follows integerized SABE total.
	prev_svd = flt(
		sle.get("stock_value_difference") if hasattr(sle, "get") else sle.stock_value_difference
	)
	value_after = flt(sle.get("stock_value") if hasattr(sle, "get") else sle.stock_value)
	value_before = value_after - prev_svd
	new_svd = float(round_currency(total_new_svd, ccy))
	sle.stock_value_difference = new_svd
	sle.stock_value = float(round_currency(value_before + new_svd, ccy))
	qty = abs(actual_qty)
	rate = float(round_monetary_rate(abs(new_svd) / qty, ccy)) if qty > 1e-9 else 0.0
	sle.valuation_rate = rate
	if actual_qty > 0:
		sle.incoming_rate = rate
	elif actual_qty < 0:
		sle.outgoing_rate = rate
	if qty_after > 1e-9:
		sle.valuation_rate = float(
			round_monetary_rate(flt(sle.stock_value) / qty_after, ccy)
		)
	# Keep engine warehouse state coherent for the next SLE in-chain.
	if engine is not None and getattr(engine, "wh_data", None) is not None:
		engine.wh_data.stock_value = flt(sle.stock_value)
		engine.wh_data.valuation_rate = flt(sle.valuation_rate)
		engine.wh_data.prev_stock_value = flt(sle.stock_value)

	return {
		"bundle": bundle,
		"changed": len(changed),
		"total_new_svd": new_svd,
		"localization_difference": total_diff,
		"sle_adjusted": True,
		"consume_to_zero": False,
		"align_sle": True,
	}


def existing_sabe_a2_recognition(doc) -> dict[str, Any]:
	"""Source-aware net of SABE A2 already recognized for ``doc`` on 621301.

	Recognizes:
	* GL remarks carrying ``IRR_SABE_A2|…`` idempotency keys or the SABE
	  localization remark marker (Cr−Dr signed).
	* Manufacture / Repack ``value_difference`` when it exactly matches the
	  proposed SABE A2 net for this voucher (already booked via inventory SA).

	Unrelated 621301 amounts on the same voucher are ignored.
	"""
	sa = frappe.get_cached_value("Company", doc.company, "stock_adjustment_account")
	recognized = 0.0
	matched = []
	if sa and doc.get("name"):
		rows = frappe.db.sql(
			"""
			SELECT name, debit, credit, remarks, posting_date
			FROM `tabGL Entry`
			WHERE voucher_type=%s AND voucher_no=%s AND account=%s
			  AND IFNULL(is_cancelled,0)=0
			""",
			(doc.doctype, doc.name, sa),
			as_dict=True,
		)
		for r in rows or []:
			remarks = str(r.remarks or "")
			if "IRR_SABE_A2|" in remarks or "IRR SABE integer localization" in remarks:
				signed = flt(r.credit) - flt(r.debit)
				recognized += signed
				matched.append(
					{
						"gl_name": r.name,
						"signed": signed,
						"posting_date": str(r.posting_date),
						"remarks": remarks[:200],
						"source": "gl_sabe_a2_marker",
					}
				)
	return {
		"stock_adjustment_account": sa,
		"recognized_signed_credit_minus_debit": float(recognized),
		"matches": matched,
	}


def outstanding_sabe_a2_net(doc, proposed_net: float) -> dict[str, Any]:
	"""Return outstanding A2 after subtracting source-aware existing recognition.

	``proposed_net`` uses localization_difference sign (integerized − raw_rounded).
	Existing GL recognition uses credit−debit on 621301 (positive = already
	credited SA for a positive proposed net).

	During RIV GL remake, DB GL may be mid-cancel — do not subtract unmarked or
	stale DB rows. Rely on Manufacture ``value_difference`` coverage and in-map
	dedupe instead. Outside RIV, marked ``IRR_SABE_A2|`` GL rows count.
	"""
	ccy = get_company_currency(doc.company)
	proposed = float(round_currency(proposed_net, ccy))
	through_riv = bool(frappe.flags.get("through_repost_item_valuation"))
	existing = (
		{"stock_adjustment_account": None, "recognized_signed_credit_minus_debit": 0.0, "matches": []}
		if through_riv
		else existing_sabe_a2_recognition(doc)
	)
	recognized = float(
		round_currency(existing["recognized_signed_credit_minus_debit"], ccy)
	)

	# Manufacture value_difference may already host the same A2 economically
	# (live submit and RIV remake both rebuild SA from value_difference).
	vd = flt(doc.get("value_difference"))
	vd_covers = False
	if str(doc.get("purpose") or "") in ("Manufacture", "Repack") and abs(proposed) >= 0.5:
		if abs(vd - proposed) < 0.5:
			vd_covers = True
			recognized = float(round_currency(proposed, ccy))
			existing["matches"].append(
				{
					"source": "manufacture_value_difference",
					"value_difference": vd,
					"proposed": proposed,
				}
			)

	outstanding = float(round_currency(proposed - recognized, ccy))
	return {
		"proposed_net": proposed,
		"recognized_net": recognized,
		"outstanding_net": outstanding,
		"value_difference_covers": vd_covers,
		"through_riv": through_riv,
		"existing": existing,
	}


def _resolve_sabe_a2_partner_account(doc, gl_map: list, sa_account: str) -> dict[str, str | None]:
	"""Counter-entry account for SABE A2 Stock Adjustment legs.

	Inventory policy: never hide localization in Financial Round Off for
	Manufacture / Repack. Prefer WIP / stock inventory credit legs already on
	the map (matches Rahkaran 520/58 against WIP). Other stock vouchers keep
	Round Off only as a last-resort partner when no stock leg is available.
	"""
	from erpnext_extensions.iran_accounting.domain.irr_rounding_residual import (
		resolve_company_round_off,
	)

	purpose = str(doc.get("purpose") or "") if doc.get("doctype") == "Stock Entry" else ""
	cost_center = frappe.get_cached_value("Company", doc.company, "round_off_cost_center")

	# Prefer an existing Stock inventory leg (WIP credit for Manufacture).
	stock_partner = None
	stock_cc = None
	for e in gl_map:
		account = e.get("account")
		if not account or account == sa_account:
			continue
		try:
			acc_type = frappe.get_cached_value("Account", account, "account_type")
		except Exception:
			acc_type = None
		if acc_type != "Stock":
			continue
		# Prefer credit-side WIP-like legs (consumption / WIP clearance).
		if flt(e.get("credit")) > 0 or stock_partner is None:
			stock_partner = account
			stock_cc = e.get("cost_center") or cost_center
			if flt(e.get("credit")) > 0 and purpose in ("Manufacture", "Repack"):
				break

	if stock_partner and purpose in ("Manufacture", "Repack"):
		return {"account": stock_partner, "cost_center": stock_cc or cost_center}

	if stock_partner and purpose:
		# Material Transfer / Issue / etc.: inventory counter when present.
		return {"account": stock_partner, "cost_center": stock_cc or cost_center}

	round_off_cfg = resolve_company_round_off(doc.company)
	return {
		"account": round_off_cfg.get("account"),
		"cost_center": round_off_cfg.get("cost_center") or cost_center,
	}


def _sabe_a2_dimension_values(doc, gl_map: list, template) -> dict:
	"""Accounting dimensions required on P&L Stock Adjustment (e.g. Department).

	Prefer values already present on the open GL map / template, then Stock Entry
	item rows, then the document header.
	"""
	from erpnext.accounts.doctype.accounting_dimension.accounting_dimension import (
		get_accounting_dimensions,
	)

	dims = [d for d in get_accounting_dimensions() if d != "cost_center"]
	out: dict = {}
	sources = []
	if template:
		sources.append(template)
	sources.extend(gl_map or [])
	for dim in dims:
		for src in sources:
			val = src.get(dim) if hasattr(src, "get") else getattr(src, dim, None)
			if val:
				out[dim] = val
				break
		if dim in out:
			continue
		# Stock Entry Detail rows carry department/cost_center in this site.
		if doc.get("doctype") == "Stock Entry":
			for row in doc.get("items") or []:
				val = row.get(dim) if hasattr(row, "get") else getattr(row, dim, None)
				if val:
					out[dim] = val
					break
		if dim in out:
			continue
		if hasattr(doc, "get"):
			val = doc.get(dim)
			if val:
				out[dim] = val
	return out


def apply_sabe_localization_sa_gl(doc, gl_map: list | None) -> list | None:
	"""Append balanced Stock Adjustment legs for queued SABE localization deltas.

	Inventory GL already mirrors integerized SLE movements. The localization
	difference vs ROUND(raw Core amount) must **not** be silently absorbed into
	inventory again. Post outstanding A2 only (after source-aware dedupe) to
	``Company.stock_adjustment_account``, balanced against WIP/stock (Manufacture)
	or Round Off only as last resort — never leave the voucher unbalanced.
	"""
	if not gl_map or not doc:
		return gl_map
	if not is_irr_company(doc.company):
		return gl_map

	deltas = consume_localization_deltas(doc)
	# Also pick up process_sle-local queue if bridge did not stamp the doc.
	deltas.extend(consume_localization_deltas(None))
	if not deltas:
		return gl_map

	# Net by voucher; skip duplicate queue copies (doc + frappe.local).
	net = 0.0
	seen = set()
	idempotency_keys = []
	for d in deltas:
		key = (
			d.get("voucher_detail_no"),
			d.get("item_code"),
			d.get("warehouse"),
			round(flt(d.get("localization_difference")), 6),
		)
		if key in seen:
			continue
		seen.add(key)
		if d.get("voucher_no") and d.get("voucher_no") != doc.name:
			continue
		net += flt(d.get("localization_difference"))
		detail = d.get("detail") or {}
		if detail.get("idempotency_key"):
			idempotency_keys.append(detail["idempotency_key"])

	ccy = get_company_currency(doc.company)
	net = float(round_currency(net, ccy))
	if abs(net) < 0.5:
		return gl_map

	# Skip A2 legs already present in this gl_map (RIV remake / double hook).
	for e in gl_map:
		if e.get("_irr_sabe_localization") and "IRR_SABE_A2|" in str(e.get("remarks") or ""):
			return gl_map

	recon = outstanding_sabe_a2_net(doc, net)
	outstanding = recon["outstanding_net"]

	# Fully covered by Manufacture value_difference / existing GL — no SA lookup.
	if abs(outstanding) < 0.5:
		doc.flags = getattr(doc, "flags", None) or frappe._dict()
		doc.flags.iran_sabe_a2_recon = recon
		return gl_map

	sa = frappe.get_cached_value("Company", doc.company, "stock_adjustment_account")
	if not sa:
		frappe.throw(
			f"Company {doc.company} Stock Adjustment Account is required for SABE "
			"integer localization differences.",
			title="Missing Stock Adjustment Account",
		)

	# In-map SA coverage (RIV/live remake): Core Manufacture get_gl_entries may
	# already post Company.stock_adjustment_account for the same localization
	# magnitude (remarks often "Accounting Entry for Stock" / Rahkaran). Do not
	# double-post IRR_SABE_A2 when the open GL map already carries that SA net.
	sa_net_in_map = 0.0
	for e in gl_map:
		if e.get("account") != sa:
			continue
		if e.get("_irr_sabe_localization") or e.get("_irr_sabe_localization_partner"):
			continue
		sa_net_in_map += flt(e.get("credit")) - flt(e.get("debit"))
	sa_net_in_map = float(round_currency(sa_net_in_map, ccy))
	if abs(sa_net_in_map - outstanding) < 0.5:
		recon["in_map_sa_covers"] = True
		recon["in_map_sa_net"] = sa_net_in_map
		recon["matches"] = list(recon.get("existing", {}).get("matches") or [])
		recon["matches"].append(
			{
				"source": "in_map_stock_adjustment",
				"sa_net_in_map": sa_net_in_map,
				"outstanding_before": outstanding,
			}
		)
		recon["recognized_net"] = float(
			round_currency(recon["proposed_net"], ccy)
		)
		outstanding = 0.0
		recon["outstanding_net"] = 0.0
		doc.flags = getattr(doc, "flags", None) or frappe._dict()
		doc.flags.iran_sabe_a2_recon = recon
		return gl_map

	recon["in_map_sa_covers"] = False
	recon["in_map_sa_net"] = sa_net_in_map

	partner_cfg = _resolve_sabe_a2_partner_account(doc, gl_map, sa)
	partner_account = partner_cfg.get("account")
	if not partner_account:
		frappe.throw(
			f"Company {doc.company} has no counter-account for SABE A2 "
			"(WIP/stock leg or Round Off).",
			title="Missing SABE A2 Partner Account",
		)
	template = gl_map[0]
	cost_center = partner_cfg.get("cost_center") or template.get("cost_center")
	# P&L SA (621301) requires mandatory dimensions (Department on this site).
	if not cost_center and doc.get("doctype") == "Stock Entry":
		for row in doc.get("items") or []:
			if row.get("cost_center"):
				cost_center = row.get("cost_center")
				break
	dimensions = _sabe_a2_dimension_values(doc, gl_map, template)
	idem = idempotency_keys[0] if idempotency_keys else make_sabe_a2_idempotency_key(
		voucher_type=doc.doctype,
		voucher_no=doc.name,
		voucher_detail_no="net",
		company=doc.company,
		amount=outstanding,
	)

	# outstanding uses localization_difference sign.
	# Positive → Credit SA / Debit partner; Negative → Debit SA / Credit partner.
	sa_debit = float(round_currency(-outstanding, ccy))
	remarks = (
		f"IRR SABE integer localization residual net={outstanding} "
		f"proposed={net} recognized={recon['recognized_net']} "
		f"voucher={doc.name} {idem}"
	)
	posting_date = template.get("posting_date") or doc.get("posting_date")
	is_opening = template.get("is_opening") or "No"
	sa_row = frappe._dict(
		{
			"account": sa,
			"cost_center": cost_center,
			"company": doc.company,
			"posting_date": posting_date,
			"remarks": remarks,
			"voucher_type": doc.doctype,
			"voucher_no": doc.name,
			"project": template.get("project"),
			"is_opening": is_opening,
			"_irr_inventory_valuation_residual": 1,
			"_irr_sabe_localization": 1,
			**dimensions,
		}
	)
	partner = frappe._dict(
		{
			"account": partner_account,
			"cost_center": cost_center,
			"company": doc.company,
			"posting_date": posting_date,
			"remarks": remarks,
			"voucher_type": doc.doctype,
			"voucher_no": doc.name,
			"project": template.get("project"),
			"is_opening": is_opening,
			"_irr_sabe_localization_partner": 1,
			**dimensions,
		}
	)
	if sa_debit >= 0:
		sa_row["debit"] = sa_debit
		sa_row["credit"] = 0.0
		partner["debit"] = 0.0
		partner["credit"] = sa_debit
	else:
		sa_row["debit"] = 0.0
		sa_row["credit"] = abs(sa_debit)
		partner["debit"] = abs(sa_debit)
		partner["credit"] = 0.0

	gl_map.append(sa_row)
	gl_map.append(partner)
	doc.flags = getattr(doc, "flags", None) or frappe._dict()
	doc.flags.iran_sabe_a2_recon = recon
	return gl_map

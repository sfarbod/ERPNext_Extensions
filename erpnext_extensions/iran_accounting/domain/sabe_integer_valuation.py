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


def core_stock_adjustment_net_in_map(gl_map: list | None, sa_account: str) -> float:
	"""Credit−debit of Core (non-A2) Stock Adjustment legs already on the map."""
	net = 0.0
	for e in gl_map or []:
		if (e.get("account") if hasattr(e, "get") else getattr(e, "account", None)) != sa_account:
			continue
		if e.get("_irr_sabe_localization") or e.get("_irr_sabe_localization_partner"):
			continue
		remarks = str(e.get("remarks") or "")
		if "IRR_SABE_A2|" in remarks or "IRR SABE integer localization" in remarks:
			continue
		if "IRR_SA_AGG|" in remarks and "iran_a2" in remarks:
			# Aggregated row that already mixed A2 — treat whole as non-core for coverage.
			continue
		net += flt(e.get("credit")) - flt(e.get("debit"))
	return net


def reconcile_final_stock_adjustment_net(
	doc, gl_map: list | None, proposed_a2: float
) -> dict[str, Any]:
	"""Reconcile Core SA and intermediate A2 into the final SA net to keep.

	Sign convention (credit-positive): ``credit − debit`` on Stock Adjustment.

	Formula
	-------
	``gross = core_sa_net + proposed_a2``
	``final_sa_net = core_sa_net`` when Core already posted SA (A2 is intermediate
	and must not inflate the plug): equivalently
	``final_sa_net = gross + a2_recon_effect`` with ``a2_recon_effect = −proposed_a2``.

	When no Core SA exists: ``final_sa_net = proposed_a2`` (A2 becomes the plug).

	``outstanding_to_post = final_sa_net − core_sa_net``
	→ 0 whenever Core SA is present; → ``proposed_a2`` when Core SA is absent.

	Also honors exact Manufacture ``value_difference`` match and previously
	posted ``IRR_SABE_A2|`` recognition (remake / amend safety).
	"""
	ccy = get_company_currency(doc.company)
	proposed = float(round_currency(proposed_a2, ccy))
	sa = frappe.get_cached_value("Company", doc.company, "stock_adjustment_account")
	core_sa = float(round_currency(core_stock_adjustment_net_in_map(gl_map, sa), ccy)) if sa else 0.0

	through_riv = bool(frappe.flags.get("through_repost_item_valuation"))
	existing = (
		{"stock_adjustment_account": sa, "recognized_signed_credit_minus_debit": 0.0, "matches": []}
		if through_riv
		else existing_sabe_a2_recognition(doc)
	)
	prior_a2_gl = float(
		round_currency(existing["recognized_signed_credit_minus_debit"], ccy)
	)

	vd = flt(doc.get("value_difference"))
	vd_covers = False
	purpose = str(doc.get("purpose") or "")
	if purpose in ("Manufacture", "Repack") and abs(proposed) >= 0.5:
		if abs(vd - proposed) < 0.5:
			vd_covers = True
			existing["matches"].append(
				{
					"source": "manufacture_value_difference",
					"value_difference": vd,
					"proposed": proposed,
				}
			)

	# Core SA / exact Manufacture value_difference → final plug; eliminate A2.
	if abs(core_sa) >= 0.5:
		final_sa = core_sa
		a2_recon_effect = float(round_currency(-proposed, ccy)) if abs(proposed) >= 0.5 else 0.0
		coverage_source = "core_stock_adjustment"
		outstanding = 0.0
	elif vd_covers:
		# Core will (or already did) recognize A2 via value_difference — do not post A2.
		final_sa = float(round_currency(vd, ccy))
		a2_recon_effect = float(round_currency(-proposed, ccy)) if abs(proposed) >= 0.5 else 0.0
		coverage_source = "manufacture_value_difference"
		outstanding = 0.0
	elif abs(proposed) >= 0.5:
		final_sa = proposed
		a2_recon_effect = 0.0
		coverage_source = "a2_becomes_stock_adjustment"
		outstanding = float(round_currency(final_sa - core_sa, ccy))
	else:
		final_sa = 0.0
		a2_recon_effect = 0.0
		coverage_source = "noop"
		outstanding = 0.0

	# Prior marked A2 GL (non-RIV) already on voucher reduces further posting.
	if abs(prior_a2_gl) >= 0.5 and abs(outstanding) >= 0.5:
		outstanding = float(round_currency(outstanding - prior_a2_gl, ccy))

	return {
		"proposed_a2": proposed,
		"proposed_net": proposed,  # backward-compatible key
		"core_sa_net": core_sa,
		"final_sa_net": final_sa,
		"a2_recon_effect": a2_recon_effect,
		"gross_sa_before_recon": float(round_currency(core_sa + proposed, ccy)),
		"outstanding_to_post": outstanding,
		"outstanding_net": outstanding,  # backward-compatible
		"recognized_net": float(round_currency(proposed - outstanding, ccy)),
		"value_difference_covers": vd_covers,
		"coverage_source": coverage_source,
		"through_riv": through_riv,
		"prior_a2_gl": prior_a2_gl,
		"existing": existing,
		"formula": (
			"final_sa_net = core_sa_net when Core SA present "
			"(a2_recon_effect = -proposed_a2); else final_sa_net = proposed_a2; "
			"outstanding_to_post = final_sa_net - core_sa_net"
		),
	}


def outstanding_sabe_a2_net(doc, proposed_net: float, gl_map: list | None = None) -> dict[str, Any]:
	"""Backward-compatible wrapper around ``reconcile_final_stock_adjustment_net``.

	When ``gl_map`` is omitted, load active GL for Core SA coverage (campaign /
	amend preview). During RIV remake the open ``gl_map`` is passed by the hook.
	"""
	if gl_map is None and doc and doc.get("name") and not frappe.flags.get("through_repost_item_valuation"):
		try:
			if frappe.db.exists(doc.doctype, doc.name):
				rows = frappe.db.sql(
					"""
					SELECT account, debit, credit, remarks
					FROM `tabGL Entry`
					WHERE voucher_type=%s AND voucher_no=%s
					  AND IFNULL(is_cancelled, 0)=0
					""",
					(doc.doctype, doc.name),
					as_dict=True,
				)
				gl_map = rows or []
		except Exception:
			gl_map = gl_map
	return reconcile_final_stock_adjustment_net(doc, gl_map, proposed_net)


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
	"""Reconcile A2 into the final Stock Adjustment net — no artificial counters.

	A2 (``localization_difference``) is an intermediate valuation component.
	Core ``get_gl_entries`` already posts ``Company.stock_adjustment_account``
	when Manufacture/Repack ``value_difference`` (or other Core SA) is the
	authoritative plug. In that case A2 must **not** add SA turnover or any
	WIP / Round Off / inventory counter-entry.

	Only when Core has no SA and an outstanding net remains may a single
	one-sided SA leg be appended — and only if the open map's debit−credit
	gap already equals that outstanding (so no partner is required).
	"""
	if not gl_map or not doc:
		return gl_map
	if not is_irr_company(doc.company):
		return gl_map

	deltas = consume_localization_deltas(doc)
	deltas.extend(consume_localization_deltas(None))
	if not deltas:
		return gl_map

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

	# Skip if this map already carries an A2-marked SA leg (RIV remake / double hook).
	for e in gl_map:
		if e.get("_irr_sabe_localization") and "IRR_SABE_A2|" in str(e.get("remarks") or ""):
			return gl_map

	recon = reconcile_final_stock_adjustment_net(doc, gl_map, net)
	outstanding = recon["outstanding_to_post"]
	recon["in_map_sa_net"] = recon["core_sa_net"]
	recon["in_map_sa_covers"] = abs(recon["core_sa_net"]) >= 0.5 and abs(outstanding) < 0.5

	doc.flags = getattr(doc, "flags", None) or frappe._dict()
	doc.flags.iran_sabe_a2_recon = recon

	# Final SA already equals Core SA (A2 eliminated) — nothing to post.
	if abs(outstanding) < 0.5:
		return gl_map

	sa = frappe.get_cached_value("Company", doc.company, "stock_adjustment_account")
	if not sa:
		frappe.throw(
			f"Company {doc.company} Stock Adjustment Account is required for SABE "
			"integer localization differences.",
			title="Missing Stock Adjustment Account",
		)

	# Single-sided SA append only when the open map already needs that plug
	# (debit−credit gap == outstanding as credit-positive SA). No partner.
	map_delta = sum(flt(e.get("debit")) - flt(e.get("credit")) for e in gl_map)
	map_delta = float(round_currency(map_delta, ccy))
	# outstanding > 0 means we need SA credit of outstanding → map must have excess debit.
	if abs(map_delta - outstanding) >= 0.5:
		recon["skipped_unbalanced_without_partner"] = True
		recon["map_delta"] = map_delta
		doc.flags.iran_sabe_a2_recon = recon
		return gl_map

	from erpnext_extensions.iran_accounting.domain.stock_adjustment_gl_aggregate import (
		align_sabe_a2_sa_dimensions_with_in_map,
	)

	template = gl_map[0]
	sa_dims_prefer = align_sabe_a2_sa_dimensions_with_in_map(doc, gl_map, sa)
	sa_cost_center = (
		sa_dims_prefer.get("cost_center")
		or template.get("cost_center")
	)
	if not sa_cost_center and doc.get("doctype") == "Stock Entry":
		for row in doc.get("items") or []:
			if row.get("cost_center"):
				sa_cost_center = row.get("cost_center")
				break
	dimensions = _sabe_a2_dimension_values(doc, gl_map, template)
	for key, val in sa_dims_prefer.items():
		if key == "cost_center" or not val:
			continue
		dimensions[key] = val
	idem = idempotency_keys[0] if idempotency_keys else make_sabe_a2_idempotency_key(
		voucher_type=doc.doctype,
		voucher_no=doc.name,
		voucher_detail_no="net",
		company=doc.company,
		amount=outstanding,
	)
	remarks = (
		f"IRR SABE net stock adjustment outstanding={outstanding} "
		f"proposed_a2={net} final_sa={recon['final_sa_net']} "
		f"core_sa={recon['core_sa_net']} a2_recon_effect={recon['a2_recon_effect']} "
		f"voucher={doc.name} {idem}"
	)
	# outstanding credit-positive → Credit SA; debit-positive outstanding → Debit SA.
	sa_credit = outstanding if outstanding > 0 else 0.0
	sa_debit = abs(outstanding) if outstanding < 0 else 0.0
	sa_row = frappe._dict(
		{
			"account": sa,
			"cost_center": sa_cost_center,
			"company": doc.company,
			"posting_date": template.get("posting_date") or doc.get("posting_date"),
			"remarks": remarks,
			"voucher_type": doc.doctype,
			"voucher_no": doc.name,
			"project": template.get("project"),
			"is_opening": template.get("is_opening") or "No",
			"debit": sa_debit,
			"credit": sa_credit,
			"account_currency": ccy,
			"transaction_currency": ccy,
			"debit_in_account_currency": sa_debit,
			"credit_in_account_currency": sa_credit,
			"debit_in_transaction_currency": sa_debit,
			"credit_in_transaction_currency": sa_credit,
			"_irr_inventory_valuation_residual": 1,
			"_irr_sabe_localization": 1,
			**dimensions,
		}
	)
	if abs(sa_debit) < 0.5 and abs(sa_credit) < 0.5:
		return gl_map

	gl_map.append(sa_row)
	recon["posted_sa_only"] = True
	doc.flags.iran_sabe_a2_recon = recon
	return gl_map

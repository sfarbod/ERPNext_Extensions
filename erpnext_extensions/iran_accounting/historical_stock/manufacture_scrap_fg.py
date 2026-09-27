# Copyright (c) 2026, ERPNext Extensions contributors
"""Manufacture Scrap + FG reconstruction (Development, v5.3.24+).

ERPNext 16.35 conservation (Stock Entry.get_basic_rate_for_manufactured_item):

    FG_basic_rate = (outgoing_source_cost − costed_out_scrap_cost) / FG_qty

where costed_out_scrap_cost = Σ scrap.basic_amount for rows with
valuation_type in (Valuation Rate, Manual) or secondary_item_type without
BOM secondary link (is_costed_out_of_finished_item).

Scrap rows normally take rate from get_valuation_rate(t_warehouse). When that
warehouse rate is exploded relative to the *same voucher's* source consume
SLE economics for the same item, scrap is corrupt and FG residual collapses
(often negative).

Authority for EXACT repair of exploded same-item scrap:

    scrap_rate = |source SLE SVD| / |source qty|   (same voucher, same item)

Then:

    scrap_basic_amount = scrap_rate × scrap_qty
    FG_basic_rate = (Σ |source SLE SVD| − Σ corrected scrap_basic_amount) / FG_qty

Business quantities are never written. Scrap + FG are one atomic write set.
"""

from __future__ import annotations

from time import perf_counter
from typing import Any

import frappe
from frappe.utils import cint, flt

from erpnext_extensions.iran_accounting.historical_stock import (
	CONFIDENCE_EXACT,
	CONFIDENCE_MANUAL,
	HISTORICAL_REPAIR_FLAG,
	QTY_EPS,
	RATE_EPS,
	VALUE_EPS,
)
from erpnext_extensions.iran_accounting.historical_stock.audit import append_entry, finish_run, start_run
from erpnext_extensions.iran_accounting.historical_stock.manufacture_native import (
	EXACT,
	HEALTHY,
	LEGITIMATE,
	MANUAL,
	WAITING_UPSTREAM,
	_is_byproduct_like,
	_is_scrap_like,
	_scrap_value_from_same_voucher_consume,
	reconstruct_manufacture_valuation,
)

# Scrap SE rate > this multiple of same-voucher consume rate ⇒ exploded.
EXPLOSION_RATIO = 2.0
STRATEGY = "MANUFACTURE_SCRAP_FG_RECONSTRUCTION"


def _load_voucher(voucher: str) -> tuple[Any, list, dict]:
	se = frappe.db.get_value(
		"Stock Entry",
		voucher,
		[
			"name",
			"purpose",
			"docstatus",
			"company",
			"work_order",
			"job_card",
			"posting_date",
			"posting_time",
			"total_additional_costs",
		],
		as_dict=True,
	)
	details = frappe.db.sql(
		"""
		SELECT name, idx, item_code, qty, transfer_qty, basic_rate, valuation_rate,
		       amount, basic_amount, s_warehouse, t_warehouse,
		       IFNULL(is_finished_item,0) AS is_fg,
		       IFNULL(is_scrap_item,0) AS is_scrap,
		       IFNULL(allow_zero_valuation_rate,0) AS allow_zero,
		       secondary_item_type, valuation_type, bom_secondary_item,
		       IFNULL(additional_cost,0) AS additional_cost
		FROM `tabStock Entry Detail`
		WHERE parent=%s
		ORDER BY idx
		""",
		(voucher,),
		as_dict=True,
	)
	sles = frappe.db.sql(
		"""
		SELECT name, voucher_detail_no, item_code, warehouse, actual_qty,
		       incoming_rate, outgoing_rate, valuation_rate,
		       stock_value_difference, stock_value, batch_no
		FROM `tabStock Ledger Entry`
		WHERE voucher_no=%s AND IFNULL(is_cancelled,0)=0
		""",
		(voucher,),
		as_dict=True,
	)
	by_detail = {s.voucher_detail_no: s for s in sles}
	return se, details, by_detail


def _is_costed_out(d) -> bool:
	"""Mirror ERPNext is_costed_out_of_finished_item."""
	vt = str(d.valuation_type or "")
	if vt in ("Valuation Rate", "Manual"):
		return True
	if (d.secondary_item_type or "") and not d.bom_secondary_item:
		return True
	if cint(d.is_scrap) or _is_scrap_like(d):
		return True
	return False


def analyze_manufacture_scrap_fg(voucher: str) -> dict[str, Any]:
	"""Classify one Manufacture voucher for exploded-scrap + FG repair."""
	if not voucher or not frappe.db.exists("Stock Entry", voucher):
		return {"voucher": voucher, "classification": MANUAL, "eligible": False, "reason": "missing"}
	se, details, by_detail = _load_voucher(voucher)
	if not se or se.purpose != "Manufacture" or cint(se.docstatus) != 1:
		return {
			"voucher": voucher,
			"classification": MANUAL,
			"eligible": False,
			"reason": "not submitted Manufacture",
		}

	sources = []
	scraps = []
	fgs = []
	consumed = 0.0
	poisoned = 0
	for d in details:
		sle = by_detail.get(d.name)
		if cint(d.is_fg):
			fgs.append({"detail": d, "sle": sle})
			continue
		if d.s_warehouse and sle and flt(sle.actual_qty) < 0:
			svd = abs(flt(sle.stock_value_difference))
			orate = abs(flt(sle.outgoing_rate))
			qty = abs(flt(d.qty))
			rate = orate if orate > RATE_EPS else (svd / qty if qty > QTY_EPS else 0.0)
			consumed += svd
			zero_poison = qty > QTY_EPS and svd <= VALUE_EPS and orate <= RATE_EPS and not cint(d.allow_zero)
			if zero_poison:
				poisoned += 1
			sources.append(
				{
					"detail": d.name,
					"item": d.item_code,
					"qty": qty,
					"sle": sle.name if sle else None,
					"sle_outgoing": orate,
					"sle_svd": svd,
					"consume_rate": rate,
					"zero_poison": zero_poison,
				}
			)
		elif _is_costed_out(d) or _is_scrap_like(d) or _is_byproduct_like(d):
			scraps.append({"detail": d, "sle": sle})

	native = reconstruct_manufacture_valuation(voucher)

	if poisoned:
		return {
			"voucher": voucher,
			"strategy": STRATEGY,
			"classification": WAITING_UPSTREAM,
			"eligible": False,
			"confidence": CONFIDENCE_MANUAL,
			"reason": f"{poisoned} zero-value source rows — repair upstream first",
			"consumed_value": consumed,
			"native": native,
		}
	if len(fgs) != 1:
		return {
			"voucher": voucher,
			"strategy": STRATEGY,
			"classification": MANUAL,
			"eligible": False,
			"confidence": CONFIDENCE_MANUAL,
			"reason": f"multi/zero FG ({len(fgs)}) not auto-EXACT",
			"consumed_value": consumed,
			"native": native,
		}

	consume_by_item = {}
	for s in sources:
		consume_by_item.setdefault(s["item"], s)

	scrap_plans = []
	exploded_n = 0
	corrected_scrap_value = 0.0
	documented_scrap_value = 0.0
	for sc in scraps:
		d = sc["detail"]
		sle = sc["sle"]
		qty = abs(flt(d.qty))
		doc_rate = abs(flt(d.basic_rate))
		doc_amt = abs(flt(d.basic_amount)) or doc_rate * qty
		documented_scrap_value += doc_amt
		peer = consume_by_item.get(d.item_code)
		expected_rate = None
		expected_amt = None
		ratio = None
		exploded = False
		if peer and peer["consume_rate"] > RATE_EPS and qty > QTY_EPS:
			expected_rate = peer["consume_rate"]
			expected_amt = expected_rate * qty
			if doc_rate > RATE_EPS:
				ratio = doc_rate / expected_rate
				if ratio > EXPLOSION_RATIO:
					exploded = True
					exploded_n += 1
			elif doc_amt <= VALUE_EPS and expected_amt > VALUE_EPS:
				# documented zero with consume peer — keep zero (QC sample)
				expected_rate = 0.0
				expected_amt = 0.0
		if expected_amt is None:
			# No same-item consume peer: keep documented amount when present
			expected_rate = doc_rate
			expected_amt = doc_amt
		corrected_scrap_value += expected_amt if exploded else doc_amt
		scrap_plans.append(
			{
				"detail": d.name,
				"sle": sle.name if sle else None,
				"item": d.item_code,
				"warehouse": d.t_warehouse,
				"qty": qty,
				"current_rate": doc_rate,
				"current_amount": doc_amt,
				"expected_rate": expected_rate if exploded else doc_rate,
				"expected_amount": expected_amt if exploded else doc_amt,
				"consume_peer_rate": (peer or {}).get("consume_rate"),
				"explosion_ratio": ratio,
				"exploded": exploded,
				"valuation_type": d.valuation_type,
				"secondary_item_type": d.secondary_item_type,
			}
		)

	fg = fgs[0]
	fd = fg["detail"]
	fsle = fg["sle"]
	fg_qty = abs(flt(fd.qty))
	if fg_qty <= QTY_EPS:
		return {
			"voucher": voucher,
			"strategy": STRATEGY,
			"classification": MANUAL,
			"eligible": False,
			"reason": "FG qty zero",
		}

	residual = consumed - corrected_scrap_value
	if residual < -VALUE_EPS:
		return {
			"voucher": voucher,
			"strategy": STRATEGY,
			"classification": MANUAL,
			"eligible": False,
			"confidence": CONFIDENCE_MANUAL,
			"reason": (
				f"even after correcting exploded scrap, residual {residual} < 0 "
				f"(consumed={consumed}, corrected_scrap={corrected_scrap_value})"
			),
			"scrap_plans": scrap_plans,
			"consumed_value": consumed,
			"documented_scrap_value": documented_scrap_value,
			"corrected_scrap_value": corrected_scrap_value,
		}

	expected_fg_rate = residual / fg_qty
	current_fg_rate = abs(flt(fd.basic_rate))
	# Negative FG basic_rate is always corrupt under this contract.
	fg_negative = flt(fd.basic_rate) < -RATE_EPS
	fg_mismatch = abs(current_fg_rate - expected_fg_rate) > 1.0 or fg_negative

	# High scrap-vs-consume ratio is not corruption when native conservation
	# already holds (FG matches residual using documented scrap). 37090-class.
	native_cls = native.get("classification")
	native_healthy = native_cls in (HEALTHY, LEGITIMATE) and not fg_negative
	conservation_broken = (
		fg_negative
		or residual < -VALUE_EPS
		or native_cls == MANUAL
		and "exceeds consumption" in str(native.get("reason") or "")
	)

	if exploded_n == 0 and not fg_negative:
		# No exploded scrap — defer to manufacture_native HEALTHY/EXACT.
		cls = HEALTHY if native_cls == HEALTHY else native_cls or LEGITIMATE
		return {
			"voucher": voucher,
			"strategy": STRATEGY,
			"classification": cls if cls != EXACT else HEALTHY,
			"eligible": False,
			"confidence": CONFIDENCE_EXACT,
			"reason": "no exploded same-item scrap; native manufacture contract applies",
			"scrap_plans": scrap_plans,
			"consumed_value": consumed,
			"documented_scrap_value": documented_scrap_value,
			"corrected_scrap_value": corrected_scrap_value,
			"expected_fg_rate": expected_fg_rate,
			"current_fg_rate": flt(fd.basic_rate),
			"native": {
				k: native.get(k)
				for k in ("classification", "eligible", "expected_target_rate", "reason")
			},
		}

	if exploded_n and native_healthy and not conservation_broken:
		return {
			"voucher": voucher,
			"strategy": STRATEGY,
			"classification": HEALTHY,
			"eligible": False,
			"confidence": CONFIDENCE_EXACT,
			"reason": (
				"scrap rate differs from same-item consume rate but native FG residual "
				"already matches documented scrap — legitimate warehouse-rate scrap"
			),
			"scrap_plans": scrap_plans,
			"consumed_value": consumed,
			"documented_scrap_value": documented_scrap_value,
			"corrected_scrap_value": documented_scrap_value,
			"expected_fg_rate": native.get("expected_target_rate"),
			"current_fg_rate": flt(fd.basic_rate),
			"exploded_count": exploded_n,
			"native": {
				k: native.get(k)
				for k in ("classification", "eligible", "expected_target_rate", "reason")
			},
		}

	if exploded_n == 0 and fg_negative:
		return {
			"voucher": voucher,
			"strategy": STRATEGY,
			"classification": MANUAL,
			"eligible": False,
			"reason": "FG negative without exploded same-item scrap — needs broader evidence",
			"current_fg_rate": flt(fd.basic_rate),
			"scrap_plans": scrap_plans,
		}

	fg_plan = {
		"detail": fd.name,
		"sle": fsle.name if fsle else None,
		"item": fd.item_code,
		"warehouse": fd.t_warehouse,
		"qty": fg_qty,
		"current_rate": flt(fd.basic_rate),
		"current_amount": flt(fd.basic_amount),
		"expected_rate": expected_fg_rate,
		"expected_amount": expected_fg_rate * fg_qty,
		"additional_cost": flt(fd.additional_cost),
		"negative": fg_negative,
		"mismatch": fg_mismatch,
	}

	return {
		"voucher": voucher,
		"strategy": STRATEGY,
		"classification": EXACT,
		"eligible": True,
		"confidence": CONFIDENCE_EXACT,
		"reason": (
			f"{exploded_n} exploded scrap row(s); "
			f"FG {'negative ' if fg_negative else ''}"
			f"current={flt(fd.basic_rate)} expected={expected_fg_rate:.6f}"
		),
		"work_order": se.work_order,
		"job_card": se.job_card,
		"posting_date": str(se.posting_date),
		"posting_time": str(se.posting_time),
		"consumed_value": consumed,
		"documented_scrap_value": documented_scrap_value,
		"corrected_scrap_value": corrected_scrap_value,
		"conservation": {
			"consumed": consumed,
			"scrap": corrected_scrap_value,
			"fg": expected_fg_rate * fg_qty,
			"residual_check": consumed - corrected_scrap_value - expected_fg_rate * fg_qty,
		},
		"scrap_plans": scrap_plans,
		"fg_plan": fg_plan,
		"exploded_count": exploded_n,
		"sources": sources,
	}


def scan_exploded_scrap_fg(company=None, limit=500) -> dict:
	"""Find Manufacture vouchers with exploded same-item scrap signature."""
	company = company or frappe.defaults.get_user_default("Company")
	vouchers = frappe.db.sql(
		"""
		SELECT DISTINCT se.name
		FROM `tabStock Entry` se
		JOIN `tabStock Entry Detail` sed ON sed.parent=se.name
		WHERE se.docstatus=1 AND se.purpose='Manufacture'
		  AND (IFNULL(sed.secondary_item_type,'')!='' OR IFNULL(sed.is_scrap_item,0)=1
		       OR IFNULL(sed.valuation_type,'') IN ('Valuation Rate','Manual'))
		  AND (%s IS NULL OR se.company=%s)
		ORDER BY se.posting_date, se.posting_time, se.name
		LIMIT %s
		""",
		(company, company, int(limit)),
	)
	rows = []
	for (vn,) in vouchers:
		an = analyze_manufacture_scrap_fg(vn)
		if an.get("classification") in (EXACT, MANUAL, WAITING_UPSTREAM) or an.get("exploded_count"):
			rows.append(an)
	by_cls = Counter_cls(rows)
	return {
		"count": len(rows),
		"by_classification": by_cls,
		"exact": [r for r in rows if r.get("eligible")],
		"rows": rows,
	}


def Counter_cls(rows):
	from collections import Counter

	return dict(Counter(r.get("classification") for r in rows))


def repair_manufacture_scrap_fg(voucher: str, *, dry_run: bool = True) -> dict:
	"""Atomically rewrite exploded scrap + dependent FG on one Manufacture Entry."""
	an = analyze_manufacture_scrap_fg(voucher)
	if not an.get("eligible") or an.get("classification") != EXACT:
		return {
			"ok": False,
			"aborted": True,
			"dry_run": dry_run,
			"reason": an.get("reason") or "not eligible",
			"analyze": an,
		}

	scrap_writes = [p for p in (an.get("scrap_plans") or []) if p.get("exploded")]
	fg = an.get("fg_plan") or {}
	preview = {
		"voucher": voucher,
		"scrap_writes": scrap_writes,
		"fg": fg,
		"conservation": an.get("conservation"),
	}
	if dry_run:
		return {
			"ok": True,
			"dry_run": True,
			"preview": preview,
			"analyze": an,
			"economic_writes": 0,
			"strategy": STRATEGY,
		}

	log = start_run(STRATEGY, dry_run=False)
	frappe.flags[HISTORICAL_REPAIR_FLAG] = True
	savepoint = f"msgf_{frappe.generate_hash(length=8)}"
	t0 = perf_counter()
	try:
		frappe.db.savepoint(savepoint)
		writes = 0
		# Snapshot quantities — refuse if business qty would change.
		qty_before = {
			r.name: flt(r.qty)
			for r in frappe.db.sql(
				"SELECT name, qty FROM `tabStock Entry Detail` WHERE parent=%s",
				voucher,
				as_dict=True,
			)
		}
		for p in scrap_writes:
			rate = abs(flt(p["expected_rate"]))
			qty = abs(flt(p["qty"]))
			amt = rate * qty
			frappe.db.set_value(
				"Stock Entry Detail",
				p["detail"],
				{
					"basic_rate": rate,
					"valuation_rate": rate,
					"basic_amount": amt,
					"amount": amt,
				},
				update_modified=False,
			)
			writes += 1
			if p.get("sle"):
				frappe.db.set_value(
					"Stock Ledger Entry",
					p["sle"],
					{
						"incoming_rate": rate,
						"outgoing_rate": 0,
						"valuation_rate": rate,
						"stock_value_difference": amt,
					},
					update_modified=False,
				)
				writes += 1
		# FG
		fg_rate = abs(flt(fg["expected_rate"]))
		fg_qty = abs(flt(fg["qty"]))
		fg_add = abs(flt(fg.get("additional_cost") or 0))
		fg_basic = fg_rate * fg_qty
		fg_amount = fg_basic + fg_add
		frappe.db.set_value(
			"Stock Entry Detail",
			fg["detail"],
			{
				"basic_rate": fg_rate,
				"valuation_rate": fg_rate,
				"basic_amount": fg_basic,
				"amount": fg_amount,
			},
			update_modified=False,
		)
		writes += 1
		if fg.get("sle"):
			sle_rate = fg_amount / fg_qty if fg_qty > QTY_EPS else fg_rate
			frappe.db.set_value(
				"Stock Ledger Entry",
				fg["sle"],
				{
					"incoming_rate": abs(sle_rate),
					"outgoing_rate": 0,
					"valuation_rate": abs(sle_rate),
					"stock_value_difference": fg_amount,
				},
				update_modified=False,
			)
			writes += 1

		qty_after = {
			r.name: flt(r.qty)
			for r in frappe.db.sql(
				"SELECT name, qty FROM `tabStock Entry Detail` WHERE parent=%s",
				voucher,
				as_dict=True,
			)
		}
		for name, q0 in qty_before.items():
			if abs(q0 - qty_after.get(name, q0)) > QTY_EPS:
				raise frappe.ValidationError(f"quantity mutated on {name}")

		# Verify conservation on SE after write
		post = analyze_manufacture_scrap_fg(voucher)
		if post.get("exploded_count"):
			raise frappe.ValidationError(
				f"post-write still has exploded scrap: {post.get('exploded_count')}"
			)
		if flt((post.get("fg_plan") or {}).get("current_rate") or post.get("current_fg_rate") or 0) < -RATE_EPS:
			# re-read FG
			fg_now = flt(
				frappe.db.get_value("Stock Entry Detail", fg["detail"], "basic_rate")
			)
			if fg_now < -RATE_EPS:
				raise frappe.ValidationError(f"FG still negative after write: {fg_now}")

		frappe.db.commit()
		row_out = {
			"voucher": voucher,
			"written": True,
			"economic_writes": writes,
			"strategy": STRATEGY,
			"preview": preview,
			"post_analyze": {
				k: post.get(k)
				for k in ("classification", "eligible", "exploded_count", "reason", "conservation")
			},
		}
		append_entry(log, row_out, written=True)
		finish_run(log, applied=1, blocked=0)
		return {
			"ok": True,
			"dry_run": False,
			"written": True,
			"economic_writes": writes,
			"strategy": STRATEGY,
			"preview": preview,
			"post_analyze": row_out["post_analyze"],
			"elapsed_seconds": round(perf_counter() - t0, 3),
			"repair_run_id": getattr(log, "repair_run_id", None),
			"riv": "NOT_INVOKED",
		}
	except Exception as exc:
		frappe.db.rollback(save_point=savepoint)
		finish_run(log, applied=0, blocked=1, error=str(exc))
		return {
			"ok": False,
			"aborted": True,
			"dry_run": False,
			"reason": str(exc)[:500],
			"strategy": STRATEGY,
			"analyze": an,
		}
	finally:
		frappe.flags[HISTORICAL_REPAIR_FLAG] = False


def repair_and_riv_manufacture_scrap_fg(voucher: str, *, dry_run: bool = False) -> dict:
	"""Repair scrap+FG then run narrow RIV from voucher posting datetime."""
	from erpnext_extensions.iran_accounting.historical_stock.leftover_ma import create_and_run_narrow_riv

	live = repair_manufacture_scrap_fg(voucher, dry_run=dry_run)
	if dry_run or not live.get("ok"):
		return live
	an = live.get("preview") or {}
	fg = an.get("fg") or {}
	item = fg.get("item")
	wh = fg.get("warehouse")
	se = frappe.db.get_value(
		"Stock Entry", voucher, ["posting_date", "posting_time"], as_dict=True
	)
	riv = None
	if item and wh and se:
		riv = create_and_run_narrow_riv(
			item, wh, posting_date=se.posting_date, posting_time=se.posting_time
		)
		frappe.db.commit()
	# Also RIV scrap warehouses touched
	scrap_rivs = []
	for p in an.get("scrap_writes") or []:
		if p.get("item") and p.get("warehouse"):
			sr = create_and_run_narrow_riv(
				p["item"],
				p["warehouse"],
				posting_date=se.posting_date,
				posting_time=se.posting_time,
			)
			frappe.db.commit()
			scrap_rivs.append({"item": p["item"], "warehouse": p["warehouse"], **{k: sr.get(k) for k in ("ok", "riv_status", "riv_name")}})
	post = analyze_manufacture_scrap_fg(voucher)
	live["riv_fg"] = {k: (riv or {}).get(k) for k in ("ok", "riv_status", "riv_name", "reason")}
	live["riv_scrap"] = scrap_rivs
	live["post_riv_analyze"] = {
		k: post.get(k)
		for k in ("classification", "eligible", "exploded_count", "reason", "conservation", "fg_plan")
	}
	live["riv_stable"] = not post.get("eligible") and not post.get("exploded_count")
	return live

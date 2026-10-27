"""v5.6.25 release gate: historical Category A/B detection + repair + RIV bootstrap.

Creates isolated discrepancies via temporary stubs during submit (no direct SLE/GL
writes), then exercises campaign dry-run and repair orchestration.
"""

from __future__ import annotations

import json
from typing import Any

import frappe
from frappe.utils import flt, nowdate


TAG = "E2E_V5625_HIST_GATE"


def _company_ctx() -> dict[str, Any]:
	company = frappe.db.get_value("Company", {"is_group": 0}, "name", order_by="creation asc")
	whs = frappe.db.sql(
		"""select name from `tabWarehouse`
		   where company=%s and is_group=0 and disabled=0 order by creation limit 3""",
		company,
		as_dict=True,
	)
	items = frappe.db.sql(
		"""select i.name, i.stock_uom from `tabItem` i
		   where i.disabled=0 and i.is_stock_item=1 and ifnull(i.has_serial_no,0)=0
		     and ifnull(i.has_batch_no,0)=0 and i.is_fixed_asset=0
		   order by i.creation desc limit 80""",
		as_dict=True,
	)
	item = None
	wh = whs[0].name
	for it in items:
		bins = frappe.db.sql(
			"select actual_qty, stock_value from `tabBin` where item_code=%s and warehouse=%s",
			(it.name, wh),
			as_dict=True,
		)
		if any(flt(b.actual_qty) or flt(b.stock_value) for b in bins):
			continue
		item = it
		break
	item = item or items[0]
	supplier = frappe.db.get_value("Supplier", {"disabled": 0}, "name", order_by="creation asc")
	cc = frappe.db.get_value("Company", company, "cost_center") or frappe.db.get_value(
		"Cost Center", {"company": company, "is_group": 0}, "name"
	)
	dept = "واحد انبار - E"
	lcv_expense = "111606 - حساب کنترل هزینه های جانبی خرید کالای داخلی - E"
	if not frappe.db.exists("Account", lcv_expense):
		lcv_expense = frappe.db.get_value(
			"Account", {"company": company, "is_group": 0, "root_type": "Expense"}, "name"
		)
	return {
		"company": company,
		"warehouse": wh,
		"item": item,
		"supplier": supplier,
		"cost_center": cc,
		"department": dept,
		"lcv_expense": lcv_expense,
	}


def _po(ctx, qty, rate, remarks):
	po = frappe.new_doc("Purchase Order")
	po.update(
		dict(
			company=ctx["company"],
			supplier=ctx["supplier"],
			transaction_date=nowdate(),
			schedule_date=nowdate(),
			currency="IRR",
			conversion_rate=1,
		)
	)
	if frappe.get_meta("Purchase Order").has_field("remarks"):
		po.remarks = remarks
	row = dict(
		item_code=ctx["item"].name,
		qty=qty,
		rate=rate,
		uom=ctx["item"].stock_uom,
		stock_uom=ctx["item"].stock_uom,
		conversion_factor=1,
		warehouse=ctx["warehouse"],
		schedule_date=nowdate(),
		cost_center=ctx["cost_center"],
	)
	if frappe.get_meta("Purchase Order Item").has_field("department"):
		row["department"] = ctx["department"]
	po.append("items", row)
	po.flags.ignore_permissions = True
	po.insert()
	po.submit()
	frappe.db.commit()
	return po.name


def _pr_from_po(po_name, remarks):
	from erpnext.buying.doctype.purchase_order.purchase_order import make_purchase_receipt

	pr = make_purchase_receipt(po_name)
	if frappe.get_meta("Purchase Receipt").has_field("remarks"):
		pr.remarks = remarks
	pr.flags.ignore_permissions = True
	pr.insert()
	pr.submit()
	frappe.db.commit()
	return pr.name


def _pi_from_pr(pr_name, rate, remarks, *, stub_adjust: bool = False, bill_po: str | None = None):
	from erpnext.stock.doctype.purchase_receipt.purchase_receipt import make_purchase_invoice
	import erpnext.stock.doctype.purchase_receipt.purchase_receipt as pr_mod

	pi = make_purchase_invoice(pr_name)
	pi.posting_date = nowdate()
	pi.update_stock = 0
	pi.currency = "IRR"
	pi.conversion_rate = 1
	pi.set("taxes", [])
	if frappe.get_meta("Purchase Invoice").has_field("remarks"):
		pi.remarks = remarks
	po_item = None
	if bill_po:
		po_item = frappe.db.get_value("Purchase Order Item", {"parent": bill_po}, "name")
	for row in pi.items:
		row.rate = rate
		if bill_po and po_item:
			row.purchase_order = bill_po
			row.po_detail = po_item
	pi.flags.ignore_permissions = True
	pi.calculate_taxes_and_totals()

	_orig = pr_mod.adjust_incoming_rate_for_pr
	if stub_adjust:
		pr_mod.adjust_incoming_rate_for_pr = lambda doc: None
	try:
		pi.insert()
		pi.submit()
		frappe.db.commit()
	finally:
		pr_mod.adjust_incoming_rate_for_pr = _orig
	return pi.name


def _lcv(ctx, pr_name, amount, remarks, *, legacy_integer_vr: bool = False):
	from erpnext_extensions.iran_accounting.domain import qty_rate_amount as qra
	from erpnext_extensions.iran_accounting.domain import irr_rounding_residual as irr_res
	from erpnext_extensions.iran_accounting.domain import stock_ledger_deterministic as sld
	from erpnext_extensions.iran_accounting.domain.currency import round_currency

	_orig_align = qra._align_purchase_valuation_rate_amount_preserving
	_orig_auth = sld.authoritative_acquisition_incoming_rate
	_orig_residual = irr_res.apply_irr_rate_rounding_residual_gl

	def _legacy(row, doc):
		# Force classic integer VR from composed amount (historical bug shape).
		qty = flt(row.get("qty") or row.get("stock_qty") or 0)
		if qty <= 0:
			return
		base = flt(row.get("base_net_amount") or row.get("base_amount") or 0)
		pi_diff = flt(row.get("amount_difference_with_purchase_invoice") or 0)
		lcv_amt = flt(row.get("landed_cost_voucher_amount") or 0)
		amount_total = base + pi_diff + lcv_amt
		row.valuation_rate = float(round_currency(amount_total / qty, "IRR"))

	def _auth_integer(sle_doc_or_dict, currency=None):
		rate = _orig_auth(sle_doc_or_dict, currency)
		if rate:
			return float(round_currency(rate, currency or "IRR"))
		return rate

	lcv = frappe.new_doc("Landed Cost Voucher")
	lcv.company = ctx["company"]
	lcv.posting_date = nowdate()
	lcv.distribute_charges_based_on = "Qty"
	if frappe.get_meta("Landed Cost Voucher").has_field("remarks"):
		lcv.remarks = remarks
	lcv.append(
		"purchase_receipts",
		{"receipt_document_type": "Purchase Receipt", "receipt_document": pr_name},
	)
	lcv.append(
		"taxes",
		{"expense_account": ctx["lcv_expense"], "amount": amount, "description": remarks},
	)
	lcv.flags.ignore_permissions = True
	if legacy_integer_vr:
		qra._align_purchase_valuation_rate_amount_preserving = _legacy
		sld.authoritative_acquisition_incoming_rate = _auth_integer
		# Allow legacy residual path so the inflated SLE can be submitted for the fixture.
		irr_res.apply_irr_rate_rounding_residual_gl = lambda *a, **k: None
	try:
		lcv.get_items_from_purchase_receipts()
		lcv.insert()
		lcv.submit()
		frappe.db.commit()
	finally:
		qra._align_purchase_valuation_rate_amount_preserving = _orig_align
		sld.authoritative_acquisition_incoming_rate = _orig_auth
		irr_res.apply_irr_rate_rounding_residual_gl = _orig_residual
	return lcv.name


def _sle_svd(pr_name):
	return flt(
		frappe.db.sql(
			"""select stock_value_difference from `tabStock Ledger Entry`
			   where voucher_type='Purchase Receipt' and voucher_no=%s and is_cancelled=0
			   limit 1""",
			pr_name,
		)[0][0]
	)


def scenario_a(ctx) -> dict[str, Any]:
	"""Historical PI valuation: PI adjusts document economics but SLE stays provisional."""
	from erpnext_extensions.iran_accounting.historical_rebuild.purchase_valuation_detection import (
		dry_run_purchase_valuation_scan,
	)
	from erpnext_extensions.iran_accounting.historical_rebuild.purchase_valuation_repair import (
		assess_repair,
		repair_one,
	)

	qty, pr_rate, pi_rate = 1000, 1_000_000, 26_900_332
	expected = qty * pi_rate
	# Dual PO pattern for site Server Script that locks PO rate
	po_recv = _po(ctx, qty, pr_rate, f"{TAG} A recv")
	pr = _pr_from_po(po_recv, f"{TAG} A PR")
	# Billing PO at invoice rate (site may require)
	po_bill = _po(ctx, qty, pi_rate, f"{TAG} A bill")
	# Link PI via PR; stub adjust so SLE remains provisional
	pi = _pi_from_pr(pr, pi_rate, f"{TAG} A PI", stub_adjust=True, bill_po=po_bill)

	svd_before = _sle_svd(pr)
	scan = dry_run_purchase_valuation_scan(
		ctx["company"], purchase_receipts=[pr], limit=10
	)
	cat_a = [
		f
		for f in scan.get("findings") or []
		if f.get("voucher_no") == pr and f.get("root_cause_category") == "A_INVOICE_BASED_VALUATION"
	]
	assess = assess_repair(pr, company=ctx["company"])
	# Prove RIV alone assessment
	riv_alone = assess.get("riv_alone_sufficient")
	# Dry-run repair
	dry = repair_one(pr, execute=False, company=ctx["company"])
	# Execute repair
	rep = repair_one(pr, execute=True, company=ctx["company"])
	svd_after = _sle_svd(pr)
	# Idempotent second repair
	rep2 = repair_one(pr, execute=True, company=ctx["company"])

	return {
		"scenario": "A_PI_VALUATION",
		"pr": pr,
		"pi": pi,
		"po_recv": po_recv,
		"po_bill": po_bill,
		"expected": expected,
		"sle_before": svd_before,
		"sle_after": svd_after,
		"detected_category_a": bool(cat_a),
		"detection_difference": cat_a[0].get("difference_amount") if cat_a else None,
		"related_pi": cat_a[0].get("related_purchase_invoices") if cat_a else None,
		"assess_riv_alone": riv_alone,
		"dry_run_status": dry.get("status"),
		"repair_status": rep.get("status"),
		"grni_after": rep.get("grni_net_after"),
		"idempotent_status": rep2.get("status"),
		"pass": (
			bool(cat_a)
			and abs(svd_before - qty * pr_rate) <= 1
			and abs(svd_after - expected) <= 1
			and abs(flt(rep.get("grni_net_after"))) <= 1
			and rep.get("status") == "repaired"
			and rep2.get("status") in {"repaired", "idempotent_skip"}
		),
	}


def scenario_b(ctx) -> dict[str, Any]:
	"""Historical LCV integer VR inflation."""
	from erpnext_extensions.iran_accounting.historical_rebuild.purchase_valuation_detection import (
		dry_run_purchase_valuation_scan,
		scan_purchase_grni_residuals,
	)
	from erpnext_extensions.iran_accounting.historical_rebuild.purchase_valuation_repair import (
		assess_repair,
		repair_one,
	)

	qty, rate, lcv_amt = 800, 1_000_000, 1500
	expected = qty * rate + lcv_amt
	po = _po(ctx, qty, rate, f"{TAG} B po")
	pr = _pr_from_po(po, f"{TAG} B PR")
	pi = _pi_from_pr(pr, rate, f"{TAG} B PI", stub_adjust=False, bill_po=po)
	lcv = _lcv(ctx, pr, lcv_amt, f"{TAG} B LCV", legacy_integer_vr=True)

	svd_before = _sle_svd(pr)
	scan = dry_run_purchase_valuation_scan(
		ctx["company"], purchase_receipts=[pr], limit=10
	)
	cat_b = [
		f
		for f in scan.get("findings") or []
		if f.get("voucher_no") == pr
		and f.get("root_cause_category") == "B_LANDED_COST_ROUNDING"
	]
	related = [pr, pi, lcv]
	grni_findings = scan_purchase_grni_residuals(ctx["company"], voucher_nos=related)
	assess = assess_repair(pr, company=ctx["company"])
	dry = repair_one(pr, execute=False, company=ctx["company"])
	rep = repair_one(pr, execute=True, company=ctx["company"])
	svd_after = _sle_svd(pr)
	rep2 = repair_one(pr, execute=True, company=ctx["company"])

	return {
		"scenario": "B_LCV_ROUNDING",
		"pr": pr,
		"pi": pi,
		"lcv": lcv,
		"expected": expected,
		"sle_before": svd_before,
		"sle_after": svd_after,
		"overvaluation_before": svd_before - expected,
		"detected_category_b": bool(cat_b),
		"detection_difference": cat_b[0].get("difference_amount") if cat_b else None,
		"grni_findings_before_count": len(grni_findings),
		"grni_before_sample": grni_findings[:2],
		"assess_riv_alone": assess.get("riv_alone_sufficient"),
		"dry_run_status": dry.get("status"),
		"repair_status": rep.get("status"),
		"grni_after": rep.get("grni_net_after"),
		"idempotent_status": rep2.get("status"),
		"pass": (
			bool(cat_b)
			and abs(svd_before - expected) > 1
			and abs(svd_after - expected) <= 1
			and abs(flt(rep.get("grni_net_after"))) <= 1
			and rep.get("status") == "repaired"
			and rep2.get("status") in {"repaired", "idempotent_skip"}
		),
	}


def scenario_control(ctx) -> dict[str, Any]:
	"""Valid PR/PI/LCV must not be flagged Category A/B."""
	from erpnext_extensions.iran_accounting.historical_rebuild.purchase_valuation_detection import (
		dry_run_purchase_valuation_scan,
	)

	qty, rate, lcv_amt = 50, 1_000_000, 25
	po = _po(ctx, qty, rate, f"{TAG} CTRL po")
	pr = _pr_from_po(po, f"{TAG} CTRL PR")
	pi = _pi_from_pr(pr, rate, f"{TAG} CTRL PI", stub_adjust=False, bill_po=po)
	lcv = _lcv(ctx, pr, lcv_amt, f"{TAG} CTRL LCV", legacy_integer_vr=False)
	scan = dry_run_purchase_valuation_scan(
		ctx["company"], purchase_receipts=[pr], limit=5
	)
	bad = [
		f
		for f in scan.get("findings") or []
		if f.get("voucher_no") == pr
		and f.get("root_cause_category") in ("A_INVOICE_BASED_VALUATION", "B_LANDED_COST_ROUNDING")
	]
	return {
		"scenario": "CONTROL_VALID",
		"pr": pr,
		"pi": pi,
		"lcv": lcv,
		"false_positives": len(bad),
		"pass": len(bad) == 0,
	}


def scenario_riv_bootstrap() -> dict[str, Any]:
	"""Prove execute_job path loads Iran patches via pickle-safe riv_execute_guard.

	Nested locals wraps of ``execute_reposting_entry`` are intentionally not used —
	they break RQ pickling. The module-level guard bootstraps on every RIV call.
	"""
	from erpnext.stock import stock_ledger as sl
	from frappe.utils.background_jobs import execute_job
	import erpnext.stock.doctype.repost_item_valuation.repost_item_valuation as riv_mod
	from erpnext_extensions.iran_accounting.integration import riv_execute_guard as guard
	from erpnext_extensions.iran_accounting.domain.riv_valuation_guard import (
		is_iran_riv_recalculate_wrapper_active,
	)

	was_patched = getattr(sl, "_iran_patched_update_entries_after", None)

	execute_job(
		site=frappe.local.site,
		method="erpnext_extensions.iran_accounting.integration.bootstrap.apply",
		event=None,
		job_name="v5625_bootstrap_probe",
		kwargs={},
		user="Administrator",
		is_async=False,
	)
	patched_after = bool(getattr(sl, "_iran_patched_update_entries_after", None))
	is_guard = riv_mod.execute_reposting_entry is guard.execute_reposting_entry
	not_nested = "<locals>" not in getattr(riv_mod.execute_reposting_entry, "__qualname__", "")
	wrapper_active = is_iran_riv_recalculate_wrapper_active()
	return {
		"scenario": "RIV_BOOTSTRAP",
		"patched_before_job_observation": was_patched,
		"patched_after_execute_job": patched_after,
		"execute_reposting_entry_is_guard": is_guard,
		"execute_reposting_entry_not_nested": not_nested,
		"recalculate_wrapper_active": wrapper_active,
		"pass": bool(patched_after and is_guard and not_nested and wrapper_active),
	}


def run():
	frappe.set_user("Administrator")
	from erpnext_extensions.iran_accounting.integration.bootstrap import apply as apply_iran

	apply_iran()
	frappe.db.set_single_value(
		"Buying Settings", "set_landed_cost_based_on_purchase_invoice_rate", 1
	)
	frappe.db.commit()
	ctx = _company_ctx()
	out = {
		"company": ctx["company"],
		"item": ctx["item"].name,
		"warehouse": ctx["warehouse"],
		"bootstrap": scenario_riv_bootstrap(),
		"control": scenario_control(ctx),
		"scenario_a": scenario_a(ctx),
		"scenario_b": scenario_b(ctx),
	}
	out["overall_pass"] = all(
		out[k].get("pass") for k in ("bootstrap", "control", "scenario_a", "scenario_b")
	)
	print(json.dumps(out, indent=2, default=str))
	return out


if __name__ == "__main__":
	run()

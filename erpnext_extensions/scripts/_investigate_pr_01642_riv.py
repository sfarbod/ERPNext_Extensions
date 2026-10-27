"""Read-only investigation of MAT-PRE-2026-01642 / RIV 7q5c056oc3 / HRC-00006."""

from __future__ import annotations

import json
from typing import Any

import frappe
from frappe.utils import flt


PR = "MAT-PRE-2026-01642"
RIV = "7q5c056oc3"
HRC = "HRC-00006"
ITEM = "13100023"


def _rows(sql, *args):
	return frappe.db.sql(sql, args, as_dict=True)


def inspect_pr() -> dict[str, Any]:
	if not frappe.db.exists("Purchase Receipt", PR):
		return {"exists": False}
	pr = frappe.get_doc("Purchase Receipt", PR)
	items = []
	for r in pr.items:
		items.append(
			{
				"idx": r.idx,
				"item_code": r.item_code,
				"qty": r.qty,
				"stock_qty": r.stock_qty,
				"rate": r.rate,
				"base_rate": r.base_rate,
				"amount": r.amount,
				"base_amount": r.base_amount,
				"net_rate": r.net_rate,
				"base_net_rate": r.base_net_rate,
				"net_amount": r.net_amount,
				"base_net_amount": r.base_net_amount,
				"valuation_rate": r.valuation_rate,
				"item_tax_amount": r.item_tax_amount,
				"landed_cost_voucher_amount": r.landed_cost_voucher_amount,
				"amount_difference_with_purchase_invoice": r.amount_difference_with_purchase_invoice,
				"billed_amt": r.billed_amt,
				"purchase_order": r.purchase_order,
				"purchase_invoice": r.purchase_invoice,
				"warehouse": r.warehouse,
				"uom": r.uom,
				"conversion_factor": r.conversion_factor,
				"is_fixed_asset": r.is_fixed_asset,
			}
		)
	return {
		"exists": True,
		"name": pr.name,
		"docstatus": pr.docstatus,
		"status": pr.status,
		"company": pr.company,
		"supplier": pr.supplier,
		"posting_date": str(pr.posting_date),
		"posting_time": str(pr.posting_time),
		"currency": pr.currency,
		"conversion_rate": pr.conversion_rate,
		"grand_total": pr.grand_total,
		"base_grand_total": pr.base_grand_total,
		"net_total": pr.net_total,
		"base_net_total": pr.base_net_total,
		"per_billed": pr.per_billed,
		"is_return": pr.is_return,
		"items": items,
	}


def inspect_sle() -> list[dict]:
	return _rows(
		"""select name, item_code, warehouse, actual_qty, incoming_rate, outgoing_rate,
		          valuation_rate, stock_value, stock_value_difference, qty_after_transaction,
		          voucher_detail_no, posting_date, posting_time, is_cancelled
		   from `tabStock Ledger Entry`
		   where voucher_type='Purchase Receipt' and voucher_no=%s
		   order by posting_date, posting_time, creation""",
		PR,
	)


def inspect_gl() -> list[dict]:
	return _rows(
		"""select name, account, debit, credit, against, cost_center, remarks,
		          is_cancelled, voucher_detail_no
		   from `tabGL Entry`
		   where voucher_type='Purchase Receipt' and voucher_no=%s
		   order by account, creation""",
		PR,
	)


def inspect_links() -> dict[str, Any]:
	pos = _rows(
		"""select distinct purchase_order from `tabPurchase Receipt Item`
		   where parent=%s and ifnull(purchase_order,'')!=''""",
		PR,
	)
	pis = _rows(
		"""select distinct parent as purchase_invoice, item_code, qty, rate, amount,
		          purchase_receipt, po_detail
		   from `tabPurchase Invoice Item`
		   where purchase_receipt=%s and docstatus=1""",
		PR,
	)
	# also PI via billed against
	pis2 = _rows(
		"""select parent as purchase_invoice, item_code, qty, rate, amount, purchase_receipt
		   from `tabPurchase Invoice Item`
		   where purchase_receipt=%s""",
		PR,
	)
	lcvs = _rows(
		"""select parent as lcv, receipt_document, receipt_document_type
		   from `tabLanded Cost Purchase Receipt`
		   where receipt_document=%s""",
		PR,
	)
	lcv_docs = []
	for row in lcvs:
		if frappe.db.exists("Landed Cost Voucher", row.lcv):
			lcv = frappe.get_doc("Landed Cost Voucher", row.lcv)
			lcv_docs.append(
				{
					"name": lcv.name,
					"docstatus": lcv.docstatus,
					"distribute_charges_based_on": lcv.distribute_charges_based_on,
					"total_taxes_and_charges": lcv.total_taxes_and_charges,
					"taxes": [
						{"expense_account": t.expense_account, "amount": t.amount, "description": t.description}
						for t in lcv.taxes
					],
					"items": [
						{
							"item_code": i.item_code,
							"qty": i.qty,
							"rate": i.rate,
							"amount": i.amount,
							"applicable_charges": i.applicable_charges,
						}
						for i in lcv.items
						if i.receipt_document == PR
					],
				}
			)
	return {"purchase_orders": pos, "purchase_invoices": pis or pis2, "landed_costs": lcv_docs}


def inspect_riv() -> dict[str, Any]:
	out: dict[str, Any] = {"requested": RIV, "exists": frappe.db.exists("Repost Item Valuation", RIV)}
	if out["exists"]:
		riv = frappe.get_doc("Repost Item Valuation", RIV)
		out.update(
			{
				"status": riv.status,
				"company": riv.company,
				"item_code": riv.item_code,
				"warehouse": riv.warehouse,
				"based_on": riv.based_on,
				"voucher_type": riv.get("voucher_type"),
				"voucher_no": riv.get("voucher_no"),
				"posting_date": str(riv.posting_date),
				"error_log": (riv.error_log or "")[:4000],
				"recreate_stock_ledgers": riv.get("recreate_stock_ledgers"),
				"allow_negative_stock": riv.allow_negative_stock,
			}
		)
	# related RIVs for item
	out["related"] = _rows(
		"""select name, status, item_code, warehouse, posting_date, modified, error_log
		   from `tabRepost Item Valuation`
		   where item_code=%s
		   order by modified desc limit 20""",
		ITEM,
	)
	for r in out["related"]:
		if r.get("error_log"):
			r["error_log"] = (r["error_log"] or "")[:500]
	return out


def inspect_hrc() -> dict[str, Any]:
	if not frappe.db.exists("Historical Rebuild Campaign", HRC):
		# try alternate doctype names
		alts = frappe.get_all(
			"DocType",
			filters={"name": ["like", "%Historical%Campaign%"]},
			pluck="name",
		)
		return {"exists": False, "alternates": alts}
	doc = frappe.get_doc("Historical Rebuild Campaign", HRC)
	fields = {
		k: doc.get(k)
		for k in (
			"name",
			"status",
			"company",
			"docstatus",
			"completed_identities",
			"verified_documents",
			"manifest_checksum",
			"current_identity",
			"failed_identity",
			"last_error",
			"progress_json",
		)
		if hasattr(doc, k) or doc.meta.has_field(k)
	}
	# dump useful meta
	fields["meta_fields"] = [df.fieldname for df in doc.meta.fields]
	# raw as_dict subset
	d = doc.as_dict()
	for k in list(d.keys()):
		if k.startswith("_") or k in ("creation", "modified", "owner", "modified_by", "idx"):
			continue
		if isinstance(d[k], (str, int, float, bool, type(None))):
			fields.setdefault("raw_" + k, d[k] if not isinstance(d[k], str) or len(d[k]) < 500 else d[k][:500])
	return {"exists": True, "fields": fields}


def inspect_accounts(company: str) -> dict[str, Any]:
	c = frappe.get_doc("Company", company)
	return {
		"default_currency": c.default_currency,
		"stock_adjustment_account": c.stock_adjustment_account,
		"stock_received_but_not_billed": c.stock_received_but_not_billed,
		"round_off_account": c.round_off_account,
		"round_off_cost_center": c.round_off_cost_center,
		"default_inventory_account": c.default_inventory_account,
		"precision": frappe.get_precision("Stock Ledger Entry", "stock_value_difference"),
		"currency_precision": frappe.get_precision("GL Entry", "debit"),
	}


def inspect_item() -> dict[str, Any]:
	if not frappe.db.exists("Item", ITEM):
		return {"exists": False}
	it = frappe.get_doc("Item", ITEM)
	return {
		"exists": True,
		"stock_uom": it.stock_uom,
		"valuation_method": it.valuation_method,
		"is_stock_item": it.is_stock_item,
		"has_batch_no": it.has_batch_no,
		"has_serial_no": it.has_serial_no,
	}


def compute_auth_math(pr_info: dict) -> list[dict]:
	from erpnext_extensions.iran_accounting.domain.irr_residual_classification import (
		purchase_receipt_stock_valuation_amount,
		purchase_receipt_valuation_stock_qty,
		classify_amount_rate_residual,
		classify_document_residuals,
		evaluate_irr_rate_rounding_residual,
	)
	from erpnext_extensions.iran_accounting.domain.currency import (
		round_monetary_rate,
		integer_valuation_rate_from_amount,
		get_currency_precision,
	)

	pr = frappe.get_doc("Purchase Receipt", PR)
	out = []
	for r in pr.items:
		if r.item_code != ITEM and ITEM:
			# include all but highlight target
			pass
		qty = purchase_receipt_valuation_stock_qty(r) or flt(r.qty)
		auth = purchase_receipt_stock_valuation_amount(r, pr)
		vr = flt(r.valuation_rate)
		qty_x_vr = flt(qty) * flt(vr)
		int_vr = flt(integer_valuation_rate_from_amount(auth, qty, "IRR")) if qty else None
		rounded_vr = flt(round_monetary_rate(vr, "IRR"))
		cls = classify_amount_rate_residual(auth, qty, vr, "IRR")
		out.append(
			{
				"item_code": r.item_code,
				"qty": qty,
				"auth_amount": auth,
				"valuation_rate": vr,
				"qty_x_vr": qty_x_vr,
				"auth_minus_qty_vr": flt(auth) - qty_x_vr,
				"integer_vr_from_auth": int_vr,
				"qty_x_int_vr": flt(qty) * flt(int_vr) if int_vr is not None else None,
				"auth_minus_qty_int_vr": flt(auth) - flt(qty) * flt(int_vr) if int_vr is not None else None,
				"rounded_existing_vr": rounded_vr,
				"qty_x_rounded_vr": flt(qty) * rounded_vr,
				"auth_minus_qty_rounded": flt(auth) - flt(qty) * rounded_vr,
				"classification": cls,
			}
		)
	a, b = classify_document_residuals(pr)
	decision = evaluate_irr_rate_rounding_residual(pr)
	return {
		"per_row": out,
		"class_a": a,
		"class_b": b,
		"decision": {
			"action": getattr(decision, "action", None) or decision.get("action") if isinstance(decision, dict) else str(decision),
			"repr": repr(decision)[:2000],
			"dict": decision.__dict__ if hasattr(decision, "__dict__") else decision,
		},
		"currency_precision": get_currency_precision("IRR"),
	}


def reproduce_gl_map() -> dict[str, Any]:
	"""Attempt Core GL regeneration path in dry fashion / catch residual error."""
	from erpnext_extensions.iran_accounting.integration.bootstrap import apply as apply_iran

	apply_iran()
	pr = frappe.get_doc("Purchase Receipt", PR)
	# Build gl map as Core would
	try:
		gl_map = pr.get_gl_entries()
	except Exception as e:
		return {"get_gl_entries_error": str(e)}
	# Apply residual
	from erpnext_extensions.iran_accounting.domain.irr_rounding_residual import (
		apply_irr_rate_rounding_residual_gl,
	)

	try:
		out = apply_irr_rate_rounding_residual_gl(pr, list(gl_map) if gl_map else [])
		return {
			"gl_map_count": len(gl_map or []),
			"gl_map": [
				{"account": g.account, "debit": g.debit, "credit": g.credit, "cost_center": g.cost_center}
				for g in (gl_map or [])
			],
			"after_residual_count": len(out or []),
			"after_residual": [
				{"account": g.account, "debit": g.debit, "credit": g.credit, "cost_center": getattr(g, "cost_center", None)}
				for g in (out or [])
			],
			"error": None,
		}
	except Exception as e:
		import traceback

		return {
			"gl_map_count": len(gl_map or []),
			"gl_map": [
				{"account": getattr(g, "account", None), "debit": getattr(g, "debit", None), "credit": getattr(g, "credit", None)}
				for g in (gl_map or [])
			],
			"error": str(e),
			"traceback": traceback.format_exc()[-3000:],
		}


def reproduce_riv_sync() -> dict[str, Any]:
	"""Reproduce RIV failure without committing — use savepoint."""
	from erpnext_extensions.iran_accounting.integration.bootstrap import apply as apply_iran

	apply_iran()
	if not frappe.db.exists("Repost Item Valuation", RIV):
		return {"error": "RIV missing"}
	frappe.db.begin()
	try:
		# mark Queued and execute
		frappe.db.set_value("Repost Item Valuation", RIV, "status", "Queued", update_modified=False)
		from erpnext.stock.doctype.repost_item_valuation.repost_item_valuation import (
			execute_reposting_entry,
		)

		try:
			execute_reposting_entry(RIV)
			status = frappe.db.get_value("Repost Item Valuation", RIV, ["status", "error_log"], as_dict=True)
			return {"executed": True, "status": status, "committed": False}
		except Exception as e:
			import traceback

			status = frappe.db.get_value("Repost Item Valuation", RIV, ["status", "error_log"], as_dict=True)
			return {
				"executed": True,
				"exception": str(e),
				"traceback": traceback.format_exc()[-4000:],
				"status": status,
			}
	finally:
		frappe.db.rollback()


def run():
	frappe.set_user("Administrator")
	pr = inspect_pr()
	if not pr.get("exists"):
		print(json.dumps({"error": "PR not found on development.localhost", "pr": PR}, indent=2))
		return {"error": "PR not found"}
	company = pr["company"]
	out = {
		"pr": pr,
		"item": inspect_item(),
		"accounts": inspect_accounts(company),
		"links": inspect_links(),
		"sle": inspect_sle(),
		"gl": inspect_gl(),
		"riv": inspect_riv(),
		"hrc": inspect_hrc(),
		"auth_math": compute_auth_math(pr),
		"gl_residual_probe": reproduce_gl_map(),
	}
	# GL totals
	gl = out["gl"]
	out["gl_totals"] = {
		"debit": sum(flt(r.debit) for r in gl if not r.is_cancelled),
		"credit": sum(flt(r.credit) for r in gl if not r.is_cancelled),
		"active_rows": sum(1 for r in gl if not r.is_cancelled),
	}
	sle = out["sle"]
	out["sle_totals"] = {
		"svd": sum(flt(r.stock_value_difference) for r in sle if not r.is_cancelled),
		"active_rows": sum(1 for r in sle if not r.is_cancelled),
	}
	print(json.dumps(out, indent=2, default=str))
	return out


if __name__ == "__main__":
	run()

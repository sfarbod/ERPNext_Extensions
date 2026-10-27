"""v5.6.25 real RQ worker RIV validation.

Creates an isolated PR → PI → Material Issue → Material Transfer chain,
queues Repost Item Valuation via the standard ERPNext enqueue path, waits for
the live bench worker to complete it, and asserts SLE/GL/GRNI + idempotency.

Does **not** call ``apply_monkey_patches()`` in the worker path — patches must
load via before_job / riv_execute_guard bootstrap only.
"""

from __future__ import annotations

import json
import time
from typing import Any

import frappe
from frappe.utils import flt, nowdate, now_datetime, add_to_date


TAG = "E2E_V5625_WORKER_RIV"


def _ctx() -> dict[str, Any]:
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
	wh2 = whs[1].name if len(whs) > 1 else wh
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
	expense = frappe.db.get_value(
		"Company", company, "default_expense_account"
	) or frappe.db.get_value(
		"Account", {"company": company, "root_type": "Expense", "is_group": 0}, "name"
	)
	return {
		"company": company,
		"warehouse": wh,
		"warehouse2": wh2,
		"item": item,
		"supplier": supplier,
		"cost_center": cc,
		"expense": expense,
		"department": "واحد انبار - E",
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
		warehouse=ctx["warehouse"],
		schedule_date=nowdate(),
		uom=ctx["item"].stock_uom,
		stock_uom=ctx["item"].stock_uom,
		conversion_factor=1,
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


def _pr_from_po(po_name, remarks, ctx):
	from erpnext.buying.doctype.purchase_order.purchase_order import make_purchase_receipt

	pr = make_purchase_receipt(po_name)
	if frappe.get_meta("Purchase Receipt").has_field("remarks"):
		pr.remarks = remarks
	if frappe.get_meta("Purchase Receipt").has_field("department") and not pr.get("department"):
		pr.department = ctx["department"]
	for row in pr.items:
		if frappe.get_meta("Purchase Receipt Item").has_field("department") and not row.get(
			"department"
		):
			row.department = ctx["department"]
		if not row.get("cost_center"):
			row.cost_center = ctx["cost_center"]
	pr.flags.ignore_permissions = True
	pr.insert()
	pr.submit()
	frappe.db.commit()
	return pr.name


def _pi_from_pr(pr_name, rate, remarks, ctx, bill_po=None):
	from erpnext.stock.doctype.purchase_receipt.purchase_receipt import make_purchase_invoice

	pi = make_purchase_invoice(pr_name)
	pi.posting_date = nowdate()
	pi.update_stock = 0
	pi.currency = "IRR"
	pi.conversion_rate = 1
	pi.set("taxes", [])
	if frappe.get_meta("Purchase Invoice").has_field("remarks"):
		pi.remarks = remarks
	if frappe.get_meta("Purchase Invoice").has_field("department") and not pi.get("department"):
		pi.department = ctx["department"]
	po_item = None
	if bill_po:
		po_item = frappe.db.get_value("Purchase Order Item", {"parent": bill_po}, "name")
	for row in pi.items:
		row.rate = rate
		if bill_po and po_item:
			row.purchase_order = bill_po
			row.po_detail = po_item
		if frappe.get_meta("Purchase Invoice Item").has_field("department") and not row.get(
			"department"
		):
			row.department = ctx["department"]
		if not row.get("cost_center"):
			row.cost_center = ctx["cost_center"]
	pi.flags.ignore_permissions = True
	pi.calculate_taxes_and_totals()
	pi.insert()
	pi.submit()
	frappe.db.commit()
	return pi.name


def _issue(ctx, qty, remarks):
	se = frappe.new_doc("Stock Entry")
	se.company = ctx["company"]
	se.stock_entry_type = "Material Issue"
	se.purpose = "Material Issue"
	se.posting_date = nowdate()
	se.set_posting_time = 1
	if frappe.get_meta("Stock Entry").has_field("remarks"):
		se.remarks = remarks
	if frappe.get_meta("Stock Entry").has_field("department"):
		se.department = ctx["department"]
	row = dict(
		item_code=ctx["item"].name,
		qty=qty,
		s_warehouse=ctx["warehouse"],
		uom=ctx["item"].stock_uom,
		stock_uom=ctx["item"].stock_uom,
		conversion_factor=1,
		cost_center=ctx["cost_center"],
		expense_account=ctx["expense"],
	)
	if frappe.get_meta("Stock Entry Detail").has_field("department"):
		row["department"] = ctx["department"]
	se.append("items", row)
	se.flags.ignore_permissions = True
	se.insert()
	se.submit()
	frappe.db.commit()
	return se.name


def _transfer(ctx, qty, remarks):
	se = frappe.new_doc("Stock Entry")
	se.company = ctx["company"]
	se.stock_entry_type = "Material Transfer"
	se.purpose = "Material Transfer"
	se.posting_date = nowdate()
	se.set_posting_time = 1
	if frappe.get_meta("Stock Entry").has_field("remarks"):
		se.remarks = remarks
	if frappe.get_meta("Stock Entry").has_field("department"):
		se.department = ctx["department"]
	row = dict(
		item_code=ctx["item"].name,
		qty=qty,
		s_warehouse=ctx["warehouse"],
		t_warehouse=ctx["warehouse2"],
		uom=ctx["item"].stock_uom,
		stock_uom=ctx["item"].stock_uom,
		conversion_factor=1,
		cost_center=ctx["cost_center"],
	)
	if frappe.get_meta("Stock Entry Detail").has_field("department"):
		row["department"] = ctx["department"]
	se.append("items", row)
	se.flags.ignore_permissions = True
	se.insert()
	se.submit()
	frappe.db.commit()
	return se.name


def _sle_rows(voucher_type, voucher_no):
	return frappe.db.sql(
		"""select name, warehouse, actual_qty, incoming_rate, outgoing_rate,
		          stock_value_difference, is_cancelled
		   from `tabStock Ledger Entry`
		   where voucher_type=%s and voucher_no=%s
		   order by creation""",
		(voucher_type, voucher_no),
		as_dict=True,
	)


def _gl_sum(voucher_type, voucher_no):
	rows = frappe.db.sql(
		"""select account, sum(debit) d, sum(credit) c, count(*) n
		   from `tabGL Entry`
		   where voucher_type=%s and voucher_no=%s and is_cancelled=0
		   group by account""",
		(voucher_type, voucher_no),
		as_dict=True,
	)
	return {
		"rows": rows,
		"debit": sum(flt(r.d) for r in rows),
		"credit": sum(flt(r.c) for r in rows),
		"entry_count": int(
			frappe.db.sql(
				"""select count(*) from `tabGL Entry`
				   where voucher_type=%s and voucher_no=%s and is_cancelled=0""",
				(voucher_type, voucher_no),
			)[0][0]
		),
	}


def _grni_net(company, vouchers):
	acc = frappe.db.get_value("Company", company, "stock_received_but_not_billed")
	if not acc:
		return None
	total = 0.0
	for vt, vn in vouchers:
		total += flt(
			frappe.db.sql(
				"""select coalesce(sum(debit)-sum(credit),0) from `tabGL Entry`
				   where company=%s and account=%s and voucher_type=%s and voucher_no=%s
				     and is_cancelled=0""",
				(company, acc, vt, vn),
			)[0][0]
		)
	return total


def _create_riv(ctx, item_code, warehouse, based_on="Item and Warehouse"):
	riv = frappe.new_doc("Repost Item Valuation")
	riv.company = ctx["company"]
	riv.based_on = based_on
	riv.item_code = item_code
	riv.warehouse = warehouse
	riv.posting_date = add_to_date(nowdate(), days=-1)
	riv.posting_time = "00:00:01"
	riv.allow_negative_stock = 1
	riv.status = "Queued"
	riv.insert()
	frappe.db.commit()
	return riv.name


def _enqueue_standard(riv_name):
	"""Standard ERPNext enqueue — must be picked by the live RQ worker."""
	from erpnext.stock.doctype.repost_item_valuation.repost_item_valuation import (
		enqueue_reposting_entry,
	)

	enqueue_reposting_entry(riv_name)
	frappe.db.commit()


def _wait_riv(riv_name, timeout=180):
	deadline = time.time() + timeout
	last = None
	while time.time() < deadline:
		last = frappe.db.get_value(
			"Repost Item Valuation",
			riv_name,
			["status", "error_log", "modified"],
			as_dict=True,
		)
		if last and last.status in ("Completed", "Failed", "Skipped"):
			return last
		time.sleep(1)
		frappe.db.commit()  # refresh read visibility
	return last


def _probe_worker_patches_via_job() -> dict[str, Any]:
	"""Enqueue a probe job; worker must report patches without caller apply."""
	cache_key = f"v5625_worker_probe_{int(time.time())}"
	job = frappe.enqueue(
		"erpnext_extensions.scripts._e2e_v5625_real_worker_riv.worker_patch_probe",
		queue="short",
		timeout=60,
		job_id=cache_key,
		enqueue_after_commit=False,
		cache_key=cache_key,
	)
	frappe.db.commit()
	deadline = time.time() + 60
	while time.time() < deadline:
		time.sleep(1)
		try:
			status = job.get_status(refresh=True)
		except Exception:
			status = None
		cached = frappe.cache().get_value(cache_key)
		if cached:
			return {"status": status or "finished", "result": cached, "job_id": job.id}
		if status in ("failed", "stopped", "canceled"):
			return {"status": status, "exc": str(getattr(job, "exc_info", None)), "job_id": job.id}
	return {"status": "timeout", "job_id": getattr(job, "id", None)}


def worker_patch_probe(cache_key: str | None = None):
	"""Runs inside RQ worker — report whether Iran patches are active."""
	from erpnext.stock import stock_ledger as sl
	import erpnext.stock.doctype.repost_item_valuation.repost_item_valuation as riv_mod
	from erpnext_extensions.iran_accounting.integration import riv_execute_guard as guard
	from erpnext_extensions.iran_accounting.domain.riv_valuation_guard import (
		is_iran_riv_recalculate_wrapper_active,
	)

	payload = {
		"sle_patched": bool(getattr(sl, "_iran_patched_update_entries_after", None)),
		"riv_is_guard": riv_mod.execute_reposting_entry is guard.execute_reposting_entry,
		"riv_qualname": getattr(riv_mod.execute_reposting_entry, "__qualname__", None),
		"riv_module": getattr(riv_mod.execute_reposting_entry, "__module__", None),
		"recalculate_wrapper_active": is_iran_riv_recalculate_wrapper_active(),
		# Must NOT be a nested locals wrap
		"not_nested_wrap": "<locals>" not in getattr(
			riv_mod.execute_reposting_entry, "__qualname__", ""
		),
	}
	if cache_key:
		frappe.cache().set_value(cache_key, payload, expires_in_sec=600)
	return payload


def run():
	frappe.set_user("Administrator")
	# Desk/console may already be bootstrapped; that is fine for *creating*
	# documents. The RIV job itself must rely on worker/guard bootstrap only.
	from erpnext_extensions.iran_accounting.integration.bootstrap import apply as apply_iran

	apply_iran()
	frappe.db.set_single_value(
		"Buying Settings", "set_landed_cost_based_on_purchase_invoice_rate", 1
	)
	frappe.db.commit()

	ctx = _ctx()
	qty, rate, pi_rate = 100, 1_000_000, 1_250_000
	expected_pr = qty * pi_rate

	# Cancel leftover draft from prior failed attempt if any
	for draft in frappe.get_all(
		"Purchase Receipt", filters={"docstatus": 0, "remarks": ["like", f"%{TAG}%"]}, pluck="name"
	):
		frappe.delete_doc("Purchase Receipt", draft, force=1)
		frappe.db.commit()

	po_recv = _po(ctx, qty, rate, f"{TAG} recv")
	pr = _pr_from_po(po_recv, f"{TAG} PR", ctx)
	po_bill = _po(ctx, qty, pi_rate, f"{TAG} bill")
	pi = _pi_from_pr(pr, pi_rate, f"{TAG} PI", ctx, bill_po=po_bill)

	# Downstream consumption / transfer after PI so RIV must revalue them
	issue = _issue(ctx, 10, f"{TAG} ISSUE")
	xfer = _transfer(ctx, 10, f"{TAG} XFER")

	pr_sle_before = _sle_rows("Purchase Receipt", pr)
	issue_sle_before = _sle_rows("Stock Entry", issue)
	xfer_sle_before = _sle_rows("Stock Entry", xfer)
	pr_gl_before = _gl_sum("Purchase Receipt", pr)
	issue_gl_before = _gl_sum("Stock Entry", issue)
	grni_before = _grni_net(
		ctx["company"],
		[("Purchase Receipt", pr), ("Purchase Invoice", pi)],
	)

	# Probe worker patch init without manual apply in the worker process
	probe = _probe_worker_patches_via_job()

	riv = _create_riv(ctx, ctx["item"].name, ctx["warehouse"])
	_enqueue_standard(riv)
	riv_status = _wait_riv(riv, timeout=180)

	pr_sle_after = _sle_rows("Purchase Receipt", pr)
	issue_sle_after = _sle_rows("Stock Entry", issue)
	xfer_sle_after = _sle_rows("Stock Entry", xfer)
	pr_gl_after = _gl_sum("Purchase Receipt", pr)
	issue_gl_after = _gl_sum("Stock Entry", issue)
	xfer_gl_after = _gl_sum("Stock Entry", xfer)
	grni_after = _grni_net(
		ctx["company"],
		[("Purchase Receipt", pr), ("Purchase Invoice", pi)],
	)

	pr_svd = sum(flt(r.stock_value_difference) for r in pr_sle_after if not r.is_cancelled)
	issue_svd = sum(flt(r.stock_value_difference) for r in issue_sle_after if not r.is_cancelled)
	xfer_out = [
		r
		for r in xfer_sle_after
		if not r.is_cancelled and flt(r.actual_qty) < 0
	]
	xfer_in = [
		r
		for r in xfer_sle_after
		if not r.is_cancelled and flt(r.actual_qty) > 0
	]
	xfer_balanced = False
	if xfer_out and xfer_in:
		xfer_balanced = abs(
			flt(xfer_out[0].stock_value_difference) + flt(xfer_in[0].stock_value_difference)
		) <= 1

	# Idempotent second RIV
	riv2 = _create_riv(ctx, ctx["item"].name, ctx["warehouse"])
	_enqueue_standard(riv2)
	riv2_status = _wait_riv(riv2, timeout=180)
	pr_sle_idem = _sle_rows("Purchase Receipt", pr)
	pr_gl_idem = _gl_sum("Purchase Receipt", pr)
	grni_idem = _grni_net(
		ctx["company"],
		[("Purchase Receipt", pr), ("Purchase Invoice", pi)],
	)
	pr_svd_idem = sum(flt(r.stock_value_difference) for r in pr_sle_idem if not r.is_cancelled)

	probe_ok = (
		probe.get("status") == "finished"
		and isinstance(probe.get("result"), dict)
		and probe["result"].get("sle_patched")
		and probe["result"].get("riv_is_guard")
		and probe["result"].get("recalculate_wrapper_active")
		and probe["result"].get("not_nested_wrap")
	)

	out = {
		"company": ctx["company"],
		"item": ctx["item"].name,
		"warehouse": ctx["warehouse"],
		"pr": pr,
		"pi": pi,
		"issue": issue,
		"xfer": xfer,
		"riv": riv,
		"riv_status": riv_status.status if riv_status else None,
		"riv_error": (riv_status.error_log if riv_status else None),
		"riv2": riv2,
		"riv2_status": riv2_status.status if riv2_status else None,
		"worker_probe": probe,
		"expected_pr_svd": expected_pr,
		"pr_svd_after": pr_svd,
		"issue_svd_after": issue_svd,
		"xfer_balanced": xfer_balanced,
		"xfer_out_svd": flt(xfer_out[0].stock_value_difference) if xfer_out else None,
		"xfer_in_svd": flt(xfer_in[0].stock_value_difference) if xfer_in else None,
		"pr_gl_balanced": abs(pr_gl_after["debit"] - pr_gl_after["credit"]) <= 1,
		"issue_gl_balanced": abs(issue_gl_after["debit"] - issue_gl_after["credit"]) <= 1,
		"xfer_gl_balanced": abs(xfer_gl_after["debit"] - xfer_gl_after["credit"]) <= 1,
		"grni_before": grni_before,
		"grni_after": grni_after,
		"grni_idem": grni_idem,
		"pr_gl_entry_count_after": pr_gl_after["entry_count"],
		"pr_gl_entry_count_idem": pr_gl_idem["entry_count"],
		"pr_svd_idem": pr_svd_idem,
		"sle_cancelled_noise": any(r.is_cancelled for r in pr_sle_idem),
		"pass": (
			probe_ok
			and riv_status
			and riv_status.status == "Completed"
			and riv2_status
			and riv2_status.status == "Completed"
			and abs(pr_svd - expected_pr) <= 1
			and abs(flt(grni_after)) <= 1
			and abs(flt(grni_idem)) <= 1
			and abs(pr_svd_idem - pr_svd) <= 1
			and pr_gl_idem["entry_count"] == pr_gl_after["entry_count"]
			and xfer_balanced
			and abs(pr_gl_after["debit"] - pr_gl_after["credit"]) <= 1
		),
	}
	print(json.dumps(out, indent=2, default=str))
	return out


if __name__ == "__main__":
	run()

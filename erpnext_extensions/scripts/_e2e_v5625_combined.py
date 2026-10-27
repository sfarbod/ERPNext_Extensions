"""Phase 3 combined E2E: PR 1000@1e6 → Issue 300 → Transfer 200 → PI@26900332 → LCV 1500."""
from __future__ import annotations
import json, time
import frappe
from frappe.utils import flt, nowdate, nowtime, cint

TAG = "E2E_V5625_COMBINED"
QTY, PR_RATE, PI_RATE = 1000, 1_000_000, 26_900_332
ISSUE_QTY, XFER_QTY, LCV_AMT = 300, 200, 1500
LCV_EXPENSE = "111606 - حساب کنترل هزینه های جانبی خرید کالای داخلی - E"

def _sle(vt, vn):
	return frappe.db.sql(
		"""select warehouse, actual_qty, incoming_rate, outgoing_rate, valuation_rate,
		          stock_value_difference from `tabStock Ledger Entry`
		   where voucher_type=%s and voucher_no=%s and is_cancelled=0
		   order by actual_qty""", (vt, vn), as_dict=True)

def _gl(vt, vn):
	return frappe.db.sql(
		"""select account, debit, credit from `tabGL Entry`
		   where voucher_type=%s and voucher_no=%s and is_cancelled=0 order by account""",
		(vt, vn), as_dict=True)

def _wait_item(item, timeout=300):
	deadline = time.time() + timeout
	while time.time() < deadline:
		active = cint(frappe.db.sql(
			"""select count(*) from `tabRepost Item Valuation`
			   where docstatus=1 and status in ('Queued','In Progress') and item_code=%s""",
			item)[0][0])
		if not active:
			return
		time.sleep(2); frappe.db.commit()

def run():
	from erpnext_extensions.iran_accounting.integration.bootstrap import apply as apply_iran
	apply_iran()
	company = frappe.db.get_value("Company", {"is_group": 0}, "name", order_by="creation asc")
	grni_acc = frappe.db.get_value("Company", company, "stock_received_but_not_billed")
	whs = frappe.db.sql(
		"select name from `tabWarehouse` where company=%s and is_group=0 and disabled=0 order by creation limit 5",
		company, as_dict=True)
	wh_src, wh_tgt = whs[0].name, whs[1].name
	items = frappe.db.sql(
		"""select i.name, i.stock_uom from `tabItem` i
		   where i.disabled=0 and i.is_stock_item=1 and ifnull(i.has_serial_no,0)=0
		     and ifnull(i.has_batch_no,0)=0 and i.is_fixed_asset=0
		   order by i.creation desc limit 100""", as_dict=True)
	item = None
	for it in items:
		bins = frappe.db.sql(
			"select actual_qty, stock_value from `tabBin` where item_code=%s and warehouse in (%s,%s)",
			(it.name, wh_src, wh_tgt), as_dict=True)
		if any(flt(b.actual_qty) or flt(b.stock_value) for b in bins):
			continue
		item = it; break
	item = item or items[0]
	supplier = frappe.db.get_value("Supplier", {"disabled": 0}, "name", order_by="creation asc")
	cc = frappe.db.get_value("Company", company, "cost_center") or frappe.db.get_value(
		"Cost Center", {"company": company, "is_group": 0}, "name")
	expense = frappe.db.get_value("Company", company, "default_expense_account") or frappe.db.get_value(
		"Account", {"company": company, "root_type": "Expense", "is_group": 0}, "name", order_by="lft asc")
	dept = "واحد انبار - E"

	def _po(rate, remarks):
		po = frappe.new_doc("Purchase Order")
		po.update(dict(company=company, supplier=supplier, transaction_date=nowdate(),
		               schedule_date=nowdate(), currency="IRR", conversion_rate=1))
		if frappe.get_meta("Purchase Order").has_field("remarks"):
			po.remarks = remarks
		row = dict(item_code=item.name, qty=QTY, rate=rate, uom=item.stock_uom, stock_uom=item.stock_uom,
		           conversion_factor=1, warehouse=wh_src, schedule_date=nowdate(), cost_center=cc)
		if frappe.get_meta("Purchase Order Item").has_field("department"):
			row["department"] = dept
		po.append("items", row)
		po.flags.ignore_permissions = True
		po.insert(); po.submit(); frappe.db.commit()
		return po.name

	recv_po = _po(PR_RATE, f"{TAG} recv PO")
	from erpnext.buying.doctype.purchase_order.purchase_order import make_purchase_receipt
	pr = make_purchase_receipt(recv_po)
	pr.posting_date = nowdate(); pr.posting_time = nowtime(); pr.set_posting_time = 1
	pr.currency = "IRR"; pr.conversion_rate = 1
	if frappe.get_meta("Purchase Receipt").has_field("remarks"):
		pr.remarks = f"{TAG} PR provisional"
	pr.set("taxes", [])
	for r in pr.items:
		r.qty = QTY; r.rate = PR_RATE; r.warehouse = wh_src
		if frappe.get_meta("Purchase Receipt Item").has_field("department"):
			r.department = dept
	pr.flags.ignore_permissions = True
	pr.set_missing_values()
	for r in pr.items:
		r.rate = PR_RATE
	pr.calculate_taxes_and_totals()
	pr.insert(); pr.submit(); frappe.db.commit()
	pr_name = pr.name
	# gate provisional SLE
	sle0 = _sle("Purchase Receipt", pr_name)
	assert abs(flt(sle0[0].incoming_rate) - PR_RATE) < 0.01, sle0

	# Issue
	se = frappe.new_doc("Stock Entry")
	se.update(dict(company=company, stock_entry_type="Material Issue", purpose="Material Issue",
	               posting_date=nowdate(), posting_time=nowtime(), set_posting_time=1))
	ir = dict(item_code=item.name, qty=ISSUE_QTY, uom=item.stock_uom, stock_uom=item.stock_uom,
	          conversion_factor=1, s_warehouse=wh_src, expense_account=expense, cost_center=cc)
	if frappe.get_meta("Stock Entry Detail").has_field("department"):
		ir["department"] = dept
	se.append("items", ir)
	se.flags.ignore_permissions = True
	se.insert(); se.submit(); frappe.db.commit()
	issue = se.name

	# Transfer
	se2 = frappe.new_doc("Stock Entry")
	se2.update(dict(company=company, stock_entry_type="Material Transfer", purpose="Material Transfer",
	                posting_date=nowdate(), posting_time=nowtime(), set_posting_time=1))
	xr = dict(item_code=item.name, qty=XFER_QTY, uom=item.stock_uom, stock_uom=item.stock_uom,
	          conversion_factor=1, s_warehouse=wh_src, t_warehouse=wh_tgt, cost_center=cc)
	if frappe.get_meta("Stock Entry Detail").has_field("department"):
		xr["department"] = dept
	se2.append("items", xr)
	se2.flags.ignore_permissions = True
	se2.insert(); se2.submit(); frappe.db.commit()
	xfer = se2.name

	# Billing PO at PI rate (site Lock PO Rate + overbill vs PO)
	bill_po = _po(PI_RATE, f"{TAG} billing PO")
	from erpnext.stock.doctype.purchase_receipt.purchase_receipt import make_purchase_invoice
	pi = make_purchase_invoice(pr_name)
	pi.posting_date = nowdate(); pi.posting_time = nowtime(); pi.set_posting_time = 1
	pi.update_stock = 0; pi.currency = "IRR"; pi.conversion_rate = 1
	pi.remarks = f"{TAG} PI valuation"
	pi.set("taxes", [])
	po_item = frappe.db.get_value("Purchase Order Item", {"parent": bill_po}, "name")
	for r in pi.items:
		r.qty = QTY; r.rate = PI_RATE
		r.purchase_order = bill_po; r.po_detail = po_item
		if frappe.get_meta("Purchase Invoice Item").has_field("department"):
			r.department = dept
	pi.flags.ignore_permissions = True
	pi.calculate_taxes_and_totals()
	pi.insert(); pi.submit(); frappe.db.commit()
	pi_name = pi.name
	_wait_item(item.name, 360)

	# LCV
	lcv = frappe.new_doc("Landed Cost Voucher")
	lcv.company = company; lcv.posting_date = nowdate(); lcv.distribute_charges_based_on = "Qty"
	lcv.append("purchase_receipts", dict(receipt_document_type="Purchase Receipt", receipt_document=pr_name))
	lcv.get_items_from_purchase_receipts()
	acc = LCV_EXPENSE if frappe.db.exists("Account", LCV_EXPENSE) else frappe.db.get_value(
		"Account", {"company": company, "is_group": 0, "root_type": "Expense"}, "name")
	lcv.append("taxes", dict(expense_account=acc, description=f"{TAG} LCV", amount=LCV_AMT))
	lcv.flags.ignore_permissions = True
	lcv.validate(); lcv.insert(); lcv.submit(); frappe.db.commit()
	lcv_name = lcv.name
	_wait_item(item.name, 360)

	# Capture
	pr_item = frappe.db.get_value(
		"Purchase Receipt Item", {"parent": pr_name},
		["valuation_rate", "landed_cost_voucher_amount", "base_net_amount",
		 "amount_difference_with_purchase_invoice", "billed_amt"], as_dict=True)
	sle_pr = _sle("Purchase Receipt", pr_name)
	sle_issue = _sle("Stock Entry", issue)
	sle_xfer = _sle("Stock Entry", xfer)
	gl_pr = _gl("Purchase Receipt", pr_name)
	gl_pi = _gl("Purchase Invoice", pi_name)
	gl_issue = _gl("Stock Entry", issue)
	bins = {
		"src": frappe.db.get_value("Bin", {"item_code": item.name, "warehouse": wh_src},
		                          ["actual_qty", "stock_value", "valuation_rate"], as_dict=True),
		"tgt": frappe.db.get_value("Bin", {"item_code": item.name, "warehouse": wh_tgt},
		                          ["actual_qty", "stock_value", "valuation_rate"], as_dict=True),
	}
	nos = [pr_name, pi_name, lcv_name, issue, xfer]
	ph = ",".join(["%s"] * len(nos))
	grni = frappe.db.sql(
		f"""select ifnull(sum(debit),0) d, ifnull(sum(credit),0) c,
		          ifnull(sum(debit),0)-ifnull(sum(credit),0) bal
		   from `tabGL Entry` where is_cancelled=0 and account=%s and voucher_no in ({ph})""",
		tuple([grni_acc] + nos), as_dict=True)[0]

	pr_svd = sum(flt(s.stock_value_difference) for s in sle_pr)
	issue_svd = abs(sum(flt(s.stock_value_difference) for s in sle_issue))
	xfer_out = abs(sum(flt(s.stock_value_difference) for s in sle_xfer if flt(s.actual_qty) < 0))
	xfer_in = abs(sum(flt(s.stock_value_difference) for s in sle_xfer if flt(s.actual_qty) > 0))
	remaining = flt(bins["src"].stock_value if bins["src"] else 0) + flt(bins["tgt"].stock_value if bins["tgt"] else 0)

	# Expected: total acquisition = PI total + LCV; remaining qty 700
	acq = PI_RATE * QTY + LCV_AMT
	# Per-unit effective after LCV (amount-preserving): acq/1000
	unit = acq / QTY
	exp_issue = unit * ISSUE_QTY
	exp_xfer = unit * XFER_QTY
	exp_remaining = unit * (QTY - ISSUE_QTY)

	checks = [
		("PI submitted (no overbill block)", True, True),
		("PR SLE acquisition", acq, pr_svd),
		("amount_difference", (PI_RATE - PR_RATE) * QTY, pr_item.amount_difference_with_purchase_invoice),
		("LCV amount on PR", LCV_AMT, pr_item.landed_cost_voucher_amount),
		("Material Issue |svd|", exp_issue, issue_svd),
		("Transfer out", exp_xfer, xfer_out),
		("Transfer in", exp_xfer, xfer_in),
		("Remaining inventory", exp_remaining, remaining),
		("GRNI balance", 0, grni.bal),
		("Transfer balanced", 0, abs(xfer_out - xfer_in)),
	]
	matrix = []
	overall = "PASS"
	for name, exp, act in checks:
		if isinstance(exp, bool):
			ok = exp is act
		else:
			ok = abs(flt(exp) - flt(act)) <= 2.0  # allow 1-2 IRR float noise
		matrix.append(dict(name=name, expected=exp, actual=act, result="PASS" if ok else "FAIL"))
		if not ok:
			overall = "FAIL"

	out = dict(
		overall=overall,
		docs=dict(recv_po=recv_po, bill_po=bill_po, pr=pr_name, issue=issue, xfer=xfer, pi=pi_name, lcv=lcv_name),
		pr_item=pr_item, sle_pr=sle_pr, sle_issue=sle_issue, sle_xfer=sle_xfer,
		gl_pr=gl_pr, gl_pi=gl_pi, gl_issue=gl_issue, bins=bins, grni=grni,
		expected=dict(acq=acq, unit=unit, issue=exp_issue, xfer=exp_xfer, remaining=exp_remaining),
		matrix=matrix, item=item.name, warehouses=dict(src=wh_src, tgt=wh_tgt),
	)
	path = frappe.get_site_path("private", "files", f"{TAG}_REPORT.json")
	with open(path, "w") as f:
		json.dump(out, f, default=str, indent=2)
	print("OVERALL", overall)
	print("DOCS", json.dumps(out["docs"]))
	print("EXPECTED", out["expected"])
	print("PR_ITEM", pr_item)
	print("GRNI", grni)
	print("BINS", bins)
	for m in matrix:
		print(f"  [{m['result']}] {m['name']}: expected={m['expected']} actual={m['actual']}")
	print("REPORT", path)
	return out


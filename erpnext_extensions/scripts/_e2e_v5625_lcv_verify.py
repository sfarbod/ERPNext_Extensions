"""Phase 2 E2E: PR 800@1e6 → PI same → LCV 1500. Assert GRNI=0 and SLE=800001500."""
from __future__ import annotations
import json, time
import frappe
from frappe.utils import flt, nowdate, nowtime, cint

TAG = "E2E_V5625_LCV_FIX4"
QTY, RATE, LCV_AMT = 800, 1_000_000, 1500
LCV_EXPENSE = "111606 - حساب کنترل هزینه های جانبی خرید کالای داخلی - E"

def _sle(vt, vn):
	return frappe.db.sql(
		"""select actual_qty, incoming_rate, valuation_rate, stock_value_difference, warehouse
		   from `tabStock Ledger Entry`
		   where voucher_type=%s and voucher_no=%s and is_cancelled=0""",
		(vt, vn), as_dict=True,
	)

def _gl(vt, vn):
	return frappe.db.sql(
		"""select account, debit, credit, remarks from `tabGL Entry`
		   where voucher_type=%s and voucher_no=%s and is_cancelled=0 order by account""",
		(vt, vn), as_dict=True,
	)

def _grni(account, nos):
	ph = ",".join(["%s"] * len(nos))
	return frappe.db.sql(
		f"""select ifnull(sum(debit),0) d, ifnull(sum(credit),0) c,
		          ifnull(sum(debit),0)-ifnull(sum(credit),0) bal
		   from `tabGL Entry` where is_cancelled=0 and account=%s and voucher_no in ({ph})""",
		tuple([account] + list(nos)), as_dict=True,
	)[0]

def run():
	from erpnext_extensions.iran_accounting.integration.bootstrap import apply as apply_iran
	apply_iran()
	company = frappe.db.get_value("Company", {"is_group": 0}, "name", order_by="creation asc")
	grni = frappe.db.get_value("Company", company, "stock_received_but_not_billed")
	wh = frappe.db.sql(
		"select name from `tabWarehouse` where company=%s and is_group=0 and disabled=0 order by creation limit 1",
		company,
	)[0][0]
	items = frappe.db.sql(
		"""select i.name, i.stock_uom from `tabItem` i
		   where i.disabled=0 and i.is_stock_item=1 and ifnull(i.has_serial_no,0)=0
		     and ifnull(i.has_batch_no,0)=0 and i.is_fixed_asset=0
		   order by i.creation desc limit 80""",
		as_dict=True,
	)
	item = None
	for it in items:
		b = frappe.db.sql(
			"select actual_qty, stock_value from `tabBin` where item_code=%s and warehouse=%s",
			(it.name, wh), as_dict=True,
		)
		if b and (flt(b[0].actual_qty) or flt(b[0].stock_value)):
			continue
		item = it
		break
	item = item or items[0]
	supplier = frappe.db.get_value("Supplier", {"disabled": 0}, "name", order_by="creation asc")
	cc = frappe.db.get_value("Company", company, "cost_center") or frappe.db.get_value(
		"Cost Center", {"company": company, "is_group": 0}, "name"
	)
	dept = "واحد انبار - E"

	# PO
	po = frappe.new_doc("Purchase Order")
	po.update(dict(company=company, supplier=supplier, transaction_date=nowdate(), schedule_date=nowdate(), currency="IRR", conversion_rate=1))
	if frappe.get_meta("Purchase Order").has_field("remarks"):
		po.remarks = f"{TAG} PO"
	row = dict(item_code=item.name, qty=QTY, rate=RATE, uom=item.stock_uom, stock_uom=item.stock_uom,
	           conversion_factor=1, warehouse=wh, schedule_date=nowdate(), cost_center=cc)
	if frappe.get_meta("Purchase Order Item").has_field("department"):
		row["department"] = dept
	po.append("items", row)
	po.flags.ignore_permissions = True
	po.insert(); po.submit(); frappe.db.commit()

	from erpnext.buying.doctype.purchase_order.purchase_order import make_purchase_receipt
	pr = make_purchase_receipt(po.name)
	pr.posting_date = nowdate(); pr.posting_time = nowtime(); pr.set_posting_time = 1
	pr.currency = "IRR"; pr.conversion_rate = 1
	if frappe.get_meta("Purchase Receipt").has_field("remarks"):
		pr.remarks = f"{TAG} PR"
	pr.set("taxes", [])
	for r in pr.items:
		r.qty = QTY; r.rate = RATE; r.warehouse = wh
		if frappe.get_meta("Purchase Receipt Item").has_field("department"):
			r.department = dept
	pr.flags.ignore_permissions = True
	pr.set_missing_values()
	for r in pr.items:
		r.rate = RATE
	pr.calculate_taxes_and_totals()
	pr.insert(); pr.submit(); frappe.db.commit()
	pr_name = pr.name

	from erpnext.stock.doctype.purchase_receipt.purchase_receipt import make_purchase_invoice
	pi = make_purchase_invoice(pr_name)
	pi.posting_date = nowdate(); pi.posting_time = nowtime(); pi.set_posting_time = 1
	pi.update_stock = 0; pi.currency = "IRR"; pi.conversion_rate = 1
	pi.remarks = f"{TAG} PI"
	pi.set("taxes", [])
	for r in pi.items:
		r.qty = QTY; r.rate = RATE
		if frappe.get_meta("Purchase Invoice Item").has_field("department"):
			r.department = dept
	pi.flags.ignore_permissions = True
	pi.calculate_taxes_and_totals()
	pi.insert(); pi.submit(); frappe.db.commit()
	pi_name = pi.name

	# LCV
	lcv = frappe.new_doc("Landed Cost Voucher")
	lcv.company = company; lcv.posting_date = nowdate(); lcv.distribute_charges_based_on = "Qty"
	lcv.append("purchase_receipts", dict(receipt_document_type="Purchase Receipt", receipt_document=pr_name))
	lcv.get_items_from_purchase_receipts()
	acc = LCV_EXPENSE if frappe.db.exists("Account", LCV_EXPENSE) else frappe.db.get_value(
		"Account", {"company": company, "is_group": 0, "root_type": "Expense"}, "name"
	)
	lcv.append("taxes", dict(expense_account=acc, description=f"{TAG} 1500", amount=LCV_AMT))
	lcv.flags.ignore_permissions = True
	lcv.validate()
	lcv.insert(); lcv.submit(); frappe.db.commit()
	lcv_name = lcv.name

	# wait riv for item
	deadline = time.time() + 120
	while time.time() < deadline:
		active = cint(frappe.db.sql(
			"""select count(*) from `tabRepost Item Valuation`
			   where docstatus=1 and status in ('Queued','In Progress') and item_code=%s""",
			item.name,
		)[0][0])
		if not active:
			break
		time.sleep(2); frappe.db.commit()

	pr_item = frappe.db.get_value(
		"Purchase Receipt Item", {"parent": pr_name},
		["valuation_rate", "landed_cost_voucher_amount", "base_net_amount", "amount_difference_with_purchase_invoice"],
		as_dict=True,
	)
	sle = _sle("Purchase Receipt", pr_name)
	gl_pr = _gl("Purchase Receipt", pr_name)
	gl_pi = _gl("Purchase Invoice", pi_name)
	grni = _grni(grni, [pr_name, pi_name, lcv_name])
	sle_svd = sum(flt(s.stock_value_difference) for s in sle)
	grni_pr = sum(flt(g.credit) - flt(g.debit) for g in gl_pr if g.account == grni or True)
	# fix grni filter
	grni_acc = frappe.db.get_value("Company", company, "stock_received_but_not_billed")
	grni_pr_net = sum(flt(g.credit) - flt(g.debit) for g in gl_pr if g.account == grni_acc)
	grni_pi_net = sum(flt(g.debit) - flt(g.credit) for g in gl_pi if g.account == grni_acc)

	checks = [
		("SLE acquisition total", RATE * QTY + LCV_AMT, sle_svd),
		("PR item LCV amount", LCV_AMT, pr_item.landed_cost_voucher_amount),
		("GRNI chain balance", 0, grni.bal),
		("GRNI PR credit == PI debit", grni_pi_net, grni_pr_net),
	]
	matrix = []
	overall = "PASS"
	for name, exp, act in checks:
		ok = abs(flt(exp) - flt(act)) <= 1.0
		matrix.append(dict(name=name, expected=exp, actual=act, result="PASS" if ok else "FAIL"))
		if not ok:
			overall = "FAIL"

	out = dict(
		overall=overall, docs=dict(po=po.name, pr=pr_name, pi=pi_name, lcv=lcv_name),
		pr_item=pr_item, sle=sle, gl_pr=gl_pr, gl_pi=gl_pi, grni=grni, matrix=matrix,
	)
	path = frappe.get_site_path("private", "files", f"{TAG}_REPORT.json")
	with open(path, "w") as f:
		json.dump(out, f, default=str, indent=2)
	print("OVERALL", overall)
	print("DOCS", out["docs"])
	print("PR_ITEM VR", pr_item.valuation_rate, "LCV", pr_item.landed_cost_voucher_amount)
	print("SLE_SVD", sle_svd)
	print("GRNI", grni)
	for m in matrix:
		print(f"  [{m['result']}] {m['name']}: expected={m['expected']} actual={m['actual']}")
	print("GL_PR")
	for g in gl_pr:
		print(" ", g["account"], "D", g["debit"], "C", g["credit"])
	return out


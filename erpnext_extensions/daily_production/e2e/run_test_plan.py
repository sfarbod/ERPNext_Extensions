# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""End-to-end test plan for Daily Production Log, driven over REST against a site
(staging). Runs the whole Alcarisa-28 batch (BOM-20100067-017, 2756 units) only
through Daily Production Logs, then the six failure cases, idempotency and timing.

    ERP_URL=https://erpstage... ERP_API_KEY=... ERP_API_SECRET=... \
        python -m erpnext_extensions.daily_production.e2e.run_test_plan [--bom BOM-20100067-017]

Prints a report in the same shape as the manual test (per-op planned/actual/booked,
valuation chain, Job Card count) and exits non-zero if any expectation fails.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("ERP_URL", "").rstrip("/")
TOKEN = f"token {os.environ.get('ERP_API_KEY', '')}:{os.environ.get('ERP_API_SECRET', '')}"
HDR = {"Authorization": TOKEN, "Content-Type": "application/json", "Accept": "application/json"}

COMPANY = "اسپاد فارمد دارو"
FG_WH = "FG - Test - E"
WIP_WH = "WIP Filling - Test - E"
EMP1 = os.environ.get("DPL_EMP1", "HR-EMP-0537")
EMP2 = os.environ.get("DPL_EMP2", "HR-EMP-0538")
DAYS = ["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04", "2026-09-05"]

failures: list[str] = []


class ERPError(Exception):
	pass


def req(method, path, params=None, body=None):
	url = BASE + path
	if params:
		url += "?" + urllib.parse.urlencode(
			{k: json.dumps(v) if isinstance(v, (list, dict)) else v for k, v in params.items()}
		)
	data = json.dumps(body).encode() if body is not None else None
	try:
		for attempt in range(3):
			try:
				with urllib.request.urlopen(
					urllib.request.Request(url, data=data, headers=HDR, method=method), timeout=300
				) as r:
					return json.loads(r.read())
			except urllib.error.HTTPError:
				raise
			except (urllib.error.URLError, OSError):  # transient TLS/network hiccup
				if attempt == 2:
					raise
				time.sleep(2)
	except urllib.error.HTTPError as e:
		txt = e.read().decode(errors="replace")
		try:
			j = json.loads(txt)
			msgs = [json.loads(m).get("message") for m in json.loads(j.get("_server_messages") or "[]")]
			raise ERPError(f"HTTP {e.code}: {j.get('exception', '')[:400]} | {msgs}")
		except ERPError:
			raise
		except Exception:
			raise ERPError(f"HTTP {e.code}: {txt[:400]}") from None


def get_doc(dt, name):
	return req("GET", f"/api/resource/{urllib.parse.quote(dt)}/{urllib.parse.quote(name)}")["data"]


def get_list(dt, fields, filters=None, limit=100, order_by=None, parent=None):
	p = {"fields": fields, "limit_page_length": limit}
	if filters:
		p["filters"] = filters
	if order_by:
		p["order_by"] = order_by
	if parent:
		p["parent"] = parent
	return req("GET", f"/api/resource/{urllib.parse.quote(dt)}", p)["data"]


def insert(doc):
	return req("POST", "/api/method/frappe.client.insert", body={"doc": doc})["message"]


def save(doc):
	return req("POST", "/api/method/frappe.client.save", body={"doc": doc})["message"]


def submit(doc):
	return req("POST", "/api/method/frappe.client.submit", body={"doc": doc})["message"]


def call(method, **kw):
	return req("POST", f"/api/method/{method}", body=kw).get("message")


def run_doc_method(dt, name, method, args=None):
	return req(
		"POST",
		"/api/method/run_doc_method",
		body={"dt": dt, "dn": name, "method": method, "args": json.dumps(args or {})},
	)


def check(cond, msg):
	print(("  PASS  " if cond else "  FAIL  ") + msg)
	if not cond:
		failures.append(msg)


# ----------------------------------------------------------------------------- setup
def make_work_order(bom):
	wo = insert(
		{
			"doctype": "Work Order",
			"production_item": get_doc("BOM", bom)["item"],
			"bom_no": bom,
			"qty": 2756,
			"company": COMPANY,
			"fg_warehouse": FG_WH,
			"wip_warehouse": WIP_WH,
			"use_multi_level_bom": 0,
			"skip_transfer": 0,
			"transfer_material_against": "Job Card",
			"custom_batch_size": "2756",
			"custom_fg_batch_no": "DPLTEST",
		}
	)
	r = run_doc_method("Work Order", wo["name"], "get_items_and_operations_from_bom")
	doc = next(x for x in r["docs"] if x["doctype"] == "Work Order")
	doc = save(doc)
	doc = submit(doc)
	print(
		f"Work Order {doc['name']} submitted: ops={len(doc['operations'])} items={len(doc['required_items'])}"
	)
	return doc


def new_log(wo, op_idx, qty, emp, day, t_from, t_to, batch=None):
	ops = get_doc("Work Order", wo)["operations"]
	op = ops[op_idx - 1]
	doc = insert(
		{
			"doctype": "Daily Production Log",
			"work_order": wo,
			"operation": op["operation"],
			"operation_row_id": op["idx"],
			"qty": qty,
			"employee": emp,
			"from_time": f"{day} {t_from}:00",
			"to_time": f"{day} {t_to}:00",
			"output_batch_no": batch,
		}
	)
	return doc["name"]


def run_log(name):
	t0 = time.monotonic()
	try:
		r = call("erpnext_extensions.daily_production.runner.run", name=name)
		ok = True
	except ERPError as e:
		r = str(e)
		ok = False
	doc = get_doc("Daily Production Log", name)
	print(
		f"  {name}: status={doc['status']} jc={doc.get('job_card')} xfer={doc.get('transfer_stock_entry')} mfg={doc.get('manufacture_stock_entry')} {doc.get('duration_seconds')}s wall={time.monotonic() - t0:.1f}s{'' if ok else ' | ' + str(r)[:160]}"
	)
	return doc


def expect_done(name):
	doc = run_log(name)
	check(doc["status"] == "Done", f"{name} Done")
	check(doc.get("duration_seconds", 999) < 10, f"{name} under 10 s ({doc.get('duration_seconds')})")
	return doc


def expect_failed(name, why):
	doc = run_log(name)
	check(doc["status"] == "Failed", f"{name} Failed ({why})")
	check(not doc.get("job_card") and not doc.get("manufacture_stock_entry"), f"{name} created nothing")
	return doc


# ----------------------------------------------------------------------------- plan
def main():
	ap = argparse.ArgumentParser()
	ap.add_argument("--bom", default="BOM-20100067-017")
	ap.add_argument("--work-order", help="reuse an existing submitted WO instead of creating one")
	args = ap.parse_args()
	if not BASE:
		sys.exit("set ERP_URL / ERP_API_KEY / ERP_API_SECRET")

	wo = args.work_order or make_work_order(args.bom)["name"]
	fg_item = get_doc("Work Order", wo)["production_item"]
	fg_batch = f"DPL-{fg_item}-{wo[-5:]}"
	if not get_list("Batch", ["name"], [["name", "=", fg_batch]]):
		insert(
			{
				"doctype": "Batch",
				"item": fg_item,
				"custom_batch_no": f"DPLTEST-{wo[-5:]}",
				"batch_id": fg_batch,
				"manufacturing_date": DAYS[0],
				"expiry_date": "2028-09-01",
				"reference_doctype": "Work Order",
				"reference_name": wo,
			}
		)
	print("FG batch", fg_batch)

	print("\n== Failure cases first (nothing may be created) ==")
	expect_failed(new_log(wo, 1, 3000, EMP1, DAYS[0], "08:00", "09:00"), "qty > pending")
	try:
		new_log(wo, 1, 100, EMP1, DAYS[0], "10:00", "09:00")
		check(False, "to_time < from_time rejected at save")
	except ERPError as e:
		check("after From" in str(e) or "To time" in str(e), "to_time < from_time rejected at save")
	expect_failed(new_log(wo, 4, 600, EMP2, DAYS[0], "08:00", "09:00"), "op 4 for 600 with 0 inspected units")

	print("\n== Day 1/2: op 1 twice (1378 + 1378) ==")
	expect_done(new_log(wo, 1, 1378, EMP1, DAYS[0], "08:00", "17:00"))
	expect_done(new_log(wo, 1, 1378, EMP1, DAYS[1], "08:00", "16:00"))

	print("\n== Day 3: op 2 (2756), op 3 lot 1 (500), op 3 lot 2 started via a manual timer, op 4/5 lot 1 ==")
	expect_done(new_log(wo, 2, 2756, EMP1, DAYS[2], "08:00", "10:00"))
	expect_done(new_log(wo, 3, 500, EMP1, DAYS[2], "11:00", "13:30"))
	# op 4 for 600 now that only 500 are inspected
	expect_failed(new_log(wo, 4, 600, EMP2, DAYS[2], "13:30", "13:45"), "op 4 for 600 with 500 inspected")
	# "op 3 lot 2 is open": create its card through the WO dialog and start EMP1's timer on it —
	# that is the state an operator leaves behind between shifts. The same timer is the
	# "employee with an open timer elsewhere" failure case for op 4.
	ops = get_doc("Work Order", wo)["operations"]
	op3 = ops[2]
	call(
		"erpnext.manufacturing.doctype.work_order.work_order.make_job_card",
		work_order=wo,
		operations=[
			{
				"name": op3["name"],
				"operation": op3["operation"],
				"workstation": op3["workstation"],
				"qty": 1500,
				"pending_qty": 2256,
			}
		],
		parent_bom=get_doc("Work Order", wo)["bom_no"],
	)
	open_card = get_list(
		"Job Card",
		["name"],
		[["work_order", "=", wo], ["operation_id", "=", op3["name"]], ["docstatus", "=", 0]],
		order_by="creation desc",
	)[0]["name"]
	run_doc_method(
		"Job Card",
		open_card,
		"start_timer",
		{"start_time": f"{DAYS[3]} 08:00:00", "employees": [{"employee": EMP1}]},
	)
	print("  open op-3 lot-2 card with running timer:", open_card)
	expect_failed(new_log(wo, 4, 500, EMP1, DAYS[2], "14:00", "16:00"), "employee with open timer elsewhere")
	expect_done(new_log(wo, 4, 500, EMP2, DAYS[2], "14:00", "16:00"))
	expect_done(new_log(wo, 5, 500, EMP2, DAYS[2], "16:00", "17:00", fg_batch))
	still_open = get_doc("Job Card", open_card)
	check(still_open["docstatus"] == 0, "op-3 lot-2 card still open while ops 4/5 of lot 1 ran")

	print("\n== concurrency: two logs for the same WO+op run at once ==")
	# finish the open card through the log (the runner reuses an untouched card only; this one has a
	# timer, so we close the manual timer first to keep the scenario clean)
	run_doc_method(
		"Job Card",
		open_card,
		"complete_job_card",
		{
			"qty": 1500,
			"for_quantity": 1500,
			"pending_qty": 0,
			"process_loss_qty": 0,
			"end_time": f"{DAYS[3]} 11:00:00",
		},
	)
	submit(get_doc("Job Card", open_card))
	r = run_doc_method("Job Card", open_card, "make_stock_entry_for_semi_fg_item", {"auto_submit": 0})
	se = get_doc("Stock Entry", r["message"]["name"])
	for it in se["items"]:
		it["expense_account"] = "621301 - تعدیلات موجودی کالا - E"
	se = save(se)
	# finished good needs a lot: give the manual card a manual lot
	fgrow = next(i for i in se["items"] if i.get("is_finished_item"))
	lot = insert(
		{
			"doctype": "Batch",
			"item": fgrow["item_code"],
			"custom_batch_no": f"DPLTEST-{wo[-5:]}-MAN",
			"batch_id": f"DPL-{fgrow['item_code']}-{wo[-5:]}-MAN",
			"manufacturing_date": DAYS[3],
			"expiry_date": "2028-09-01",
			"reference_doctype": "Work Order",
			"reference_name": wo,
		}
	)["name"]
	fgrow["use_serial_batch_fields"] = 1
	fgrow["batch_no"] = lot
	submit(save(se))
	print("  manual op-3 lot 2 closed:", open_card, se["name"])
	a = new_log(wo, 4, 1500, EMP2, DAYS[3], "11:00", "13:00")
	b = new_log(wo, 4, 1500, EMP1, DAYS[3], "11:00", "13:00")
	with concurrent.futures.ThreadPoolExecutor(2) as ex:
		fa, fb = ex.submit(run_log, a), ex.submit(run_log, b)
		da, db = fa.result(), fb.result()
	statuses = sorted([da["status"], db["status"]])
	check(statuses == ["Done", "Failed"], f"concurrent runs → one Done, one Failed ({statuses})")
	failed = a if da["status"] == "Failed" else b
	check(
		not get_doc("Daily Production Log", failed).get("job_card"),
		"the Failed concurrent log created nothing",
	)

	print("\n== re-run of a Failed log after fixing the input ==")
	fixed = get_doc("Daily Production Log", failed)
	fixed["qty"] = 756
	fixed["employee"] = EMP2
	fixed["from_time"] = f"{DAYS[4]} 10:00:00"
	fixed["to_time"] = f"{DAYS[4]} 11:30:00"
	# needs op 3 lot 3 first
	expect_done(new_log(wo, 3, 756, EMP1, DAYS[4], "08:00", "10:00"))
	save(fixed)
	expect_done(failed)
	jc_count_before = len(get_list("Job Card", ["name"], [["work_order", "=", wo], ["docstatus", "!=", 2]]))

	print("\n== idempotency: run() twice on a Done log ==")
	r = call("erpnext_extensions.daily_production.runner.run", name=failed)
	check(
		r and r.get("status") == "Done" and "nothing to do" in r.get("message", ""),
		f"second run is a no-op: {r and r.get('message')}",
	)
	check(
		len(get_list("Job Card", ["name"], [["work_order", "=", wo], ["docstatus", "!=", 2]]))
		== jc_count_before,
		"no duplicate Job Card",
	)

	print("\n== finish: op 5 lot 2 (1500) and lot 3 (756) ==")
	expect_done(new_log(wo, 5, 1500, EMP2, DAYS[3], "13:00", "15:00", fg_batch))
	expect_done(new_log(wo, 5, 756, EMP2, DAYS[4], "11:30", "13:00", fg_batch))

	report(wo)
	if failures:
		print(f"\n{len(failures)} expectation(s) failed:")
		for f in failures:
			print("  -", f)
		sys.exit(1)
	print("\nALL EXPECTATIONS PASSED")


def report(wo):
	doc = get_doc("Work Order", wo)
	print(f"\n== Work Order {wo}: status={doc['status']} produced={doc['produced_qty']} ==")
	check(doc["status"] == "Completed" and doc["produced_qty"] == 2756, "WO Completed, Produced 2756")
	cards = get_list(
		"Job Card",
		[
			"name",
			"operation",
			"operation_id",
			"for_quantity",
			"total_completed_qty",
			"semi_fg_bom",
			"docstatus",
			"total_time_in_mins",
		],
		[["work_order", "=", wo], ["docstatus", "!=", 2]],
		order_by="creation asc",
	)
	# op 1 runs as two cycles (1378 + 1378), so the batch takes 2 + 1 + 3 + 3 + 3 = 12 cards
	check(len(cards) == 12, f"12 Job Cards ({len(cards)})")
	check(
		all(c["for_quantity"] == c["total_completed_qty"] for c in cards),
		"every card for_quantity == cycle qty",
	)
	check(all(c["semi_fg_bom"] for c in cards), "semi_fg_bom set on all cards")
	ses = get_list(
		"Stock Entry",
		["name", "job_card", "fg_completed_qty", "total_additional_costs"],
		[["work_order", "=", wo], ["purpose", "=", "Manufacture"], ["docstatus", "=", 1]],
		order_by="creation asc",
	)
	booked = {}
	jc_op = {c["name"]: c["operation_id"] for c in cards}
	for s in ses:
		booked[jc_op.get(s["job_card"])] = booked.get(jc_op.get(s["job_card"]), 0) + (
			s["total_additional_costs"] or 0
		)
	print(
		f"{'op':3} {'operation':24} {'planned min':>11} {'actual min':>10} {'planned cost':>16} {'actual cost (WO)':>18} {'booked to stock':>16} {'delta':>12}"
	)
	for op in doc["operations"]:
		b = booked.get(op["name"], 0)
		delta = round(b - (op["actual_operating_cost"] or 0), 2)
		print(
			f"{op['idx']:<3} {op['operation'][:24]:24} {op['time_in_mins']:>11} {op['actual_operation_time']:>10} {op['planned_operating_cost']:>16,.2f} {op['actual_operating_cost']:>18,.2f} {b:>16,.2f} {delta:>12,.2f}"
		)
		check(abs(delta) < 1, f"op {op['idx']} booked == actual")
	print("\nvaluation chain:")
	for s in ses:
		d = get_doc("Stock Entry", s["name"])
		fg = next(i for i in d["items"] if i.get("is_finished_item"))
		print(
			f"  {s['name']} {fg['item_code']} x{fg['qty']:>6} valuation={fg['valuation_rate']} batch={fg.get('batch_no')} addl={d['total_additional_costs']}"
		)


if __name__ == "__main__":
	main()

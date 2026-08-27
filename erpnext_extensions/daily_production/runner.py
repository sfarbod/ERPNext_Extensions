# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Daily Production Log runner.

One Log = one production cycle of one operation. ``run(name)`` does, in one
transaction, what an operator otherwise does by hand on the desk:

    validate → Job Card (reuse / create) → Material Transfer → time log →
    Complete Job (cycle qty = produced qty) → submit card → Manufacture entry

Every step calls the same ERPNext function the desk button calls; nothing
manufacturing-related is re-implemented here. Any exception rolls the whole
run back and leaves the Log in status *Failed* with the traceback.

Read ``_execute`` top to bottom for the sequence.
"""

from __future__ import annotations

import time
import traceback
from datetime import timedelta
from itertools import pairwise

import frappe
from erpnext.manufacturing.doctype.job_card.job_card import make_stock_entry as make_transfer_from_job_card
from erpnext.manufacturing.doctype.work_order.work_order import make_job_card
from erpnext.stock.doctype.batch.batch import get_batch_qty
from erpnext.stock.doctype.stock_entry.stock_entry import get_previous_operation_output_sn_batch
from frappe import _
from frappe.utils import add_days, cint, flt, get_datetime, now_datetime
from frappe.utils.synchronization import LockTimeoutError, filelock

LOG_DT = "Daily Production Log"

# The site's Before-Submit validator («ولیدیت-تولید-دیفرنس تعدیلات سند منیفکچر») requires this
# account on every row of a Manufacture entry; the desk button «اعمال حساب تعدیلات» sets it.
MANUFACTURE_DIFFERENCE_ACCOUNT = "621301 - تعدیلات موجودی کالا - E"

# Seconds to wait for another run of the same WO + operation before giving up.
LOCK_TIMEOUT = 2


# ----------------------------------------------------------------------------- entry point
@frappe.whitelist()
def run(name: str) -> dict:
	"""Run the cycle described by Daily Production Log ``name``.

	Idempotent: a *Done* log is a no-op; a *Running* log is refused; *Draft* and
	*Failed* logs are (re-)run. On failure everything created in this run is
	rolled back and the log is set to *Failed*.
	"""
	log = frappe.get_doc(LOG_DT, name)
	log.check_permission("write")

	# ---- claim the log (row lock → no two requests can both start it) --------------------
	status = frappe.db.get_value(LOG_DT, name, "status", for_update=True)
	if status == "Done":
		return {"status": "Done", "message": _("Log {0} is already Done — nothing to do.").format(name)}
	if status == "Running":
		frappe.throw(_("Log {0} is already running.").format(name))
	_set(name, {"status": "Running", "error_log": None, "run_on": now_datetime()})
	frappe.db.commit()

	started = time.monotonic()
	try:
		with filelock(_lock_name(log), timeout=LOCK_TIMEOUT):
			created = _execute(log)
	except LockTimeoutError:
		return _fail(
			name,
			started,
			_("Another Daily Production Log for Work Order {0} / operation {1} is running right now.").format(
				log.work_order, log.operation
			),
		)
	except Exception:
		return _fail(name, started, traceback.format_exc())

	# ---- success: link documents and close the log in the same transaction ---------------
	doc = frappe.get_doc(LOG_DT, name)
	doc.flags.from_runner = True
	doc.status = "Done"
	doc.error_log = None
	doc.duration_seconds = round(time.monotonic() - started, 2)
	doc.job_card = created["job_card"]
	doc.transfer_stock_entry = created.get("transfer")
	doc.manufacture_stock_entry = created["manufacture"]
	doc.set("created_documents", [])
	for dt, dn, action in created["documents"]:
		doc.append("created_documents", {"document_type": dt, "document_name": dn, "action": action})
	doc.save(ignore_permissions=True)
	frappe.db.commit()

	return {
		"status": "Done",
		"job_card": doc.job_card,
		"transfer_stock_entry": doc.transfer_stock_entry,
		"manufacture_stock_entry": doc.manufacture_stock_entry,
		"duration_seconds": doc.duration_seconds,
		"message": _("Cycle posted: Job Card {0}, Manufacture Entry {1} ({2} s).").format(
			doc.job_card, doc.manufacture_stock_entry, doc.duration_seconds
		),
	}


# ----------------------------------------------------------------------------- the sequence
def _execute(log) -> dict:
	"""The whole cycle, top to bottom. Raises on any problem; the caller rolls back."""
	documents: list[tuple[str, str, str]] = []
	qty = flt(log.qty)
	from_time, to_time = get_datetime(log.from_time), get_datetime(log.to_time)

	# 1. Work Order + operation row ---------------------------------------------------------
	wo = frappe.get_doc("Work Order", log.work_order)
	if wo.docstatus != 1 or wo.status in ("Completed", "Stopped", "Closed", "Cancelled"):
		frappe.throw(
			_("Work Order {0} is {1}; nothing can be produced against it.").format(wo.name, wo.status)
		)
	if not wo.track_semi_finished_goods:
		frappe.throw(_("Work Order {0} does not track semi finished goods.").format(wo.name))
	op_row = _operation_row(wo, log)
	if not op_row.finished_good:
		frappe.throw(_("Operation {0} has no Finished Good on the Work Order.").format(op_row.operation))

	# 2. Input sanity -----------------------------------------------------------------------
	if qty <= 0:
		frappe.throw(_("Qty must be greater than zero."))
	if to_time <= from_time:
		frappe.throw(_("To time must be after From time."))
	if frappe.db.get_value("Employee", log.employee, "status") != "Active":
		frappe.throw(_("Employee {0} is not active.").format(log.employee))

	# 3. Pending qty of the operation ≥ qty ---------------------------------------------------
	#    pending = WO qty - completed - qty already claimed by other open cards of this operation.
	reusable, open_cards = _find_cards(wo, op_row)
	claimed = sum(flt(c.for_quantity) for c in open_cards)
	pending = flt(wo.qty) - flt(op_row.completed_qty) - flt(op_row.process_loss_qty) - claimed
	if qty > pending + 1e-9:
		frappe.throw(
			_(
				"Only {0} of {1} is still pending for operation {2} on {3} "
				"(completed {4}, claimed by open cards {5}); this log asks for {6}."
			).format(pending, wo.qty, op_row.operation, wo.name, op_row.completed_qty, claimed, qty)
		)

	# 4. Previous operation's output available in this operation's source warehouse ------------
	#    (batch-aware: only lots this Work Order produced). Enforces the "downstream card qty" rule
	#    up front instead of failing inside the transfer mapping.
	input_item, source_wh = _previous_output(wo, op_row)
	if input_item:
		available = _available_previous_output(wo, input_item, source_wh)
		if available + 1e-9 < qty:
			frappe.throw(
				_(
					"Only {0} of {1} (output of the previous operation) is available in {2}; this cycle needs {3}."
				).format(available, input_item, source_wh, qty)
			)

	# 5. Operator must not be on another running timer ---------------------------------------
	open_log = frappe.db.sql(
		"""select jc.name from `tabJob Card Time Log` tl
		   join `tabJob Card` jc on jc.name = tl.parent
		   where tl.employee = %s and ifnull(tl.to_time, '') = '' and jc.docstatus = 0 limit 1""",
		(log.employee,),
	)
	if open_log:
		frappe.throw(
			_("Employee {0} still has a running timer on Job Card {1}.").format(log.employee, open_log[0][0])
		)

	# 6. No other log for the same WO + operation may be running --------------------------------
	other = frappe.db.get_value(
		LOG_DT,
		{
			"work_order": wo.name,
			"operation_row_id": op_row.idx,
			"status": "Running",
			"name": ("!=", log.name),
		},
		"name",
	)
	if other:
		frappe.throw(
			_("Daily Production Log {0} for the same Work Order / operation is running.").format(other)
		)

	# 7. Job Card: reuse an untouched draft card, else create one the way the WO dialog does -----
	if reusable:
		card = reusable
		action = "reused"
		if flt(card.for_quantity) != qty:
			# Same as editing "Qty To Manufacture" on the draft card: items are rebuilt and rescaled.
			card.for_quantity = qty
			card.set("items", [])
			card.get_required_items()
			card.save()
			action = f"reused (qty set to {_num(qty)})"
	else:
		card = _create_card(wo, op_row, qty, pending)
		action = "created"
	if not card.semi_fg_bom:
		# Closes the per-lot operating-cost double count (see job_card_hooks.after_insert).
		card.db_set("semi_fg_bom", wo.bom_no, update_modified=False)
	if card.meta.has_field("custom_batch_number") and not card.get("custom_batch_number"):
		card.db_set("custom_batch_number", wo.get("custom_fg_batch_no"), update_modified=False)
	card.reload()
	documents.append(("Job Card", card.name, action))

	# 8. Material Transfer for Manufacture (unless the operation skips it) -----------------------
	transfer = None
	if not card.skip_material_transfer:
		transfer = _transfer_materials(card, wo, qty, log)
		documents.append(("Stock Entry", transfer, "created"))
		card.reload()

	# 9. Time log(s): one per sub-operation if the operation has any, else one ------------------
	_log_time(card, log.employee, from_time, to_time, qty)
	card.reload()

	# 10. The card must now be exactly this cycle -------------------------------------------------
	if flt(card.for_quantity) != qty or flt(card.total_completed_qty) != qty or flt(card.pending_qty):
		frappe.throw(
			_(
				"Job Card {0} ended with Qty To Manufacture {1}, Completed {2}, Pending {3}; expected {4}/{4}/0."
			).format(card.name, card.for_quantity, card.total_completed_qty, card.pending_qty, _num(qty))
		)
	card.submit()
	card.reload()
	if card.docstatus != 1:
		frappe.throw(_("Job Card {0} could not be submitted.").format(card.name))

	# 11. Manufacture entry for the operation's Finished Good ---------------------------------------
	manufacture = _manufacture(card, wo, op_row, qty, log)
	documents.append(("Stock Entry", manufacture, "created"))

	return {"job_card": card.name, "transfer": transfer, "manufacture": manufacture, "documents": documents}


# ----------------------------------------------------------------------------- steps
def _operation_row(wo, log):
	rows = [r for r in wo.operations if r.idx == cint(log.operation_row_id)] or [
		r for r in wo.operations if r.operation == log.operation
	]
	if not rows or rows[0].operation != log.operation:
		frappe.throw(
			_("Operation {0} (row {1}) not found on {2}.").format(
				log.operation, log.operation_row_id, wo.name
			)
		)
	return rows[0]


def _find_cards(wo, op_row):
	"""Draft cards of this operation. A card nobody touched yet (no time log, nothing
	transferred) can be reused for this cycle; the others count as claimed quantity."""
	reusable, open_cards = None, []
	names = frappe.get_all(
		"Job Card",
		filters={
			"work_order": wo.name,
			"operation_id": op_row.name,
			"docstatus": 0,
			"is_corrective_job_card": 0,
		},
		pluck="name",
		order_by="creation asc",
	)
	for name in names:
		card = frappe.get_doc("Job Card", name)
		untouched = not card.time_logs and not flt(card.transferred_qty) and not _has_stock_entry(card.name)
		if untouched and reusable is None:
			reusable = card
		else:
			open_cards.append(card)
	return reusable, open_cards


def _has_stock_entry(job_card):
	return bool(frappe.db.exists("Stock Entry", {"job_card": job_card, "docstatus": ("<", 2)}))


def _previous_output(wo, op_row):
	"""(item, source warehouse) of the semi-finished good this operation consumes, or (None, None)."""
	finished_goods = {r.finished_good for r in wo.operations if r.finished_good}
	for d in wo.required_items:
		if cint(d.operation_row_id) == op_row.idx and d.item_code in finished_goods:
			return d.item_code, (op_row.source_warehouse or d.source_warehouse or wo.source_warehouse)
	return None, None


def _available_previous_output(wo, item_code, warehouse):
	pool = get_previous_operation_output_sn_batch(wo.name, item_code, warehouse)
	if pool.batches or pool.serial_nos:
		return sum(flt(q) for q in pool.batches.values()) or len(pool.serial_nos)
	# Item not batch/serial tracked: plain bin quantity.
	return flt(frappe.db.get_value("Bin", {"item_code": item_code, "warehouse": warehouse}, "actual_qty"))


def _create_card(wo, op_row, qty, pending):
	"""Same server call as the Work Order → *Create Job Card* dialog (items scaled to qty)."""
	before = set(frappe.get_all("Job Card", filters={"work_order": wo.name}, pluck="name"))
	make_job_card(
		wo.name,
		[
			{
				"name": op_row.name,
				"operation": op_row.operation,
				"workstation": op_row.workstation,
				"qty": qty,
				"pending_qty": pending,
				"batch_size": op_row.batch_size,
				"sequence_id": op_row.sequence_id,
			}
		],
		parent_bom=wo.bom_no,
	)
	new = [
		n
		for n in frappe.get_all("Job Card", filters={"work_order": wo.name}, pluck="name")
		if n not in before
	]
	if len(new) != 1:
		frappe.throw(
			_(
				"Creating the Job Card produced {0} cards instead of 1 — the qty {1} exceeds the operation's "
				"batch size ({2}). Split the cycle."
			).format(len(new), _num(qty), _num(op_row.batch_size))
		)
	return frappe.get_doc("Job Card", new[0])


def _transfer_materials(card, wo, qty, log):
	"""Material Transfer for Manufacture from the Job Card (same mapper as the desk button),
	for exactly this cycle, with batches picked automatically."""
	se = make_transfer_from_job_card(card.name)
	se.naming_series = None  # the mapper copies the Job Card's series; let Stock Entry use its own
	se.fg_completed_qty = qty
	rows = list(se.items)
	semi_finished = {r.finished_good for r in wo.operations if r.finished_good}
	se.set("items", [])
	for row in rows:
		row.department = row.department or log.department
		row.cost_center = row.cost_center or log.cost_center
		if row.serial_and_batch_bundle or row.batch_no or not _item_has_batch(row.item_code):
			# previous-operation output already carries the lots this Work Order produced
			se.append("items", row)
			continue
		if row.item_code in semi_finished:
			# never pick another Work Order's lot of a semi-finished good by FEFO
			frappe.throw(
				_("No lot of {0} produced by {1} was found in {2} for the transfer.").format(
					row.item_code, wo.name, row.s_warehouse
				)
			)
		base = row.as_dict()
		for key in (
			"name",
			"idx",
			"parent",
			"parentfield",
			"parenttype",
			"docstatus",
			"creation",
			"modified",
			"owner",
			"modified_by",
		):
			base.pop(key, None)
		for batch_no, batch_qty in _fefo_batches(row.item_code, row.s_warehouse, flt(row.qty)):
			part = dict(base)
			part.update(
				qty=batch_qty,
				transfer_qty=batch_qty * flt(row.conversion_factor or 1),
				use_serial_batch_fields=1,
				batch_no=batch_no,
				serial_and_batch_bundle=None,
			)
			se.append("items", part)
	se.insert()
	se.submit()
	return se.name


def _item_has_batch(item_code):
	return cint(frappe.get_cached_value("Item", item_code, "has_batch_no"))


def _fefo_batches(item_code, warehouse, qty):
	"""Allocate ``qty`` of a batch-tracked raw material from ``warehouse`` — earliest expiry
	first (Stock Settings: pick batches based on expiry). Fails on shortfall instead of
	leaving the operator with a negative-stock error at submit."""
	rows = [r for r in get_batch_qty(item_code=item_code, warehouse=warehouse) or [] if flt(r.get("qty")) > 0]
	expiry = {
		r.name: r.expiry_date
		for r in frappe.get_all(
			"Batch", filters={"name": ("in", [r["batch_no"] for r in rows])}, fields=["name", "expiry_date"]
		)
	}
	rows.sort(
		key=lambda r: (expiry.get(r["batch_no"]) is None, expiry.get(r["batch_no"]) or "", r["batch_no"])
	)
	picked, remaining = [], flt(qty)
	for r in rows:
		if remaining <= 1e-9:
			break
		take = min(flt(r["qty"]), remaining)
		picked.append((r["batch_no"], take))
		remaining -= take
	if remaining > 1e-9:
		frappe.throw(
			_("Not enough batched stock of {0} in {1}: need {2}, found {3}.").format(
				item_code, warehouse, _num(qty), _num(qty - remaining)
			)
		)
	return picked


def _log_time(card, employee, from_time, to_time, qty):
	"""Start/Complete exactly like the desk timer. Operations with sub-operations need one
	closed log per sub-operation (v16 takes the minimum completed qty across them), so the
	window is split into equal consecutive slots."""
	subs = [s.sub_operation for s in card.get("sub_operations") or []] or [None]
	slots = _split_window(from_time, to_time, len(subs))
	for sub, (start, end) in zip(subs, slots, strict=True):
		card.reload()
		card.start_timer(start_time=start, employees=[{"employee": employee}])
		card.reload()
		# Cycle qty = produced qty, pending 0 — the rule from the semi-finished-goods test.
		card.complete_job_card(
			qty=qty,
			for_quantity=qty,
			pending_qty=0,
			process_loss_qty=0,
			end_time=end,
			sub_operation=sub,
		)


def _split_window(start, end, n):
	total = (end - start).total_seconds()
	edges = [start + timedelta(seconds=round(total * i / n)) for i in range(n)] + [end]
	return list(pairwise(edges))


def _manufacture(card, wo, op_row, qty, log):
	"""Manufacture entry for the card's Finished Good (same as *Make Stock Entry* → No, then
	the account button, then Submit)."""
	created = card.make_stock_entry_for_semi_fg_item(auto_submit=False)
	se = frappe.get_doc("Stock Entry", created["name"])
	fg_row = None
	for row in se.items:
		row.expense_account = MANUFACTURE_DIFFERENCE_ACCOUNT
		row.department = row.department or log.department
		row.cost_center = row.cost_center or log.cost_center
		if row.is_finished_item:
			fg_row = row
	if not fg_row or flt(fg_row.qty) != qty:
		frappe.throw(
			_("Manufacture entry {0} would produce {1} of {2}; expected {3}.").format(
				se.name, fg_row.qty if fg_row else 0, op_row.finished_good, _num(qty)
			)
		)
	if _item_has_batch(fg_row.item_code):
		fg_row.use_serial_batch_fields = 1
		fg_row.serial_and_batch_bundle = None
		fg_row.batch_no = log.output_batch_no or _placeholder_lot(
			card, wo, fg_row.item_code, se.posting_date, log
		)
	se.save()
	se.submit()
	return se.name


def _placeholder_lot(card, wo, item_code, posting_date, log):
	"""Until planning defines the sub-lot suffix rule: ``<GMP no>-Lnn`` where nn is the cycle
	number of this WO + operation. Same id convention as the site's batch scripts
	(``<counter>-<item>-<custom_batch_no>``) so the duplicate validator and reports keep working."""
	gmp = card.get("custom_batch_number") or wo.get("custom_fg_batch_no") or wo.name
	cycle_no = (
		frappe.db.count(
			LOG_DT,
			{
				"work_order": wo.name,
				"operation_row_id": log.operation_row_id,
				"status": "Done",
				"name": ("!=", log.name),
			},
		)
		+ 1
	)
	lot_no = f"{gmp}-L{cycle_no:02d}"
	existing = frappe.db.get_value("Batch", {"item": item_code, "custom_batch_no": lot_no}, "name")
	if existing:
		return existing

	last = frappe.db.sql(
		"select batch_id from `tabBatch` where ifnull(batch_id, '') != '' order by creation desc limit 1",
		pluck=True,
	)
	counter = 1
	if last and last[0].split("-")[0].isdigit():
		counter = int(last[0].split("-")[0]) + 1
	while frappe.db.exists("Batch", f"{counter}-{item_code}-{lot_no}"):
		counter += 1

	batch = frappe.new_doc("Batch")
	batch.batch_id = f"{counter}-{item_code}-{lot_no}"
	batch.item = item_code
	batch.manufacturing_date = posting_date
	has_expiry, shelf_life = frappe.db.get_value("Item", item_code, ["has_expiry_date", "shelf_life_in_days"])
	if has_expiry and shelf_life:
		batch.expiry_date = add_days(posting_date, cint(shelf_life))
	batch.reference_doctype = "Work Order"
	batch.reference_name = wo.name
	batch.description = _("Placeholder lot {0} for {1} / {2} created by {3}").format(
		lot_no, wo.name, card.name, log.name
	)
	if batch.meta.has_field("custom_batch_no"):
		batch.custom_batch_no = lot_no
	if batch.meta.has_field("custom_is_placeholder_lot"):
		batch.custom_is_placeholder_lot = 1
	batch.flags.ignore_permissions = True
	batch.insert()
	return batch.name


# ----------------------------------------------------------------------------- helpers
def _fail(name, started, error_text):
	frappe.db.rollback()
	_set(
		name,
		{
			"status": "Failed",
			"error_log": error_text,
			"duration_seconds": round(time.monotonic() - started, 2),
		},
	)
	frappe.db.commit()
	last_line = [line for line in str(error_text).strip().splitlines() if line.strip()][-1]
	frappe.throw(
		_("Cycle failed and was rolled back: {0}").format(_strip_exc(last_line)), title=_("Run failed")
	)


def _set(name, values):
	frappe.db.set_value(LOG_DT, name, values, update_modified=False)


def _lock_name(log):
	return frappe.scrub(f"daily_production_{log.work_order}_{cint(log.operation_row_id)}")


def _strip_exc(line):
	# "frappe.exceptions.ValidationError: message" → "message"
	return (
		line.split(": ", 1)[1]
		if ": " in line and line.split(": ", 1)[0].replace(".", "").replace("_", "").isalnum()
		else line
	)


def _num(value):
	value = flt(value)
	return str(int(value)) if value == int(value) else str(value)

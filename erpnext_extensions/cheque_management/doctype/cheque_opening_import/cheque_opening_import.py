# Copyright (c) 2026, Farbod Siyahpoosh and contributors
# License: MIT. See license.txt

"""Opening balance import for **Post Dated Cheque** (Payable + Receivable).

Uses normal PDC insert/save/submit and workflow transitions — no bypass of validation,
reservation, or leaf Used hooks. Each data row runs in a DB savepoint so failures
do not disturb other rows.
"""

from __future__ import annotations

import io
import json
import re
from collections import deque
from datetime import date
from typing import Any

import frappe
from frappe.model.document import Document
from frappe.utils import cstr, flt, getdate

from erpnext_extensions.cheque_management.pdc_workflow_state_machine import (
	ALL_WORKFLOW_STATES,
	CHEQUE_DIRECTION_PAYABLE,
	CHEQUE_DIRECTION_RECEIVABLE,
	PAYABLE_WORKFLOW_TRANSITIONS,
	RECEIVABLE_WORKFLOW_TRANSITIONS,
	normalize_workflow_state_value,
)

REQUIRED_HEADERS = frozenset(
	{
		"cheque_direction",
		"company",
		"bank_account",
		"cheque_number",
		"cheque_due_date",
		"cheque_amount",
		"party_type",
		"party",
		"workflow_state",
	}
)

HEADER_ALIASES = {
	"direction": "cheque_direction",
	"cheque_no": "cheque_number",
	"amount": "cheque_amount",
	"due_date": "cheque_due_date",
	"workflow": "workflow_state",
	"status": "workflow_state",
	"book": "cheque_book",
	"drawer_bank": "drawer_bank_name",
	"sayad": "sayad_code",
	"sayad_code": "sayad_code",
	"sayad_registered": "sayad_registered",
	"sayad_reg": "sayad_registered",
	"purpose": "cheque_purpose",
	"cheque_purpose": "cheque_purpose",
}

# Exact header text (Persian / mixed) → canonical field name.
HEADER_ALIASES_EXACT: dict[str, str] = {
	"کد صیادی": "sayad_code",
	"شماره صیادی": "sayad_code",
	"ثبت صیادی": "sayad_registered",
	"ثبت شده در صیاد": "sayad_registered",
	"وضعیت ثبت صیاد": "sayad_registered",
	"بابت": "cheque_purpose",
	"Cheque Purpose": "cheque_purpose",
}

_SAYAD_REGISTERED_TRUE = frozenset(
	{
		"1",
		"true",
		"yes",
		"y",
		"checked",
		"on",
		"بله",
		"درست",
		"ثبت شده",
	}
)
_SAYAD_REGISTERED_FALSE = frozenset(
	{
		"0",
		"false",
		"no",
		"n",
		"unchecked",
		"off",
		"خیر",
		"نادرست",
		"ثبت نشده",
	}
)


def _norm_header(h: str | None) -> str:
	if h is None:
		return ""
	raw = cstr(h).strip()
	if raw in HEADER_ALIASES_EXACT:
		return HEADER_ALIASES_EXACT[raw]
	s = raw.lower()
	s = re.sub(r"[\s\-]+", "_", s)
	return HEADER_ALIASES.get(s, s)


def _parse_sayad_registered(val: Any) -> bool | None:
	"""Parse Sayad Registered cell; ``None`` if empty / absent."""
	if val is None:
		return None
	if isinstance(val, bool):
		return val
	if isinstance(val, (int, float)) and not isinstance(val, bool):
		return bool(int(val))
	s = cstr(val).strip()
	if not s:
		return None
	s_lower = s.lower()
	if s_lower in _SAYAD_REGISTERED_TRUE or s in _SAYAD_REGISTERED_TRUE:
		return True
	if s_lower in _SAYAD_REGISTERED_FALSE or s in _SAYAD_REGISTERED_FALSE:
		return False
	frappe.throw(
		frappe._("Invalid Sayad Registered value: {0}").format(s[:80]),
		title=frappe._("Cheque Opening Import"),
	)


def _apply_sayad_fields_from_row(pdc: Document, row: dict[str, Any]) -> None:
	"""Map import row Sayad columns to PDC (code + registered checkbox rules)."""
	code = cstr(row.get("sayad_code") or "").strip()
	reg_raw = row.get("sayad_registered")
	reg = _parse_sayad_registered(reg_raw)

	if code:
		pdc.sayad_code = code

	if reg is not None:
		pdc.sayad_registered = 1 if reg else 0
	elif code:
		pdc.sayad_registered = 1
	else:
		pdc.sayad_registered = 0


def _parse_date(val: Any):
	if val is None or val == "":
		return None
	if hasattr(val, "date"):
		return val.date()
	return getdate(val)


def _parse_cheque_amount(val: Any):
	"""Parse import file amount without losing DECIMAL(30,9) scale from string cells."""
	from decimal import ROUND_HALF_UP, Decimal

	if val is None or val == "":
		return Decimal("0")
	if isinstance(val, Decimal):
		amount = val
	else:
		amount = Decimal(cstr(val).strip().replace(",", ""))
	return amount.quantize(Decimal("0.000000001"), rounding=ROUND_HALF_UP)


def _apply_exact_cheque_amount_db(pdc_name: str, amount) -> None:
	"""Persist opening-import amount exactly; Frappe ORM uses float for Currency fields."""
	from decimal import Decimal

	if not isinstance(amount, Decimal):
		amount = _parse_cheque_amount(amount)
	amount_str = format(amount, "f")
	frappe.db.sql(
		"UPDATE `tabPost Dated Cheque` SET cheque_amount = %s WHERE name = %s",
		(amount_str, pdc_name),
	)


def _rows_from_sheet(rows: list[list[Any]]) -> list[tuple[int, dict[str, Any]]]:
	if not rows:
		return []
	header = [_norm_header(cstr(c) if c is not None else "") for c in rows[0]]
	missing = [h for h in REQUIRED_HEADERS if h not in header]
	if missing:
		frappe.throw(
			frappe._("Missing required column(s): {0}").format(", ".join(sorted(missing))),
			title=frappe._("Cheque Opening Import"),
		)
	out = []
	for i, line in enumerate(rows[1:], start=2):
		if not line or all(c is None or cstr(c).strip() == "" for c in line):
			continue
		row = {}
		for hi, key in enumerate(header):
			if not key:
				continue
			val = line[hi] if hi < len(line) else None
			if val is not None and not isinstance(val, (str, int, float)):
				val = cstr(val)
			row[key] = val
		out.append((i, row))
	return out


def _get_sheet_from_file_url(file_url: str) -> list[list[Any]]:
	if not file_url:
		frappe.throw(frappe._("Attach a CSV or Excel file first."), title=frappe._("Cheque Opening Import"))

	from frappe.utils.csvutils import read_csv_content
	from frappe.utils.xlsxutils import read_xls_file_from_attached_file

	_file = frappe.get_doc("File", {"file_url": file_url})
	path = _file.get_full_path() or ""
	lower = path.lower()

	logger = frappe.logger("cheque_opening_import")
	logger.info("Preview parse start: file_url=%s path=%s", file_url, path)

	if lower.endswith(".csv"):
		content = _file.get_content()
		logger.info("Preview parse: detected_type=csv")
		return read_csv_content(content)

	if lower.endswith(".xlsx"):
		logger.info("Preview parse: detected_type=xlsx")
		import openpyxl

		wb = openpyxl.load_workbook(filename=path, data_only=True)
		ws = wb["Data"] if "Data" in wb.sheetnames else wb.active
		if ws.title != "Data":
			logger.info("Preview parse: sheet_selected=%s (fallback)", ws.title)
		else:
			logger.info("Preview parse: sheet_selected=Data")

		rows: list[list[Any]] = []
		for row in ws.iter_rows():
			rows.append([cell.value for cell in row])
		logger.info("Preview parse: parsed_rows=%s", len(rows))
		return rows

	if lower.endswith(".xls"):
		logger.info("Preview parse: detected_type=xls")
		content = _file.get_content()
		rows = read_xls_file_from_attached_file(content)
		logger.info("Preview parse: parsed_rows=%s", len(rows))
		return rows

	frappe.throw(
		frappe._("Unsupported file type. Use .csv or .xlsx."),
		title=frappe._("Cheque Opening Import"),
	)


def find_workflow_path(cheque_direction: str, target_raw: str) -> list[str]:
	"""Shortest path from **Draft** to ``target`` on the direction-specific graph."""
	target = normalize_workflow_state_value(target_raw)
	if target not in ALL_WORKFLOW_STATES:
		frappe.throw(
			frappe._("Invalid workflow_state: {0}").format(target_raw),
			title=frappe._("Cheque Opening Import"),
		)

	graph = (
		RECEIVABLE_WORKFLOW_TRANSITIONS
		if (cheque_direction or "").strip() == CHEQUE_DIRECTION_RECEIVABLE
		else PAYABLE_WORKFLOW_TRANSITIONS
	)
	start = normalize_workflow_state_value(None)  # Draft
	if target == start:
		return [start]

	q: deque[str] = deque([start])
	prev: dict[str, str | None] = {start: None}
	while q:
		u = q.popleft()
		for v in graph.get(u, frozenset()):
			if v not in prev:
				prev[v] = u
				if v == target:
					path: list[str] = []
					cur: str | None = v
					while cur is not None:
						path.append(cur)
						cur = prev[cur]
					path.reverse()
					return path
				q.append(v)

	frappe.throw(
		frappe._("No allowed workflow path from Draft to {0} for {1}.").format(target, cheque_direction),
		title=frappe._("Cheque Opening Import"),
	)


def _collapse_ws(value: Any) -> str:
	return re.sub(r"\s+", " ", cstr(value or "").strip())


def _unique_nonempty(values: list[str]) -> list[str]:
	out: list[str] = []
	for v in values:
		if v and v not in out:
			out.append(v)
	return out


def _resolve_bank_account_name(raw: Any, company: str | None = None) -> str | None:
	"""Map Excel bank_account text to a Bank Account document name.

	Spreadsheets often carry ``account_name`` (with padding spaces) or a truncated
	label instead of the Link ``name`` (which may append `` - {bank}``).
	"""
	text = cstr(raw or "")
	if not text.strip():
		return None

	variants = _unique_nonempty([text, text.strip(), _collapse_ws(text)])
	for cand in variants:
		if frappe.db.exists("Bank Account", cand):
			return cand

	company_s = cstr(company or "").strip()
	conds = ["IFNULL(disabled, 0) = 0"]
	vals: list[Any] = []
	if company_s:
		conds.append("company = %s")
		vals.append(company_s)

	or_parts: list[str] = []
	for cand in variants:
		or_parts.append("name = %s")
		vals.append(cand)
		or_parts.append("account_name = %s")
		vals.append(cand)
		or_parts.append("TRIM(name) = %s")
		vals.append(cand.strip())
		or_parts.append("TRIM(account_name) = %s")
		vals.append(cand.strip())

	# Internal whitespace often differs (e.g. Excel "593 - Bank" vs name "593  - Bank").
	collapsed = _collapse_ws(text)
	or_parts.append(
		"REPLACE(REPLACE(REPLACE(TRIM(name), '\\t', ' '), '  ', ' '), '  ', ' ') = %s"
	)
	vals.append(collapsed)
	or_parts.append(
		"REPLACE(REPLACE(REPLACE(TRIM(IFNULL(account_name, '')), '\\t', ' '), '  ', ' '), '  ', ' ') = %s"
	)
	vals.append(collapsed)

	sql = f"""
		SELECT name
		FROM `tabBank Account`
		WHERE {" AND ".join(conds)}
		  AND ({" OR ".join(or_parts)})
		ORDER BY IFNULL(is_company_account, 0) DESC, modified DESC
	"""
	rows = frappe.db.sql(sql, tuple(vals), as_list=True) or []
	names = _unique_nonempty([cstr(r[0]) for r in rows])
	if len(names) == 1:
		return names[0]
	if len(names) > 1:
		frappe.throw(
			frappe._(
				"Ambiguous Bank Account {0!r} matched: {1}. Use the exact Bank Account name."
			).format(text.strip(), ", ".join(names[:5])),
			title=frappe._("Cheque Opening Import"),
		)
	return None


def _resolve_drawer_bank_name(raw: Any) -> str | None:
	"""Map Excel drawer_bank_name to a Bank master name (e.g. خاورمیانه → بانک خاورمیانه)."""
	text = cstr(raw or "").strip()
	if not text:
		return None
	if frappe.db.exists("Bank", text):
		return text

	prefixed = f"بانک {text}"
	if frappe.db.exists("Bank", prefixed):
		return prefixed

	rows = frappe.db.sql(
		"""
		SELECT name FROM `tabBank`
		WHERE name = %s OR name = %s OR name LIKE %s
		ORDER BY CHAR_LENGTH(name) ASC, name ASC
		""",
		(text, prefixed, f"%{text}%"),
		as_list=True,
	) or []
	names = _unique_nonempty([cstr(r[0]) for r in rows])
	if len(names) == 1:
		return names[0]
	if prefixed in names:
		return prefixed
	if len(names) > 1:
		frappe.throw(
			frappe._(
				"Ambiguous Drawer Bank {0!r} matched: {1}. Use the exact Bank name."
			).format(text, ", ".join(names[:5])),
			title=frappe._("Cheque Opening Import"),
		)
	return None


def _normalize_row_link_fields(row: dict[str, Any]) -> str | None:
	"""Resolve bank_account / drawer_bank_name in-place. Return error message or None."""
	company = cstr(row.get("company") or "").strip()
	raw_bank = cstr(row.get("bank_account") or "")
	if raw_bank.strip():
		try:
			resolved_ba = _resolve_bank_account_name(raw_bank, company)
		except Exception as e:
			return cstr(e)
		if not resolved_ba:
			return f"Bank Account not found: {raw_bank.strip()}"
		row["bank_account"] = resolved_ba

	raw_drawer = cstr(row.get("drawer_bank_name") or "")
	if raw_drawer.strip():
		try:
			resolved_drawer = _resolve_drawer_bank_name(raw_drawer)
		except Exception as e:
			return cstr(e)
		if not resolved_drawer:
			return f"Drawer Bank not found: {raw_drawer.strip()}"
		row["drawer_bank_name"] = resolved_drawer
	return None


def _resolve_cheque_leaf(row: dict[str, Any]) -> str | None:
	if (row.get("cheque_direction") or "").strip() != CHEQUE_DIRECTION_PAYABLE:
		return None
	book = cstr(row.get("cheque_book") or "").strip()
	if not book:
		return None
	chq = cstr(row.get("cheque_number") or "").strip()
	company = cstr(row.get("company") or "").strip()
	bank = cstr(row.get("bank_account") or "").strip()
	if not chq or not company or not bank:
		return None
	name = frappe.db.get_value(
		"Cheque Leaf",
		{
			"cheque_book": book,
			"cheque_number": chq,
			"company": company,
			"bank_account": bank,
		},
		"name",
	)
	return name or None


def _validate_row_preview(row: dict[str, Any], row_no: int) -> str | None:
	"""Return error message or None if OK."""
	d = (row.get("cheque_direction") or "").strip()
	if d not in (CHEQUE_DIRECTION_PAYABLE, CHEQUE_DIRECTION_RECEIVABLE):
		return f"Row {row_no}: invalid cheque_direction {d!r}"

	for fn in ("company", "party_type", "party", "workflow_state"):
		if not cstr(row.get(fn) or "").strip():
			return f"Row {row_no}: {fn} is required"

	if not cstr(row.get("cheque_number") or "").strip():
		return f"Row {row_no}: cheque_number is required"

	try:
		_parse_date(row.get("cheque_due_date"))
	except Exception:
		return f"Row {row_no}: invalid cheque_due_date"

	try:
		_parse_cheque_amount(row.get("cheque_amount"))
	except Exception:
		return f"Row {row_no}: invalid cheque_amount"

	if d == CHEQUE_DIRECTION_RECEIVABLE and not cstr(row.get("drawer_bank_name") or "").strip():
		return f"Row {row_no}: drawer_bank_name is required for Receivable"

	ws = cstr(row.get("workflow_state") or "").strip()
	try:
		find_workflow_path(d, ws)
	except Exception as e:
		return f"Row {row_no}: workflow — {e!s}"

	target = normalize_workflow_state_value(ws)
	bank = cstr(row.get("bank_account") or "").strip()

	# Payable: always requires bank_account.
	if d == CHEQUE_DIRECTION_PAYABLE and not bank:
		return f"Row {row_no}: bank_account is required"

	# Receivable: bank_account is required only when "Sent to Bank or later" / Debt Purchase
	# assignment (instrument is with the bank or in a bank Debt Purchase pool).
	# Keep this aligned with PDC runtime behavior.
	receivable_needs_bank_states_raw = {
		"Sent to Bank",
		"In Clearing",
		"Cleared",
		"Bounced",
		"Returned",
		"Cancelled",
		"Replaced",
		"Under Legal Action",
		"Assigned to Bank for Debt Purchase",
		"Debt Purchase Settled",
	}
	receivable_needs_bank = {
		normalize_workflow_state_value(s)
		for s in receivable_needs_bank_states_raw
		if normalize_workflow_state_value(s) in ALL_WORKFLOW_STATES
	}
	if d == CHEQUE_DIRECTION_RECEIVABLE and target in receivable_needs_bank and not bank:
		return f"Row {row_no}: bank_account required for this workflow state"

	# Resolve Link fields so Preview matches Execute (LinkValidation on insert).
	link_err = _normalize_row_link_fields(row)
	if link_err:
		return f"Row {row_no}: {link_err}"

	leaf = _resolve_cheque_leaf(row)
	if d == CHEQUE_DIRECTION_PAYABLE and cstr(row.get("cheque_book") or "").strip() and not leaf:
		return f"Row {row_no}: Cheque Leaf not found for book + cheque_number + company + bank_account"

	if "sayad_registered" in row and cstr(row.get("sayad_registered") or "").strip():
		try:
			_parse_sayad_registered(row.get("sayad_registered"))
		except Exception as e:
			return f"Row {row_no}: sayad_registered — {e!s}"

	return None


def _prepare_state_dates(pdc: Document, to_state: str, row: dict[str, Any]) -> None:
	"""Set dates/fields typically required when *entering* ``to_state`` (best-effort from row)."""
	ts = normalize_workflow_state_value(to_state)
	rd = _parse_date(row.get("received_date"))
	if rd and ts in (
		normalize_workflow_state_value("Registered"),
		normalize_workflow_state_value("Issued"),
		normalize_workflow_state_value("Sent to Bank"),
		normalize_workflow_state_value("Cleared"),
	):
		if not getattr(pdc, "received_date", None):
			pdc.received_date = rd

	if ts == normalize_workflow_state_value("Issued") and pdc.cheque_direction == CHEQUE_DIRECTION_PAYABLE:
		hd = _parse_date(row.get("handover_date"))
		if hd:
			pdc.handover_date = hd
		elif not getattr(pdc, "handover_date", None):
			pdc.handover_date = getattr(pdc, "cheque_due_date", None)

	if ts == normalize_workflow_state_value("Sent to Bank"):
		sd = _parse_date(row.get("sent_to_bank_date"))
		if sd:
			pdc.sent_to_bank_date = sd

	if ts == normalize_workflow_state_value("Cleared"):
		cd = _parse_date(row.get("cleared_date"))
		if cd:
			pdc.cleared_date = cd

	if ts == normalize_workflow_state_value("Bounced"):
		bd = _parse_date(row.get("bounced_date"))
		if bd:
			pdc.bounced_date = bd

	if ts == normalize_workflow_state_value("Returned"):
		rdt = _parse_date(row.get("returned_date"))
		if rdt:
			pdc.returned_date = rdt

	# Assigned DP: no dedicated event date; received_date already applied on Register hop.


def import_row(row_no: int, row: dict[str, Any]) -> str:
	"""Create PDC, walk workflow, optionally submit. Caller wraps in savepoint."""
	direction = (row.get("cheque_direction") or "").strip()
	target = normalize_workflow_state_value(row.get("workflow_state"))
	path = find_workflow_path(direction, target)

	# Resolve Link fields even if caller skipped preview normalize.
	link_err = _normalize_row_link_fields(row)
	if link_err:
		frappe.throw(frappe._("Row {0}: {1}").format(row_no, link_err), title=frappe._("Cheque Opening Import"))

	leaf = _resolve_cheque_leaf(row)
	chq_num = cstr(row.get("cheque_number") or "").strip()
	company = cstr(row.get("company") or "").strip()
	party = cstr(row.get("party") or "").strip()

	# Duplicate guard: prevent re-import of the same opening cheque.
	existing = frappe.db.get_value(
		"Post Dated Cheque",
		{
			"cheque_direction": direction,
			"cheque_no": chq_num,
			"company": company,
			"party": party,
			"is_opening_import": 1,
		},
		"name",
	)
	if existing:
		frappe.throw(frappe._("Opening cheque already imported: {0}").format(existing))

	exact_cheque_amount = _parse_cheque_amount(row.get("cheque_amount"))

	pdc = frappe.new_doc("Post Dated Cheque")
	pdc.cheque_direction = direction
	pdc.company = company
	pdc.bank_account = cstr(row.get("bank_account") or "").strip()
	pdc.cheque_due_date = _parse_date(row.get("cheque_due_date"))
	pdc.cheque_amount = exact_cheque_amount
	pdc.party_type = cstr(row.get("party_type") or "").strip()
	pdc.party = party
	pdc.workflow_state = normalize_workflow_state_value(None)
	pdc.allocation_mode = "direct_settlement"
	pdc.cheque_no = chq_num
	purpose = cstr(row.get("cheque_purpose") or "").strip()
	if purpose:
		pdc.cheque_purpose = purpose

	# Opening import tracking (traceability + re-import protection).
	pdc.is_opening_import = 1
	pdc.opening_import = frappe.flags.get("cheque_opening_import_name")
	pdc.opening_import_row = row_no

	if direction == CHEQUE_DIRECTION_RECEIVABLE:
		pdc.drawer_bank_name = cstr(row.get("drawer_bank_name") or "").strip()

	if leaf:
		pdc.cheque_leaf = leaf
		leaf_no = frappe.db.get_value("Cheque Leaf", leaf, "cheque_number")
		if leaf_no:
			pdc.cheque_no = leaf_no

	_apply_sayad_fields_from_row(pdc, row)

	rd0 = _parse_date(row.get("received_date"))
	if rd0:
		pdc.received_date = rd0
	elif len(path) > 1:
		pdc.received_date = pdc.cheque_due_date

	prev_suppress = getattr(frappe.flags, "in_cheque_opening_import", None)
	try:
		frappe.flags.in_cheque_opening_import = True
		pdc.insert()

		for i in range(1, len(path)):
			to_st = path[i]
			_prepare_state_dates(pdc, to_st, row)
			pdc.workflow_state = to_st
			pdc.save()

		baseline_state = normalize_workflow_state_value(path[-1])
		pdc.opening_import_workflow_state = baseline_state
		frappe.db.set_value(
			"Post Dated Cheque",
			pdc.name,
			"opening_import_workflow_state",
			baseline_state,
			update_modified=False,
		)
		final = normalize_workflow_state_value(pdc.workflow_state)
		if final != normalize_workflow_state_value("Draft"):
			pdc.submit()
	finally:
		if prev_suppress is None:
			if hasattr(frappe.flags, "in_cheque_opening_import"):
				delattr(frappe.flags, "in_cheque_opening_import")
		else:
			frappe.flags.in_cheque_opening_import = prev_suppress

	_apply_exact_cheque_amount_db(pdc.name, exact_cheque_amount)

	return pdc.name


def _import_row_with_savepoint(row_no: int, row: dict[str, Any]) -> str:
	"""Run ``import_row`` in a DB savepoint; rollback and re-raise on failure.

	Frappe's ``savepoint()`` context manager catches ``Exception`` by default and does
	not re-raise, which would leave ``pdc_name`` unset in the caller.
	"""
	sp = f"coi_r{row_no}"
	frappe.db.savepoint(sp)
	try:
		return import_row(row_no, row)
	except Exception:
		frappe.db.rollback(save_point=sp)
		raise
	frappe.db.release_savepoint(sp)


class ChequeOpeningImport(Document):
	@frappe.whitelist()
	def preview(self):
		"""Alias for client-side `frm.call("preview")`."""
		return self.preview_file()

	@frappe.whitelist()
	def preview_file(self):
		self.check_permission("write")
		if not self.name:
			frappe.throw(
				frappe._("Save the document (with file attached) before Preview."), title=frappe._("Preview")
			)
		if not self.import_file:
			frappe.throw(frappe._("Attach a file first."), title=frappe._("Preview"))

		logger = frappe.logger("cheque_opening_import")
		logger.info("Preview start: doc=%s file_url=%s", self.name, self.import_file)

		self.set("items", [])
		valid = 0
		failed = 0

		try:
			rows = _get_sheet_from_file_url(self.import_file)
			parsed = _rows_from_sheet(rows)
			logger.info("Preview parsed: data_rows=%s", len(parsed))
		except Exception as e:
			logger.error("Preview parse failed: %s", e, exc_info=True)
			msg = cstr(e)
			self.append(
				"items",
				{
					"row_number": 0,
					"row_status": "Failed",
					"validation_message": "Parse failed",
					"error_detail": msg[:200],
				},
			)
			self.import_status = "Draft"
			self.summary = frappe._("Preview failed: {0}").format(msg)
			self.save()
			return {"total": 0, "valid": 0, "failed": 1, "summary": self.summary}

		if not parsed:
			self.import_status = "Draft"
			self.summary = frappe._("No data rows found in file.")
			self.append(
				"items",
				{
					"row_number": 0,
					"row_status": "Failed",
					"validation_message": "No data rows found in file.",
					"error_detail": "",
				},
			)
			self.save()
			return {"total": 0, "valid": 0, "failed": 1, "summary": self.summary}

		for row_no, row in parsed:
			err = _validate_row_preview(row, row_no)
			if err:
				failed += 1
				self.append(
					"items",
					{
						"row_number": row_no,
						"row_status": "Failed",
						"validation_message": err[:140],
						"error_detail": err[:200],
					},
				)
			else:
				valid += 1
				self.append(
					"items",
					{
						"row_number": row_no,
						"row_status": "Valid",
						"validation_message": f"OK → {cstr(row.get('workflow_state'))}",
					},
				)

		self.import_status = "Previewed"
		self.summary = frappe._("Preview: {0} data row(s), {1} valid, {2} failed.").format(
			len(parsed), valid, failed
		)
		self.save()
		return {"total": len(parsed), "valid": valid, "failed": failed, "summary": self.summary}

	@frappe.whitelist()
	def execute_import(self):
		self.check_permission("write")
		if not self.name:
			frappe.throw(frappe._("Save the document first."), title=frappe._("Import"))
		if not self.import_file:
			frappe.throw(frappe._("Attach a file first."), title=frappe._("Import"))
		if (self.import_status or "").strip() in ("Completed", "Completed With Errors"):
			frappe.throw(
				frappe._(
					"This import has already been completed. Change the file and Preview again to re-run."
				),
				title=frappe._("Import"),
			)
		if (self.import_status or "").strip() != "Previewed":
			frappe.throw(
				frappe._("Run Preview first before Execute Import."),
				title=frappe._("Import"),
			)

		rows = _get_sheet_from_file_url(self.import_file)
		parsed = _rows_from_sheet(rows)

		# Guard: must have at least one valid row before executing.
		valid_candidates = 0
		for row_no, row in parsed:
			if not _validate_row_preview(row, row_no):
				valid_candidates += 1
				break
		if valid_candidates == 0:
			frappe.throw(
				frappe._("No valid rows found. Fix the file and run Preview again."),
				title=frappe._("Import"),
			)

		ok = 0
		bad = 0
		log_lines: list[str] = []

		# Pass current import doc id into row importer for traceability.
		frappe.flags.cheque_opening_import_name = self.name

		self.items = []
		for row_no, row in parsed:
			pre = _validate_row_preview(row, row_no)
			if pre:
				bad += 1
				log_lines.append(pre)
				self.append(
					"items",
					{
						"row_number": row_no,
						"row_status": "Failed",
						"validation_message": "Skipped",
						"error_detail": pre[:200],
					},
				)
				continue

			pdc_name = None
			try:
				pdc_name = _import_row_with_savepoint(row_no, row)
				ok += 1
				self.append(
					"items",
					{
						"row_number": row_no,
						"row_status": "Imported",
						"validation_message": pdc_name,
						"imported_pdc": pdc_name,
						"post_dated_cheque": pdc_name,
					},
				)
				log_lines.append(f"Row {row_no}: OK → {pdc_name}")
			except Exception as e:
				bad += 1
				msg = f"Row {row_no}: {e!s}"
				log_lines.append(msg)
				frappe.log_error(title=f"Cheque Opening Import row {row_no}", message=frappe.get_traceback())
				self.append(
					"items",
					{
						"row_number": row_no,
						"row_status": "Failed",
						"validation_message": cstr(e)[:140],
						"error_detail": cstr(e)[:200],
					},
				)

		details = {
			"total": len(parsed),
			"imported": ok,
			"failed": bad,
			"lines": log_lines,
		}
		human = [
			f"Imported: {ok}",
			f"Failed: {bad}",
			f"Total: {len(parsed)}",
			"",
			"Details (JSON):",
			json.dumps(details, indent=2, default=str),
		]
		self.summary = "\n".join(human)
		self.import_status = "Completed" if bad == 0 else "Completed With Errors"
		self.save()

		return {"imported": ok, "failed": bad, "total": len(parsed), "summary": self.summary}


TEMPLATE_HEADERS: list[str] = [
	"cheque_direction",
	"company",
	"bank_account",
	"cheque_book",
	"cheque_number",
	"cheque_due_date",
	"cheque_amount",
	"party_type",
	"party",
	"workflow_state",
	"cheque_purpose",
	"drawer_bank_name",
	"received_date",
	"sent_to_bank_date",
	"handover_date",
	"cleared_date",
	"bounced_date",
	"returned_date",
	"sayad_code",
	"sayad_registered",
]


@frappe.whitelist()
def download_import_template():
	"""Download a three-sheet **.xlsx** template (Data + Instructions + Allowed Values)."""
	if frappe.session.user == "Guest":
		frappe.throw(frappe._("Login required."), frappe.PermissionError)

	import openpyxl
	from openpyxl.styles import Font

	wb = openpyxl.Workbook()
	ws_data = wb.active
	ws_data.title = "Data"

	bold = Font(bold=True)
	ws_data.append(TEMPLATE_HEADERS)
	for cell in ws_data[1]:
		cell.font = bold

	# Sample A — Receivable, Draft (replace placeholders with real Link names from your site).
	ws_data.append(
		[
			CHEQUE_DIRECTION_RECEIVABLE,
			"__REPLACE_COMPANY__",
			"",
			"",
			"OB-REC-0001",
			date(2026, 6, 1),
			5000.0,
			"Customer",
			"__REPLACE_CUSTOMER__",
			"Draft",
			"Collection for April sales",
			"__REPLACE_DRAWER_BANK__",
			date(2026, 1, 15),
			None,
			None,
			None,
			None,
			None,
			None,
			None,
		]
	)

	# Sample B — Payable, Registered, with book + number to match a Cheque Leaf.
	ws_data.append(
		[
			CHEQUE_DIRECTION_PAYABLE,
			"__REPLACE_COMPANY__",
			"__REPLACE_BANK_ACCOUNT__",
			"__REPLACE_CHEQUE_BOOK__",
			"101",
			date(2026, 6, 15),
			10000.0,
			"Supplier",
			"__REPLACE_SUPPLIER__",
			"Registered",
			"Settlement of Purchase Invoice PI-00045",
			"",
			date(2026, 1, 10),
			None,
			None,
			None,
			None,
			None,
			None,
			None,
		]
	)

	for col in range(1, len(TEMPLATE_HEADERS) + 1):
		ws_data.column_dimensions[openpyxl.utils.get_column_letter(col)].width = 18

	ws_inst = wb.create_sheet("Instructions", 1)
	ws_inst.column_dimensions["A"].width = 110
	instructions = [
		"Cheque Opening Import — how to use this template",
		"",
		"Receivable: does NOT use cheque_leaf or cheque_book. Leave bank_account empty while in Draft; it becomes required for certain later workflow states (e.g. Sent to Bank). drawer_bank_name must be a valid Bank record (Link).",
		"",
		"Payable: you may set cheque_book + cheque_number to match an existing Cheque Leaf (same company and bank_account as the leaf). If the leaf is not found, the row will fail validation. This template does NOT create Cheque Books or leaves.",
		"",
		"Opening balance / GL: this tool only creates or updates Post Dated Cheque documents through the normal PDC save/submit path. It does NOT pick Opening Balance or Temporary Opening accounts here. Chart-of-accounts opening entries (including any auditor-required opening JEs) must be handled separately in Accounting unless a future enhancement adds dedicated import JEs.",
		"",
		"Journal entries: Cheque Opening Import does not post lifecycle Journal Entries during import (opening snapshot only). Post-import workflow changes on the PDC create JEs as usual. Chart opening / trial balance JEs remain separate in Accounting.",
		"",
		"Sayad Code (sayad_code): optional official Sayad tracking code. Aliases: Sayad Code, کد صیادی, شماره صیادی.",
		"Sayad Registered (sayad_registered): whether the cheque is registered in the Sayad system (1/0, yes/no, true/false, بله/خیر, etc.). Aliases: Sayad Registered, ثبت صیادی, ثبت شده در صیاد, وضعیت ثبت صیاد.",
		"If Sayad Registered is blank: when Sayad Code is filled, registered defaults to 1; when code is empty, registered defaults to 0. A non-empty registered value always wins over the default.",
		"",
		"Cheque Purpose (cheque_purpose): optional free-text business purpose / subject of the cheque. Aliases: purpose, بابت, Cheque Purpose. Omit the column entirely for older files — import still works and leaves the field empty.",
		"",
		"Replace all __REPLACE_*__ placeholders with real values from your site (Company, Bank Account, Customer/Supplier, Cheque Book, Drawer Bank, etc.) before Preview / Execute.",
	]
	for line in instructions:
		ws_inst.append([line])

	ws_allow = wb.create_sheet("Allowed Values", 2)
	ws_allow.append(["Field", "Values / notes"])
	for c in ws_allow[1]:
		c.font = bold
	ws_allow.column_dimensions["A"].width = 28
	ws_allow.column_dimensions["B"].width = 90
	ws_allow.append(["cheque_direction", f"{CHEQUE_DIRECTION_RECEIVABLE}, {CHEQUE_DIRECTION_PAYABLE}"])
	ws_allow.append(
		[
			"workflow_state (examples; full set must be valid for the chosen direction)",
			", ".join(ALL_WORKFLOW_STATES),
		]
	)
	ws_allow.append(
		[
			"party_type (common)",
			"Customer, Supplier, Employee, Shareholder (must match Party master)",
		]
	)
	ws_allow.append(
		[
			"sayad_code / Sayad Code",
			"Optional. Official Sayad tracking code.",
		]
	)
	ws_allow.append(
		[
			"sayad_registered / Sayad Registered",
			"Optional. 1/0, yes/no, true/false, y/n, checked/unchecked, بله/خیر, درست/نادرست, ثبت شده/ثبت نشده. Blank: default 1 if sayad_code set, else 0.",
		]
	)

	buf = io.BytesIO()
	wb.save(buf)
	buf.seek(0)

	frappe.local.response.filename = "cheque_opening_import_template.xlsx"
	frappe.local.response.filecontent = buf.getvalue()
	frappe.local.response.type = "download"

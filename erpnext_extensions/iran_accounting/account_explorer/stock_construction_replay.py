# Copyright (c) 2026, Farbod Siyahpoosh and contributors
"""Optional diagnostic: native stock GL legs before merge_similar_entries.

READ-ONLY. Never calls make_gl_entries / save / submit / db_set / repost.

v5.1.1 FINAL BUSINESS CONTRACT: Account Levels under Item / Item Group /
Warehouse filters use REAL posted tabGL Entry rows of vouchers that have at
least one scoped SLE (EXISTS bridge). This module is NOT the Account summary
engine — kept only for optional Constructed Legs diagnostics / forensic.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import frappe
from frappe.utils import cint, flt, getdate

from erpnext_extensions.iran_accounting.account_explorer.inventory_scope import (
	has_inventory_document_filters,
	resolve_inventory_scope,
)
from erpnext_extensions.iran_accounting.account_explorer.measures import measures_from_opening_period
from erpnext_extensions.iran_accounting.account_explorer.opening_entry_policy import (
	OpeningEntryPolicyMode,
	policy_from_spec,
)
from erpnext_extensions.iran_accounting.account_explorer.schemas import AccountExplorerQuerySpec

ACCOUNT_FACT_ENGINE_POSTED = "posted_gl"
ACCOUNT_FACT_ENGINE_VOUCHER_SCOPED = "voucher_scoped_gl"  # deprecated for Account summary
ACCOUNT_FACT_ENGINE_SLE_SCOPED = "sle_scoped_stock"
# Deprecated label — never selected for Account summary.
ACCOUNT_FACT_ENGINE_CONSTRUCTION = "stock_construction_replay"

SUPPORTED_STOCK_VOUCHER_TYPES = frozenset(
	{
		"Stock Entry",
		"Stock Reconciliation",
		"Purchase Receipt",
		"Delivery Note",
		"Purchase Invoice",
		"Sales Invoice",
	}
)

# Purchase/Sales Invoice without update_stock are not stock construction sources.
STOCK_UPDATE_INVOICE_TYPES = frozenset({"Purchase Invoice", "Sales Invoice"})


@dataclass
class ConstructedLeg:
	account: str
	debit: float
	credit: float
	against: str | None
	voucher_type: str
	voucher_no: str
	voucher_detail_no: str | None
	posting_date: str
	sle_names: list[str] = field(default_factory=list)
	item_code: str | None = None
	item_group: str | None = None
	warehouse: str | None = None
	construction_rule: str = "native.get_gl_entries"
	rule_tag: str = "pre_merge"
	native_source_path: str = "StockController.get_gl_entries→process_gl_map(merge_entries=False)"
	is_opening: str = "No"
	cost_center: str | None = None
	remarks: str | None = None

	def as_dict(self) -> dict:
		return {
			"account": self.account,
			"debit": self.debit,
			"credit": self.credit,
			"against": self.against,
			"voucher_type": self.voucher_type,
			"voucher_no": self.voucher_no,
			"voucher_detail_no": self.voucher_detail_no,
			"posting_date": str(self.posting_date) if self.posting_date else None,
			"sle_names": list(self.sle_names),
			"item_code": self.item_code,
			"item_group": self.item_group,
			"warehouse": self.warehouse,
			"construction_rule": self.construction_rule,
			"rule_tag": self.rule_tag,
			"native_source_path": self.native_source_path,
			"is_opening": self.is_opening,
			"cost_center": self.cost_center,
			"remarks": self.remarks,
		}


@dataclass
class ConstructionResult:
	legs: list[ConstructedLeg]
	warnings: list[str] = field(default_factory=list)
	unsupported: list[dict] = field(default_factory=list)
	voucher_count: int = 0
	sle_count: int = 0
	incomplete: bool = False
	engine: str = ACCOUNT_FACT_ENGINE_CONSTRUCTION

	@property
	def ready(self) -> bool:
		return (not self.incomplete) and (not self.unsupported)


def select_account_fact_engine(spec: AccountExplorerQuerySpec) -> str:
	"""Delegate to canonical selector in ``sle_scoped_account`` (Case A/B)."""
	from erpnext_extensions.iran_accounting.account_explorer.sle_scoped_account import (
		select_account_fact_engine as _canonical,
	)

	return _canonical(spec)


def _opening_flag(value) -> str:
	return "Yes" if str(value or "No") == "Yes" else "No"


def _normalize_gl_amounts(row: dict) -> tuple[float, float]:
	"""Match process_gl_map toggle_debit_credit_if_negative semantics for signed builders."""
	debit = flt(row.get("debit"))
	credit = flt(row.get("credit"))
	if debit < 0 and credit == 0:
		return 0.0, abs(debit)
	if credit < 0 and debit == 0:
		return abs(credit), 0.0
	if debit < 0:
		credit = credit - debit
		debit = 0.0
	if credit < 0:
		debit = debit - credit
		credit = 0.0
	return flt(debit), flt(credit)


@contextmanager
def _process_gl_map_without_merge():
	"""Force merge_entries=False on every process_gl_map binding used by stock construction."""
	import erpnext.accounts.general_ledger as gl_mod
	import erpnext.controllers.stock_controller as sc_mod

	original = gl_mod.process_gl_map

	def _no_merge(gl_map, merge_entries=True, precision=None, from_repost=False):
		return original(gl_map, merge_entries=False, precision=precision, from_repost=from_repost)

	targets: list[tuple[Any, str, Any]] = []
	patches = [(gl_mod, "process_gl_map"), (sc_mod, "process_gl_map")]
	for mod_path in (
		"erpnext_extensions.iran_accounting.zero_value_transfer",
		"erpnext_extensions.iran_accounting.domain.stock_reconciliation_gl",
	):
		try:
			mod = frappe.get_module(mod_path)
			patches.append((mod, "process_gl_map"))
		except Exception:
			pass

	for mod, name in patches:
		if hasattr(mod, name):
			targets.append((mod, name, getattr(mod, name)))
			setattr(mod, name, _no_merge)
	try:
		yield
	finally:
		for mod, name, orig in targets:
			setattr(mod, name, orig)


@contextmanager
def _scoped_stock_ledger_details(doc, scoped_sle_names: set[str]):
	"""Restrict StockController SLE map to in-scope rows (read-only monkeypatch)."""
	if not hasattr(doc, "get_stock_ledger_details"):
		yield
		return
	original = doc.get_stock_ledger_details

	def _filtered():
		full = original()
		out = {}
		for detail_no, sle_list in (full or {}).items():
			kept = [s for s in (sle_list or []) if s.get("name") in scoped_sle_names]
			if kept:
				out[detail_no] = kept
		return out

	doc.get_stock_ledger_details = _filtered
	try:
		yield
	finally:
		doc.get_stock_ledger_details = original


@contextmanager
def _scoped_purchase_receipt_items(doc, scoped_detail_nos: set[str]):
	"""Limit PR/PI item rows to scoped voucher_detail_no for stock-item GL only."""
	if doc.doctype not in {"Purchase Receipt", "Purchase Invoice"}:
		yield
		return
	if not hasattr(doc, "items"):
		yield
		return
	original_items = list(doc.items)
	doc.set("items", [row for row in original_items if row.name in scoped_detail_nos])
	try:
		yield
	finally:
		doc.set("items", original_items)


def _load_stock_doc(voucher_type: str, voucher_no: str):
	if not frappe.db.exists(voucher_type, voucher_no):
		return None
	doc = frappe.get_doc(voucher_type, voucher_no)
	doc.flags.ignore_permissions = True
	doc.flags.ignore_links = True
	# Analytical only — never persist
	doc.flags.ignore_validate = True
	return doc


def invoke_native_pre_merge_gl_entries(
	doc,
	*,
	scoped_sle_names: set[str] | None = None,
	scoped_detail_nos: set[str] | None = None,
) -> list[dict]:
	"""Call doctype get_gl_entries with merge disabled. Optional SLE scope. READ-ONLY."""
	if not doc:
		return []

	use_scope = bool(scoped_sle_names)
	scoped_sle_names = scoped_sle_names or set()
	scoped_details = set(scoped_detail_nos or ())
	if use_scope and not scoped_details:
		# Caller should pass detail nos from SLE rows; keep empty → item filter no-op
		pass

	with _process_gl_map_without_merge():
		with _scoped_stock_ledger_details(doc, scoped_sle_names) if use_scope else _nullctx():
			with _scoped_purchase_receipt_items(doc, scoped_details) if (
				use_scope and scoped_details
			) else _nullctx():
				entries = _call_get_gl_entries(doc)
	out = []
	for row in entries or []:
		d = dict(row) if not isinstance(row, dict) else dict(row)
		debit, credit = _normalize_gl_amounts(d)
		# Skip no-ops after toggle
		if abs(debit) < 1e-9 and abs(credit) < 1e-9:
			continue
		d["debit"] = debit
		d["credit"] = credit
		out.append(d)
	return out


@contextmanager
def _nullctx():
	yield


def _call_get_gl_entries(doc) -> list:
	"""Dispatch native (site-patched) get_gl_entries without persistence."""
	if doc.doctype == "Purchase Receipt":
		# Stock-accounting legs only — skip tax/payable (not SLE stock construction).
		from erpnext.accounts.general_ledger import process_gl_map

		gl_entries: list = []
		inventory_account_map = None
		if hasattr(doc, "get_inventory_account_map"):
			inventory_account_map = doc.get_inventory_account_map()
		doc.make_item_gl_entries(gl_entries, inventory_account_map=inventory_account_map)
		if hasattr(doc, "set_gl_entry_for_purchase_expense"):
			try:
				doc.set_gl_entry_for_purchase_expense(gl_entries)
			except Exception:
				pass
		try:
			from erpnext.stock.doctype.purchase_receipt.purchase_receipt import (
				update_regional_gl_entries,
			)

			update_regional_gl_entries(gl_entries, doc)
		except Exception:
			pass
		return process_gl_map(gl_entries, from_repost=False)

	inventory_account_map = None
	if hasattr(doc, "get_inventory_account_map"):
		try:
			inventory_account_map = doc.get_inventory_account_map()
		except Exception:
			inventory_account_map = None

	try:
		return doc.get_gl_entries(inventory_account_map)
	except TypeError:
		try:
			return doc.get_gl_entries()
		except TypeError:
			return doc.get_gl_entries(inventory_account_map, None, None)


def _scoped_sle_rows(spec: AccountExplorerQuerySpec) -> list[dict]:
	scope = resolve_inventory_scope(spec)
	if not scope.is_inventory_constrained:
		return []

	conditions = ["company=%(company)s"]
	values: dict[str, Any] = {"company": spec.company}
	if not spec.include_cancelled_entries:
		conditions.append("is_cancelled=0")

	if scope.item_codes is not None:
		if not scope.item_codes:
			return []
		conditions.append("item_code in %(items)s")
		values["items"] = tuple(list(scope.item_codes)[:8000])
	if scope.warehouses:
		conditions.append("warehouse in %(warehouses)s")
		values["warehouses"] = tuple(scope.warehouses)
	if spec.to_date:
		conditions.append("posting_date <= %(to_date)s")
		values["to_date"] = str(spec.to_date)

	return (
		frappe.db.sql(
			f"""
			select name, item_code, warehouse, voucher_type, voucher_no, voucher_detail_no,
			       actual_qty, stock_value_difference, posting_date, is_cancelled, company
			from `tabStock Ledger Entry`
			where {' and '.join(conditions)}
			order by posting_date, voucher_type, voucher_no, creation
			""",
			values,
			as_dict=True,
		)
		or []
	)


def _item_group_map(item_codes: list[str]) -> dict[str, str]:
	if not item_codes:
		return {}
	rows = frappe.db.sql(
		"select name, item_group from `tabItem` where name in %s",
		(tuple(item_codes),),
		as_dict=True,
	)
	return {r.name: r.item_group for r in rows}


def _legs_from_gl_rows(
	gl_rows: list[dict],
	*,
	voucher_type: str,
	voucher_no: str,
	scoped_sles: list[dict],
	item_groups: dict[str, str],
	native_source_path: str,
) -> list[ConstructedLeg]:
	"""Attach provenance to constructed GL rows for a voucher.

	When native construction emits inventory+contra pairs in SLE order (2 legs
	per SLE), zip pairs to the scoped SLE list for exact Item/Warehouse/SLE
	traceability.
	"""
	sle_by_detail: dict[str, list[dict]] = {}
	for s in scoped_sles:
		if s.voucher_detail_no:
			sle_by_detail.setdefault(s.voucher_detail_no, []).append(s)

	# Prefer ordered pairing when counts match 2:1 (inventory + contra per SLE)
	ordered_sles = list(scoped_sles)
	pair_mode = len(gl_rows) == 2 * len(ordered_sles) and len(ordered_sles) > 0

	legs: list[ConstructedLeg] = []
	for idx, g in enumerate(gl_rows):
		vdn = g.get("voucher_detail_no")
		sles = sle_by_detail.get(vdn) if vdn else []
		sle0 = None
		if pair_mode:
			sle0 = ordered_sles[idx // 2]
			sles = [sle0]
		elif sles:
			sle0 = sles[0]
		elif len(ordered_sles) == 1:
			sle0 = ordered_sles[0]
			sles = [sle0]

		item_code = sle0.item_code if sle0 else None
		warehouse = sle0.warehouse if sle0 else None
		if not item_code:
			codes = sorted({s.item_code for s in ordered_sles if s.item_code})
			if len(codes) == 1:
				item_code = codes[0]
		if not sles and ordered_sles:
			sles = list(ordered_sles)
		if not warehouse and len({s.warehouse for s in ordered_sles if s.warehouse}) == 1:
			warehouse = ordered_sles[0].warehouse
		legs.append(
			ConstructedLeg(
				account=g.get("account"),
				debit=flt(g.get("debit")),
				credit=flt(g.get("credit")),
				against=g.get("against"),
				voucher_type=voucher_type,
				voucher_no=voucher_no,
				voucher_detail_no=vdn or (sle0.voucher_detail_no if sle0 else None),
				posting_date=str(g.get("posting_date") or (sle0.posting_date if sle0 else "") or ""),
				sle_names=[s.name for s in sles] if sles else ([sle0.name] if sle0 else []),
				item_code=item_code,
				item_group=item_groups.get(item_code) if item_code else None,
				warehouse=warehouse,
				construction_rule="native.get_gl_entries",
				rule_tag="sle_pair" if pair_mode else "scoped_sle_pre_merge",
				native_source_path=native_source_path,
				is_opening=_opening_flag(g.get("is_opening")),
				cost_center=g.get("cost_center"),
				remarks=g.get("remarks"),
			)
		)
	return legs


def _native_source_path_for(voucher_type: str) -> str:
	if voucher_type == "Stock Entry":
		return "iran_stock_entry_get_gl_entries|StockEntry.get_gl_entries→process_gl_map(merge=False)"
	if voucher_type == "Stock Reconciliation":
		return "get_stock_reconciliation_gl_entries→process_gl_map(merge=False)"
	if voucher_type == "Purchase Receipt":
		return "PurchaseReceipt.make_item_gl_entries→process_gl_map(merge=False)"
	return f"{voucher_type}.get_gl_entries→process_gl_map(merge=False)"


def _ordered_scoped_sles(doc, scoped_names: set[str], fallback: list[dict]) -> list[dict]:
	"""SLE order matching StockController voucher_details → sle_list iteration."""
	if not doc or not hasattr(doc, "get_stock_ledger_details"):
		return list(fallback)
	by_name = {s.name: s for s in fallback}
	ordered: list[dict] = []
	try:
		sle_map = doc.get_stock_ledger_details() or {}
	except Exception:
		return list(fallback)
	for _detail, sle_list in sle_map.items():
		for sle in sle_list or []:
			name = sle.get("name") if hasattr(sle, "get") else getattr(sle, "name", None)
			if name in scoped_names and name in by_name:
				ordered.append(by_name[name])
	return ordered or list(fallback)


def build_constructed_legs_for_spec(spec: AccountExplorerQuerySpec) -> ConstructionResult:
	"""Build Account analytical legs from scoped native stock construction."""
	result = ConstructionResult(legs=[], warnings=[], unsupported=[], voucher_count=0, sle_count=0)
	if select_account_fact_engine(spec) != ACCOUNT_FACT_ENGINE_CONSTRUCTION:
		result.engine = ACCOUNT_FACT_ENGINE_POSTED
		return result

	sles = _scoped_sle_rows(spec)
	result.sle_count = len(sles)
	if not sles:
		return result

	by_voucher: dict[tuple[str, str], list[dict]] = {}
	for s in sles:
		by_voucher.setdefault((s.voucher_type, s.voucher_no), []).append(s)

	item_groups = _item_group_map(sorted({s.item_code for s in sles if s.item_code}))
	result.voucher_count = len(by_voucher)

	# Request-local doc cache to avoid N+1 reloads when measures + detail share a request
	doc_cache: dict[tuple[str, str], Any] = {}

	for (voucher_type, voucher_no), scoped in by_voucher.items():
		if voucher_type not in SUPPORTED_STOCK_VOUCHER_TYPES:
			result.unsupported.append(
				{"voucher_type": voucher_type, "voucher_no": voucher_no, "reason": "unsupported_doctype"}
			)
			result.incomplete = True
			continue

		if voucher_type in STOCK_UPDATE_INVOICE_TYPES:
			upd = frappe.db.get_value(voucher_type, voucher_no, "update_stock")
			if not cint(upd):
				result.unsupported.append(
					{
						"voucher_type": voucher_type,
						"voucher_no": voucher_no,
						"reason": "invoice_without_update_stock",
					}
				)
				result.incomplete = True
				continue

		key = (voucher_type, voucher_no)
		doc = doc_cache.get(key)
		if doc is None:
			doc = _load_stock_doc(voucher_type, voucher_no)
			doc_cache[key] = doc
		if not doc:
			result.unsupported.append(
				{"voucher_type": voucher_type, "voucher_no": voucher_no, "reason": "missing_document"}
			)
			result.incomplete = True
			continue

		scoped_names = {s.name for s in scoped}
		scoped_details = {s.voucher_detail_no for s in scoped if s.voucher_detail_no}
		try:
			gl_rows = invoke_native_pre_merge_gl_entries(
				doc, scoped_sle_names=scoped_names, scoped_detail_nos=scoped_details
			)
		except Exception as exc:
			result.unsupported.append(
				{
					"voucher_type": voucher_type,
					"voucher_no": voucher_no,
					"reason": f"construction_error: {exc}",
				}
			)
			result.incomplete = True
			result.warnings.append(f"{voucher_type} {voucher_no}: {exc}")
			continue

		# Fail closed: scoped SLE with nonzero value but zero constructed inventory movement
		nonzero_svd = [s for s in scoped if abs(flt(s.stock_value_difference)) > 0.0001]
		if nonzero_svd and not gl_rows:
			result.unsupported.append(
				{
					"voucher_type": voucher_type,
					"voucher_no": voucher_no,
					"reason": "empty_construction_for_valued_sle",
					"sle_count": len(nonzero_svd),
				}
			)
			result.incomplete = True
			result.warnings.append(
				f"No constructed legs for valued SLE on {voucher_type} {voucher_no}"
			)
			continue

		legs = _legs_from_gl_rows(
			gl_rows,
			voucher_type=voucher_type,
			voucher_no=voucher_no,
			scoped_sles=_ordered_scoped_sles(doc, scoped_names, scoped),
			item_groups=item_groups,
			native_source_path=_native_source_path_for(voucher_type),
		)
		result.legs.extend(legs)

	# Stash for summary response / detail API within same request
	frappe.flags.ae_construction_result = result
	return result


def get_constructed_legs_cached(spec: AccountExplorerQuerySpec) -> ConstructionResult:
	"""Reuse construction within the same request (summary + detail)."""
	cached = getattr(frappe.flags, "ae_construction_result", None)
	if cached is not None:
		return cached
	return build_constructed_legs_for_spec(spec)

def aggregate_constructed_account_measures(
	spec: AccountExplorerQuerySpec, account_names: list[str] | None = None
) -> tuple[dict[str, dict], ConstructionResult]:
	"""Opening/period Account measures from constructed legs (side-present opening)."""
	construction = get_constructed_legs_cached(spec)
	from_date = getdate(spec.from_date)
	to_date = getdate(spec.to_date)
	policy = policy_from_spec(spec)

	opening: dict[str, list[float]] = {}
	period: dict[str, list[float]] = {}

	def add(bucket, account, debit, credit):
		cur = bucket.setdefault(account, [0.0, 0.0])
		cur[0] += flt(debit)
		cur[1] += flt(credit)

	for leg in construction.legs:
		if not leg.account:
			continue
		posting = getdate(leg.posting_date) if leg.posting_date else None
		if posting is None:
			continue
		is_open_flag = leg.is_opening == "Yes"
		if posting < from_date:
			add(opening, leg.account, leg.debit, leg.credit)
		elif from_date <= posting <= to_date:
			if policy == OpeningEntryPolicyMode.EXCLUDE_OPENING_FLAGGED and is_open_flag:
				continue
			add(period, leg.account, leg.debit, leg.credit)

	result: dict[str, dict] = {}
	accounts = set(opening) | set(period)
	if account_names is not None:
		# Ensure requested accounts appear (zeros) for hierarchy completeness when needed
		pass
	for account in accounts:
		od, oc = opening.get(account, [0.0, 0.0])
		pd, pc = period.get(account, [0.0, 0.0])
		onet = flt(od) - flt(oc)
		if onet >= 0:
			od, oc = onet, 0.0
		else:
			od, oc = 0.0, abs(onet)
		result[account] = measures_from_opening_period(od, oc, pd, pc)
	return result, construction


def get_accounts_with_constructed_postings(
	spec: AccountExplorerQuerySpec, group_account_names: set[str]
) -> set[str]:
	if not group_account_names:
		return set()
	construction = get_constructed_legs_cached(spec)
	posted = {leg.account for leg in construction.legs if leg.account}
	return posted & set(group_account_names)


def native_pre_merge_parity_rows(voucher_type: str, voucher_no: str) -> list[dict]:
	"""Full-voucher pre-merge native legs (no SLE filter) for doctype parity tests."""
	doc = _load_stock_doc(voucher_type, voucher_no)
	if not doc:
		return []
	return invoke_native_pre_merge_gl_entries(doc, scoped_sle_names=None)


def construction_meta_dict(construction: ConstructionResult) -> dict:
	return {
		"account_fact_engine": construction.engine,
		"construction_ready": int(construction.ready),
		"construction_incomplete": int(construction.incomplete),
		"construction_voucher_count": construction.voucher_count,
		"construction_sle_count": construction.sle_count,
		"construction_leg_count": len(construction.legs),
		"construction_warnings": list(construction.warnings)[:50],
		"construction_unsupported": list(construction.unsupported)[:50],
		"construction_label": "Stock accounting construction for selected items",
	}

# Copyright (c) 2026, ERPNext Extensions contributors

from __future__ import annotations

import erpnext
import frappe
from frappe.model.meta import get_field_precision
from frappe.utils import cint, flt

import erpnext_extensions.iran_accounting.zero_value_transfer as zvt
from erpnext_extensions.iran_accounting.domain.currency import get_currency_precision
from erpnext_extensions.iran_accounting.domain.ledger_rounding import round_gl_entry_amounts

_PATCHED = False


def apply_monkey_patches():
	global _PATCHED
	from erpnext_extensions.iran_accounting.worker.guard import ensure_runtime_ready

	ensure_runtime_ready()
	if not _PATCHED:
		_PATCHED = True
		_patch_stock_controller()
		_patch_stock_entry()
		_patch_stock_entry_mr_alternative()
		_patch_general_ledger()
		_patch_accounts_controller()
		_patch_stock_ledger_report()
		_patch_accounting_ledger_preview()
		_patch_stock_reconciliation()
		_patch_repost_compatibility()
		_patch_buying_regional_valuation_rate()
	# Always (re)ensure stock-ledger engine patches — idempotent + fail-closed upgrade guard.
	_patch_stock_ledger_engine()
	# Idempotent: UVR regional hook must stay bound even if apply is re-entered.
	_patch_buying_regional_valuation_rate()


def _patch_buying_regional_valuation_rate():
	"""Bind IRR align to ERPNext update_regional_item_valuation_rate (end of UVR).

	This is the single valuation integerization pipeline for PR / LCV / RIV / PI
	update-stock — not an LCV-specific workaround. Reuses
	align_purchase_receipt_item_amounts (no duplicated rounding).

	Fail-closed: assert_erpnext_uvr_regional_patch_supported() must pass before
	install (same architecture as riv_rate_guard / stock-ledger engine).
	"""
	import erpnext.controllers.buying_controller as buying_controller

	from erpnext_extensions.iran_accounting.domain.uvr_regional_guard import (
		assert_erpnext_uvr_regional_patch_supported,
	)

	# Fail-closed upgrade guard before (re)installing the regional UVR binding.
	assert_erpnext_uvr_regional_patch_supported()

	if getattr(buying_controller, "_iran_patched_regional_valuation_rate", False):
		return

	from erpnext_extensions.iran_accounting.buying_selling import (
		update_regional_item_valuation_rate as iran_update_regional_item_valuation_rate,
	)

	buying_controller._iran_original_regional_valuation_rate = (
		buying_controller.update_regional_item_valuation_rate
	)
	buying_controller.update_regional_item_valuation_rate = iran_update_regional_item_valuation_rate
	buying_controller._iran_patched_regional_valuation_rate = True


def _patch_stock_entry_mr_alternative():
	from erpnext_extensions.stock_extensions.mr_alternative_item import apply_patch

	apply_patch()


def _run_irr_pipeline_after_ral(account_repost_doc: str) -> None:
	"""After RAL engine work finishes, re-assert IRR deterministic truth for stock vouchers."""
	from erpnext_extensions.iran_accounting.domain.currency import is_irr_company
	from erpnext_extensions.iran_accounting.domain.repost_determinism import (
		run_post_repost_deterministic_pipeline,
	)

	try:
		repost_doc = frappe.get_doc("Repost Accounting Ledger", account_repost_doc)
	except Exception:
		return

	for row in repost_doc.get("vouchers") or []:
		# ERPNext 16.31.1+ tracks per-voucher status; skip non-success rows when present.
		row_status = getattr(row, "status", None)
		if row_status in ("Failed", "Skipped"):
			continue
		company = frappe.db.get_value(row.voucher_type, row.voucher_no, "company")
		if not company or not is_irr_company(company):
			continue
		if row.voucher_type not in ("Stock Reconciliation", "Stock Entry"):
			continue
		try:
			doc = frappe.get_doc(row.voucher_type, row.voucher_no)
			run_post_repost_deterministic_pipeline(doc, raise_on_fail=False)
		except Exception:
			frappe.log_error(
				title="IRR accounting repost reconcile failed",
				message=frappe.get_traceback(),
			)


def _patch_ral_module_level_repost(ral_mod) -> bool:
	"""Wrap ERPNext 16.31.1+ module-level ``repost`` (actual ledger work / background job)."""
	if getattr(ral_mod, "_iran_patched_ral_repost", None):
		return True
	if not callable(getattr(ral_mod, "repost", None)):
		return False

	_orig_repost = ral_mod.repost

	def repost(repost_doc_name: str, commit: bool = True):
		try:
			return _orig_repost(repost_doc_name, commit=commit)
		finally:
			if repost_doc_name:
				_run_irr_pipeline_after_ral(repost_doc_name)

	ral_mod.repost = repost
	ral_mod._iran_patched_ral_repost = True
	return True


def _patch_ral_module_level_start_repost(ral_mod) -> bool:
	"""Wrap legacy module-level ``start_repost`` (pre-16.31.1 synchronous RAL worker)."""
	if getattr(ral_mod, "_iran_patched_start_repost", None):
		return True
	# Document.method ``start_repost`` is not a module attribute — require a real module function.
	start_fn = getattr(ral_mod, "start_repost", None)
	if not callable(start_fn):
		return False
	# Class methods live on RepostAccountingLedger; skip if this is only the Document API.
	ral_cls = getattr(ral_mod, "RepostAccountingLedger", None)
	if ral_cls is not None and start_fn is getattr(ral_cls, "start_repost", None):
		return False

	_orig_start = start_fn

	def start_repost(account_repost_doc: str | None = None):
		_orig_start(account_repost_doc)
		if account_repost_doc:
			_run_irr_pipeline_after_ral(account_repost_doc)

	ral_mod.start_repost = start_repost
	ral_mod._iran_patched_start_repost = True
	return True


def _patch_repost_compatibility():
	from erpnext_extensions.iran_accounting.domain.currency import is_irr_company

	try:
		import erpnext.stock.doctype.repost_item_valuation.repost_item_valuation as riv_mod
	except ImportError:
		riv_mod = None

	if riv_mod and not getattr(riv_mod, "_iran_patched_repost", None):
		_orig_repost = riv_mod.repost

		def repost(doc):
			voucher_type = getattr(doc, "voucher_type", None)
			voucher_no = getattr(doc, "voucher_no", None)
			company = getattr(doc, "company", None)
			try:
				return _orig_repost(doc)
			finally:
				frappe.flags.through_repost_item_valuation = False
				if not (voucher_type and voucher_no and company and is_irr_company(company)):
					return
				status = frappe.db.get_value("Repost Item Valuation", doc.name, "status")
				if status != "Completed":
					return
				try:
					doc = frappe.get_doc(voucher_type, voucher_no)
					from erpnext_extensions.iran_accounting.domain.repost_determinism import (
						run_post_repost_deterministic_pipeline,
					)

					run_post_repost_deterministic_pipeline(doc, raise_on_fail=False)
				except Exception:
					frappe.log_error(
						title="IRR repost reconcile failed",
						message=frappe.get_traceback(),
					)

		riv_mod.repost = repost
		riv_mod._iran_patched_repost = True

	try:
		import erpnext.accounts.doctype.repost_accounting_ledger.repost_accounting_ledger as ral_mod
	except ImportError:
		ral_mod = None

	if not ral_mod:
		return

	# Capability detection (no version pins):
	# - ERPNext 16.31.1+: module ``repost`` does the work; Document.start_repost only enqueues.
	# - Older: module ``start_repost`` performed the ledger work synchronously.
	patched = _patch_ral_module_level_repost(ral_mod)
	if not patched:
		patched = _patch_ral_module_level_start_repost(ral_mod)

	if not patched and not getattr(ral_mod, "_iran_ral_hook_unavailable_logged", False):
		frappe.logger("erpnext_extensions.iran_accounting").warning(
			"RAL post-repost IRR hook not installed: neither module-level "
			"`repost` nor legacy `start_repost` is available on "
			"erpnext.accounts.doctype.repost_accounting_ledger.repost_accounting_ledger"
		)
		ral_mod._iran_ral_hook_unavailable_logged = True


def _patch_stock_controller():
	from erpnext.controllers import stock_controller as sc
	from erpnext.controllers.stock_controller import StockController

	if not getattr(StockController, "_iran_original_get_gl_entries", None):
		StockController._iran_original_get_gl_entries = StockController.get_gl_entries
		StockController._iran_original_get_debit_field_precision = StockController.get_debit_field_precision
		StockController._iran_original_make_gl_entries = StockController.make_gl_entries
		StockController._iran_original_get_stock_ledger_details = StockController.get_stock_ledger_details

	for name, func in zvt.STOCK_CONTROLLER_METHODS.items():
		if name == "get_gl_entries":
			# Stock Entry only — see StockEntry.get_gl_entries below. DN/PR/SI must use core SLE-based GL.
			continue
		setattr(StockController, name, func)

	def get_debit_field_precision(self):
		if getattr(self, "company", None):
			return zvt.get_debit_field_precision_for_company(self)
		return self._iran_original_get_debit_field_precision()

	StockController.get_debit_field_precision = get_debit_field_precision

	def make_gl_entries(self, gl_entries=None, from_repost=False, via_landed_cost_voucher=False):
		from erpnext.accounts.general_ledger import make_gl_entries as _make_gl_entries
		from erpnext.accounts.general_ledger import make_reverse_gl_entries

		if self.docstatus == 2:
			make_reverse_gl_entries(voucher_type=self.doctype, voucher_no=self.name)

		provisional_accounting_for_non_stock_items = cint(
			frappe.get_cached_value(
				"Company", self.company, "enable_provisional_accounting_for_non_stock_items"
			)
		)

		is_asset_pr = any(d.get("is_fixed_asset") for d in self.get("items"))
		need_inventory_map = (self.get_stock_items() or self.get("packed_items")) and (
			cint(erpnext.is_perpetual_inventory_enabled(self.company))
		)

		inventory_account_map = frappe._dict()
		if need_inventory_map:
			inventory_account_map = self.get_inventory_account_map()

		if need_inventory_map or provisional_accounting_for_non_stock_items or is_asset_pr:
			if self.docstatus == 1:
				if not gl_entries:
					gl_entries = (
						self.get_gl_entries(inventory_account_map, via_landed_cost_voucher)
						if self.doctype == "Purchase Receipt"
						else self.get_gl_entries(inventory_account_map)
					)
				# Iran post-processing: IRR rate×qty residual → Company Round Off Account.
				# Ordinary get_gl_entries remains ERPNext (or ZVT); this only appends/adjusts.
				from erpnext_extensions.iran_accounting.domain.currency import is_irr_company
				from erpnext_extensions.iran_accounting.domain.irr_rounding_residual import (
					apply_irr_rate_rounding_residual_gl,
				)

				if is_irr_company(self.company) and gl_entries is not None:
					apply_irr_rate_rounding_residual_gl(self, gl_entries)

				skip_round_off = None
				if self.doctype == "Stock Entry":
					precision = self.get_debit_field_precision()
					if zvt._should_force_balanced_transfer_gl(self, precision):
						skip_round_off = self.name
				try:
					if skip_round_off:
						frappe.flags.skip_round_off_for_zero_value_stock_entry = skip_round_off
					_make_gl_entries(gl_entries, from_repost=from_repost)
				finally:
					frappe.flags.skip_round_off_for_zero_value_stock_entry = None

	StockController.make_gl_entries = make_gl_entries

	def get_stock_ledger_details(self):
		from erpnext.stock.doctype.inventory_dimension.inventory_dimension import get_inventory_dimensions

		stock_ledger = {}

		table = frappe.qb.DocType("Stock Ledger Entry")

		select_fields = [
			table.name,
			table.warehouse,
			table.stock_value_difference,
			table.valuation_rate,
			table.voucher_detail_no,
			table.item_code,
			table.posting_date,
			table.posting_time,
			table.actual_qty,
			table.qty_after_transaction,
			table.project,
		]

		sle_meta = frappe.get_meta("Stock Ledger Entry")
		for dimension in get_inventory_dimensions():
			if sle_meta.has_field(dimension.fieldname):
				select_fields.append(getattr(table, dimension.fieldname))

		stock_ledger_entries = (
			frappe.qb.from_(table)
			.select(*select_fields)
			.where(
				(table.voucher_type == self.doctype)
				& (table.voucher_no == self.name)
				& (table.is_cancelled == 0)
			)
		).run(as_dict=True)

		for sle in stock_ledger_entries:
			stock_ledger.setdefault(sle.voucher_detail_no, []).append(sle)

		return stock_ledger

	StockController.get_stock_ledger_details = get_stock_ledger_details

	if not getattr(sc, "_iran_original_get_accounting_ledger_preview", None):
		sc._iran_original_get_accounting_ledger_preview = sc.get_accounting_ledger_preview

	def get_accounting_ledger_preview(doc, filters):
		from erpnext.accounts.report.general_ledger.general_ledger import get_columns as get_gl_columns
		from erpnext.controllers.stock_controller import get_columns, get_data, get_gl_entries_for_preview

		gl_columns, gl_data = [], []
		fields = [
			"posting_date",
			"account",
			"debit",
			"credit",
			"against",
			"party_type",
			"party",
			"cost_center",
			"against_voucher_type",
			"against_voucher",
		]

		doc.docstatus = 1

		if doc.doctype == "Stock Entry":
			doc.make_bundle_using_old_serial_batch_fields()
			doc.update_stock_ledger()
		elif doc.get("update_stock") or doc.doctype in ("Purchase Receipt", "Delivery Note"):
			doc.update_stock_ledger()

		doc.make_gl_entries()
		columns = get_gl_columns(filters)
		gl_entries = get_gl_entries_for_preview(doc.doctype, doc.name, fields)

		gl_columns = get_columns(columns, fields)
		gl_data = get_data(fields, gl_entries)

		return gl_columns, gl_data

	sc.get_accounting_ledger_preview = get_accounting_ledger_preview


def _patch_stock_reconciliation():
	from erpnext.stock.doctype.stock_reconciliation.stock_reconciliation import StockReconciliation

	from erpnext_extensions.iran_accounting.domain.stock_reconciliation_erpnext import (
		patched_calculate_difference_amount,
		patched_remove_items_with_no_change,
		patched_set_total_qty_and_amount,
	)

	if getattr(StockReconciliation, "_iran_patched_set_total", None):
		return

	StockReconciliation._iran_original_set_total_qty_and_amount = StockReconciliation.set_total_qty_and_amount
	StockReconciliation._iran_original_calculate_difference_amount = (
		StockReconciliation.calculate_difference_amount
	)
	StockReconciliation._iran_original_remove_items_with_no_change = (
		StockReconciliation.remove_items_with_no_change
	)
	StockReconciliation.set_total_qty_and_amount = patched_set_total_qty_and_amount
	StockReconciliation.calculate_difference_amount = patched_calculate_difference_amount
	StockReconciliation.remove_items_with_no_change = patched_remove_items_with_no_change
	StockReconciliation._iran_patched_set_total = True


def _patch_stock_entry():
	from erpnext.stock.doctype.stock_entry.stock_entry import StockEntry

	import erpnext_extensions.iran_accounting.stock_entry as se_hooks

	if getattr(StockEntry, "_iran_patched", None):
		# Idempotent: never re-save wrapper as original.
		return

	se_hooks._original_set_total_incoming_outgoing_value = StockEntry.set_total_incoming_outgoing_value
	StockEntry.set_total_incoming_outgoing_value = se_hooks.patched_set_total_incoming_outgoing_value

	if not getattr(StockEntry, "before_gl_preview", None) or not getattr(
		StockEntry.before_gl_preview, "_iran_wrapped", None
	):
		_orig_before = getattr(StockEntry, "before_gl_preview", None)

		def before_gl_preview(self):
			se_hooks.before_gl_preview_stock_entry(self)
			if _orig_before:
				return _orig_before(self)

		before_gl_preview._iran_wrapped = True
		StockEntry.before_gl_preview = before_gl_preview

	current_get_gl = StockEntry.get_gl_entries
	if getattr(current_get_gl, "_iran_stock_entry_gl_wrapper", None):
		# Already wrapped somehow without _iran_patched — refuse circular save.
		frappe.throw(
			"iran_accounting: StockEntry.get_gl_entries already wrapped; refusing duplicate patch",
		)
	StockEntry._iran_original_stock_entry_get_gl_entries = current_get_gl

	def get_gl_entries(
		self, inventory_account_map=None, default_expense_account=None, default_cost_center=None
	):
		return zvt.iran_stock_entry_get_gl_entries(
			self, inventory_account_map, default_expense_account, default_cost_center
		)

	get_gl_entries._iran_stock_entry_gl_wrapper = True
	StockEntry.get_gl_entries = get_gl_entries
	StockEntry._iran_patched = True


def _patch_general_ledger():
	import erpnext.accounts.general_ledger as gl

	if getattr(gl, "_iran_patched", None):
		return

	gl._iran_original_merge_similar_entries = gl.merge_similar_entries
	gl._iran_original_save_entries = gl.save_entries
	gl._iran_original_process_debit_credit_difference = gl.process_debit_credit_difference
	gl._iran_original_make_entry = gl.make_entry
	gl._iran_original_get_debit_credit_difference = gl.get_debit_credit_difference

	gl.absorb_gl_map_rounding_residual = zvt.absorb_gl_map_rounding_residual

	gl._iran_original_distribute_cc = gl.distribute_gl_based_on_cost_center_allocation

	def distribute_gl_based_on_cost_center_allocation(gl_map, precision=None, from_repost=False):
		from erpnext_extensions.iran_accounting.domain.gl_cost_center_allocation import (
			distribute_gl_based_on_cost_center_allocation_irr,
		)

		return distribute_gl_based_on_cost_center_allocation_irr(
			gl_map, precision=precision, from_repost=from_repost
		)

	gl.distribute_gl_based_on_cost_center_allocation = distribute_gl_based_on_cost_center_allocation

	def _is_non_zero_gl_entry(x, precision):
		if (
			x.voucher_type == "Journal Entry"
			and frappe.get_cached_value("Journal Entry", x.voucher_no, "voucher_type")
			== "Exchange Gain Or Loss"
		):
			return True

		if flt(x.debit, precision) != 0 or flt(x.credit, precision) != 0:
			return True

		if (
			x.voucher_type == "Stock Entry"
			and frappe.flags.get("skip_round_off_for_zero_value_stock_entry") == x.voucher_no
		):
			acct_precision = precision
			if x.get("account_currency"):
				acct_precision = get_field_precision(
					frappe.get_meta("GL Entry").get_field("debit_in_account_currency"),
					currency=x.account_currency,
				)
			if flt(x.debit_in_account_currency, acct_precision) != 0:
				return True
			if flt(x.credit_in_account_currency, acct_precision) != 0:
				return True

		return False

	def merge_similar_entries(gl_map, precision=None):
		merged = gl._iran_original_merge_similar_entries(gl_map, precision)
		if not merged:
			return merged
		company = merged[0].company if merged else erpnext.get_default_company()
		company_currency = erpnext.get_company_currency(company)
		if not precision:
			precision = get_field_precision(
				frappe.get_meta("GL Entry").get_field("debit"), currency=company_currency
			)
		return list(filter(lambda x: _is_non_zero_gl_entry(x, precision), merged))

	gl.merge_similar_entries = merge_similar_entries

	def _finalize_zero_value_stock_entry_gl_map_before_save(gl_map):
		if not gl_map or gl_map[0].voucher_type != "Stock Entry":
			return gl_map

		voucher_no = gl_map[0].voucher_no
		if frappe.flags.get("skip_round_off_for_zero_value_stock_entry") != voucher_no:
			return gl_map

		from erpnext.stock.doctype.stock_entry.stock_entry import StockEntry

		doc = StockEntry({"doctype": "Stock Entry", "name": voucher_no, "company": gl_map[0].company})
		doc.purpose = frappe.get_cached_value("Stock Entry", voucher_no, "purpose")
		doc.total_incoming_value = frappe.get_cached_value("Stock Entry", voucher_no, "total_incoming_value")
		doc.total_outgoing_value = frappe.get_cached_value("Stock Entry", voucher_no, "total_outgoing_value")
		doc.value_difference = frappe.get_cached_value("Stock Entry", voucher_no, "value_difference")
		return zvt.finalize_zero_value_transfer_gl_map(doc, gl_map)

	def save_entries(gl_map, adv_adj, update_outstanding, from_repost=False):
		if not from_repost:
			gl.validate_cwip_accounts(gl_map)

		gl_map = _finalize_zero_value_stock_entry_gl_map_before_save(gl_map)
		gl.process_debit_credit_difference(gl_map)
		gl_map = _finalize_zero_value_stock_entry_gl_map_before_save(gl_map)

		dimension_filter_map = gl.get_dimension_filter_map()
		if gl_map:
			gl.check_freezing_date(gl_map[0]["posting_date"], gl_map[0]["company"], adv_adj)
			is_opening = any(d.get("is_opening") == "Yes" for d in gl_map)
			if gl_map[0]["voucher_type"] != "Period Closing Voucher":
				gl.validate_against_pcv(is_opening, gl_map[0]["posting_date"], gl_map[0]["company"])

		for entry in gl_map:
			gl.validate_allowed_dimensions(entry, dimension_filter_map)
			gl.make_entry(entry, adv_adj, update_outstanding, from_repost)

	gl.save_entries = save_entries

	def process_debit_credit_difference(gl_map):
		company = gl_map[0].company
		company_currency = erpnext.get_company_currency(company)
		from erpnext_extensions.iran_accounting.domain.currency import get_currency_precision, is_irr_company

		if is_irr_company(company):
			precision = get_currency_precision(company_currency)
		else:
			precision = get_field_precision(
				frappe.get_meta("GL Entry").get_field("debit"), currency=company_currency
			)

		voucher_type = gl_map[0].voucher_type
		voucher_no = gl_map[0].voucher_no
		allowance = gl.get_debit_credit_allowance(voucher_type, precision)

		debit_credit_diff, trx_cur_debit_credit_diff = gl.get_debit_credit_difference(gl_map, precision)

		if abs(debit_credit_diff) > allowance:
			if not (
				voucher_type == "Journal Entry"
				and frappe.get_cached_value("Journal Entry", voucher_no, "voucher_type")
				== "Exchange Gain Or Loss"
			):
				gl.raise_debit_credit_not_equal_error(debit_credit_diff, voucher_type, voucher_no)

		elif debit_credit_diff and precision == 0 and abs(debit_credit_diff) < 1:
			zvt.absorb_gl_map_rounding_residual(
				gl_map, precision, debit_credit_diff, trx_cur_debit_credit_diff
			)
		elif abs(debit_credit_diff) >= (1.0 / (10**precision)):
			if (
				voucher_type == "Stock Entry"
				and frappe.flags.get("skip_round_off_for_zero_value_stock_entry") == voucher_no
			):
				zvt.absorb_gl_map_rounding_residual(
					gl_map, precision, debit_credit_diff, trx_cur_debit_credit_diff
				)
			else:
				gl.make_round_off_gle(gl_map, debit_credit_diff, trx_cur_debit_credit_diff, precision)
				gl_map[:] = [
					e
					for e in gl_map
					if flt(e.get("debit"), precision) != 0 or flt(e.get("credit"), precision) != 0
				]

		debit_credit_diff, trx_cur_debit_credit_diff = gl.get_debit_credit_difference(gl_map, precision)
		if abs(debit_credit_diff) > allowance:
			if not (
				voucher_type == "Journal Entry"
				and frappe.get_cached_value("Journal Entry", voucher_no, "voucher_type")
				== "Exchange Gain Or Loss"
			):
				gl.raise_debit_credit_not_equal_error(debit_credit_diff, voucher_type, voucher_no)

	def make_entry(args, adv_adj, update_outstanding, from_repost=False):
		round_gl_entry_amounts(args)
		company = args.get("company") if isinstance(args, dict) else getattr(args, "company", None)
		if company:
			company_currency = erpnext.get_company_currency(company)
			from erpnext_extensions.iran_accounting.domain.currency import (
				get_currency_precision,
				is_irr_company,
			)

			precision = (
				get_currency_precision(company_currency)
				if is_irr_company(company)
				else get_field_precision(
					frappe.get_meta("GL Entry").get_field("debit"), currency=company_currency
				)
			)
			debit = flt(args.get("debit") if isinstance(args, dict) else args.debit, precision)
			credit = flt(args.get("credit") if isinstance(args, dict) else args.credit, precision)
			if not debit and not credit:
				return None
		return gl._iran_original_make_entry(args, adv_adj, update_outstanding, from_repost)

	def get_debit_credit_difference(gl_map, precision):
		for entry in gl_map:
			round_gl_entry_amounts(entry)
		return gl._iran_original_get_debit_credit_difference(gl_map, precision)

	gl.process_debit_credit_difference = process_debit_credit_difference
	gl.make_entry = make_entry
	gl.get_debit_credit_difference = get_debit_credit_difference
	gl._iran_patched = True


def _patch_accounts_controller():
	from erpnext.controllers import accounts_controller as ac

	if getattr(ac, "_iran_patched_set_balance", None):
		return

	_orig = ac.set_balance_in_account_currency

	def set_balance_in_account_currency(
		gl_dict, account_currency=None, conversion_rate=None, company_currency=None
	):
		_orig(gl_dict, account_currency, conversion_rate, company_currency)
		if not company_currency and gl_dict.get("company"):
			company_currency = erpnext.get_company_currency(gl_dict.company)
		if not account_currency:
			account_currency = gl_dict.get("account_currency") or company_currency
		acct_precision = get_currency_precision(account_currency)

		if flt(gl_dict.debit) and not flt(gl_dict.debit_in_account_currency):
			gl_dict.debit_in_account_currency = (
				gl_dict.debit
				if account_currency == company_currency
				else flt(gl_dict.debit / conversion_rate, acct_precision)
			)

		if flt(gl_dict.credit) and not flt(gl_dict.credit_in_account_currency):
			gl_dict.credit_in_account_currency = (
				gl_dict.credit
				if account_currency == company_currency
				else flt(gl_dict.credit / conversion_rate, acct_precision)
			)

	ac.set_balance_in_account_currency = set_balance_in_account_currency
	ac._iran_patched_set_balance = True


def _patch_stock_ledger_engine():
	import erpnext.stock.stock_ledger as sl

	from erpnext_extensions.iran_accounting.domain.currency import is_irr_company
	from erpnext_extensions.iran_accounting.domain.ledger_rounding import round_sle_monetary_fields
	from erpnext_extensions.iran_accounting.domain.riv_rate_guard import (
		assert_erpnext_riv_rate_patch_supported,
		make_update_rate_on_stock_entry_wrapper,
	)
	from erpnext_extensions.iran_accounting.domain.sle_persistence import (
		persist_processed_sle_if_possible,
	)
	from erpnext_extensions.iran_accounting.domain.stock_entry_sync import (
		sync_irr_sle_from_stock_entry_row,
	)
	from erpnext_extensions.iran_accounting.domain.stock_reconciliation_sync import (
		sync_irr_sle_from_stock_reconciliation_row,
	)

	# Fail-closed upgrade guard before (re)installing the rate wrapper.
	assert_erpnext_riv_rate_patch_supported()

	if not getattr(sl, "_iran_patched_update_entries_after", None):
		_orig_set_precision = sl.update_entries_after.set_precision
		_orig_process_sle = sl.update_entries_after.process_sle

		def set_precision(self):
			_orig_set_precision(self)
			company_currency = erpnext.get_company_currency(self.company)
			self.currency_precision = get_currency_precision(company_currency)

		sl.update_entries_after.set_precision = set_precision

		def process_sle(self, sle):
			_orig_process_sle(self, sle)
			company = getattr(self, "company", None) or (
				sle.get("company") if hasattr(sle, "get") else None
			)
			if company and is_irr_company(company):
				sync_irr_sle_from_stock_reconciliation_row(sle)
				sync_irr_sle_from_stock_entry_row(sle)
				round_sle_monetary_fields(sle, company)
				sync_irr_sle_from_stock_entry_row(sle)
				persist_processed_sle_if_possible(sle)

		sl.update_entries_after.process_sle = process_sle
		sl._iran_patched_update_entries_after = True

	# Idempotent install of rate-first RIV wrapper (may run after older process_sle-only patch).
	if not getattr(sl, "_iran_patched_update_rate_on_stock_entry", None):
		live = sl.update_entries_after.update_rate_on_stock_entry
		if getattr(live, "_iran_riv_rate_wrapper", None):
			_orig_update_rate_on_stock_entry = live._iran_original
		else:
			_orig_update_rate_on_stock_entry = live
		sl.update_entries_after._iran_original_update_rate_on_stock_entry = (
			_orig_update_rate_on_stock_entry
		)
		sl.update_entries_after.update_rate_on_stock_entry = make_update_rate_on_stock_entry_wrapper(
			_orig_update_rate_on_stock_entry
		)
		sl._iran_patched_update_rate_on_stock_entry = True


def _patch_stock_ledger_report():
	import erpnext.stock.report.stock_ledger.stock_ledger as sl_report

	from erpnext_extensions.iran_accounting.reports import sanitize_stock_ledger_report

	if getattr(sl_report, "_iran_patched_execute", None):
		return
	sl_report._iran_original_execute = sl_report.execute

	def execute(filters):
		columns, data = sl_report._iran_original_execute(filters)
		filters = frappe._dict(filters)
		return sanitize_stock_ledger_report(columns, data, filters.get("company"), filters)

	sl_report.execute = execute
	sl_report._iran_patched_execute = True


def _patch_accounting_ledger_preview():
	# Applied inside _patch_stock_controller on stock_controller module
	pass

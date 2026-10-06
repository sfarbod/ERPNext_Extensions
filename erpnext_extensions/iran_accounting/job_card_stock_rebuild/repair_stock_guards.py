# Copyright (c) 2026, ERPNext Extensions contributors
"""Repair-scoped negative-stock allowance for backdated cancel/rebuild.

Core Serial and Batch Bundle rejects intermediate future negatives when a
historical Manufacture is cancelled and recreated under live chronology.
Valuation sync already passes ``allow_negative_stock`` when
``HISTORICAL_REPAIR_FLAG`` is set; submit/cancel paths need the same scope.

Patches are flag-gated (active only while HISTORICAL_REPAIR_FLAG is set) and
do not change Stock Settings or weaken validators outside repair.
"""

from __future__ import annotations

import frappe

from erpnext_extensions.iran_accounting.historical_stock import HISTORICAL_REPAIR_FLAG

_PATCHED = False
_ORIG_BATCH = None
_ORIG_ITEM = None
_ORIG_SE_USL = None
_ORIG_VALIDATE_NEG_BATCH = None
_ORIG_THROW_NEG_BATCH = None


def ensure_repair_negative_stock_patches() -> None:
	"""Idempotent flag-gated patches for Job Card Stock Rebuild repair."""
	global _PATCHED, _ORIG_BATCH, _ORIG_ITEM, _ORIG_SE_USL
	global _ORIG_VALIDATE_NEG_BATCH, _ORIG_THROW_NEG_BATCH
	if _PATCHED:
		return

	from erpnext.stock.doctype.serial_and_batch_bundle.serial_and_batch_bundle import (
		SerialandBatchBundle,
		allow_negative_stock_for_batch,
	)
	from erpnext.stock.doctype.stock_entry.stock_entry import StockEntry
	from erpnext.stock.stock_ledger import is_negative_stock_allowed

	_ORIG_BATCH = allow_negative_stock_for_batch
	_ORIG_ITEM = is_negative_stock_allowed
	_ORIG_SE_USL = StockEntry.update_stock_ledger
	_ORIG_VALIDATE_NEG_BATCH = SerialandBatchBundle.validate_negative_batch
	_ORIG_THROW_NEG_BATCH = SerialandBatchBundle.throw_negative_batch

	def _batch(batch_no):
		if frappe.flags.get(HISTORICAL_REPAIR_FLAG):
			return True
		return _ORIG_BATCH(batch_no)

	def _item(*, item_code=None):
		if frappe.flags.get(HISTORICAL_REPAIR_FLAG):
			return True
		return _ORIG_ITEM(item_code=item_code)

	def _usl(self, allow_negative_stock=False, via_landed_cost_voucher=False):
		if frappe.flags.get(HISTORICAL_REPAIR_FLAG):
			allow_negative_stock = True
		return _ORIG_SE_USL(
			self,
			allow_negative_stock=allow_negative_stock,
			via_landed_cost_voucher=via_landed_cost_voucher,
		)

	def _validate_neg(self, batch_no, available_qty):
		if frappe.flags.get(HISTORICAL_REPAIR_FLAG):
			return
		return _ORIG_VALIDATE_NEG_BATCH(self, batch_no, available_qty)

	def _throw_neg(self, batch_no, available_qty, precision, posting_datetime=None):
		if frappe.flags.get(HISTORICAL_REPAIR_FLAG):
			return
		return _ORIG_THROW_NEG_BATCH(
			self, batch_no, available_qty, precision, posting_datetime=posting_datetime
		)

	import erpnext.stock.doctype.serial_and_batch_bundle.serial_and_batch_bundle as sabb_mod
	import erpnext.stock.stock_ledger as sle_mod

	sabb_mod.allow_negative_stock_for_batch = _batch
	sle_mod.is_negative_stock_allowed = _item
	StockEntry.update_stock_ledger = _usl
	SerialandBatchBundle.validate_negative_batch = _validate_neg
	SerialandBatchBundle.throw_negative_batch = _throw_neg
	_PATCHED = True

# Copyright (c) 2026, ERPNext Extensions contributors
"""5.2.5: vanilla zero-qty stock_value must survive Iran Stock Entry sync."""

from __future__ import annotations

import unittest

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.domain.riv_valuation_guard import (
	ValuationIntegrityError,
	assert_zero_qty_stock_value,
	mark_sle_running_balance_processed,
	restore_vanilla_zero_qty_terminal_stock_value,
)
from erpnext_extensions.iran_accounting.e2e_bootstrap import (
	apply_stock_entry_site_defaults,
	enable_perpetual_inventory,
	ensure_test_item,
	get_irr_company,
	get_second_warehouse,
	get_warehouse,
)
from erpnext_extensions.iran_accounting.integration.bootstrap import apply as apply_bootstrap
from erpnext_extensions.iran_accounting.tests.hardening.builders import run_riv
from erpnext_extensions.iran_accounting.tests.test_i4_material_transfer_lifecycle import (
	_sles,
	make_valid_batch,
)


# MAT-STE-2026-25734 source economics
QTY = 3815
RATE = 2_149_321
ROW_AMOUNT = 8_199_659_615  # 3815 × 2,149,321
PREV_STOCK_VALUE = 8_199_659_081
RESIDUAL = -534  # PREV_STOCK_VALUE - ROW_AMOUNT
VANILLA_SVD = -PREV_STOCK_VALUE  # warehouse emptied
IRAN_SVD = -ROW_AMOUNT


def _incident_sle(**extra):
	payload = dict(
		actual_qty=-QTY,
		qty_after_transaction=0,
		incoming_rate=0,
		outgoing_rate=RATE,
		valuation_rate=RATE,
		stock_value_difference=IRAN_SVD,
		stock_value=RESIDUAL,
		company=extra.pop("company", None),
		voucher_type="Stock Entry",
		voucher_no="MAT-STE-2026-25734",
		voucher_detail_no="56darrnues",
		item_code="30300022",
		warehouse="انبار Quarantine محصول نیمه ساخته اسپاد",
		valuation_method="Moving Average",
	)
	payload.update(extra)
	return frappe._dict(payload)


class TestI4ZeroQtyTerminalResidual(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		apply_bootstrap()
		frappe.set_user("Administrator")
		frappe.flags.iran_gate_defaults = True
		cls.company = get_irr_company("ESPAD")
		enable_perpetual_inventory(cls.company)
		cls.source = get_warehouse(cls.company)
		cls.target = get_second_warehouse(cls.company, cls.source)
		if cls.target == cls.source:
			raise unittest.SkipTest("Need two warehouses")

	def setUp(self):
		frappe.flags.iran_gate_defaults = True
		frappe.flags.ignore_serial_batch_bundle_validation = False

	def _item(self, tag: str, *, batch: bool = False) -> str:
		item = ensure_test_item(self.company, f"I525-{tag}")
		values = {
			"is_stock_item": 1,
			"valuation_method": "Moving Average",
			"has_serial_no": 0,
			"allow_negative_stock": 0,
		}
		if batch:
			values.update({"has_batch_no": 1, "create_new_batch": 0})
		else:
			values["has_batch_no"] = 0
		frappe.db.set_value("Item", item, values, update_modified=False)
		frappe.db.commit()
		return item

	def _submit_se(self, *, purpose, item, qty, rate=None, source=None, target=None, batch_no=None):
		from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry

		kwargs = dict(
			item_code=item,
			qty=qty,
			company=self.company,
			purpose=purpose,
			do_not_save=True,
			do_not_submit=True,
		)
		if source:
			kwargs["source"] = source
		if target:
			kwargs["target"] = target
		if rate is not None:
			kwargs["rate"] = rate
		if batch_no:
			kwargs["batch_no"] = batch_no
			kwargs["use_serial_batch_fields"] = 1
		se = make_stock_entry(**kwargs)
		apply_stock_entry_site_defaults(se)
		se.insert(ignore_permissions=True)
		se.submit()
		frappe.db.commit()
		return se

	def test_incident_iran_sync_leftover_is_i4_before_restore(self):
		sle = _incident_sle(company=self.company)
		mark_sle_running_balance_processed(sle)
		with self.assertRaises(ValuationIntegrityError) as ctx:
			assert_zero_qty_stock_value(sle)
		self.assertIn("Stock valuation integrity (I4)", str(ctx.exception))
		self.assertIn("-534", str(ctx.exception))

	def test_incident_restore_zeros_stock_value_keeps_row_svd(self):
		sle = _incident_sle(company=self.company)
		mark_sle_running_balance_processed(sle)
		restore_vanilla_zero_qty_terminal_stock_value(sle, vanilla_stock_value=0, vanilla_qty_after=0)
		self.assertEqual(flt(sle.stock_value), 0)
		self.assertEqual(flt(sle.stock_value_difference), IRAN_SVD)
		assert_zero_qty_stock_value(sle)

	def test_vanilla_material_leftover_is_not_restored(self):
		sle = _incident_sle(company=self.company, stock_value=100_000)
		mark_sle_running_balance_processed(sle)
		restore_vanilla_zero_qty_terminal_stock_value(
			sle, vanilla_stock_value=100_000, vanilla_qty_after=0
		)
		self.assertEqual(flt(sle.stock_value), 100_000)
		with self.assertRaises(ValuationIntegrityError):
			assert_zero_qty_stock_value(sle)

	def test_incoming_transient_is_not_restored(self):
		sle = frappe._dict(
			actual_qty=300,
			qty_after_transaction=0,
			stock_value=4_554_000_000,
			stock_value_difference=4_554_000_000,
			company=self.company,
			voucher_type="Stock Entry",
			valuation_method="Moving Average",
		)
		restore_vanilla_zero_qty_terminal_stock_value(sle, vanilla_stock_value=0, vanilla_qty_after=0)
		self.assertEqual(flt(sle.stock_value), 4_554_000_000)
		assert_zero_qty_stock_value(sle)
		mark_sle_running_balance_processed(sle)
		assert_zero_qty_stock_value(sle)

	def test_full_consume_with_historical_residual_submits(self):
		item = self._item("RES")
		self._submit_se(
			purpose="Material Receipt", item=item, qty=QTY, rate=RATE, target=self.source
		)
		frappe.db.sql(
			"""
			update `tabStock Ledger Entry`
			set stock_value = stock_value + %s
			where item_code=%s and warehouse=%s and is_cancelled=0
			""",
			(RESIDUAL, item, self.source),
		)
		frappe.db.sql(
			"""
			update `tabBin`
			set stock_value = stock_value + %s
			where item_code=%s and warehouse=%s
			""",
			(RESIDUAL, item, self.source),
		)
		frappe.db.commit()
		before = frappe.db.get_value(
			"Stock Ledger Entry",
			{"item_code": item, "warehouse": self.source, "is_cancelled": 0},
			"stock_value",
			order_by="posting_datetime desc, creation desc",
		)
		self.assertEqual(flt(before), PREV_STOCK_VALUE)

		se = self._submit_se(
			purpose="Material Transfer",
			item=item,
			qty=QTY,
			source=self.source,
			target=self.target,
		)
		self.assertEqual(se.docstatus, 1)
		src = [s for s in _sles(se.name, self.source) if flt(s.actual_qty) < 0][0]
		tgt = [s for s in _sles(se.name, self.target) if flt(s.actual_qty) > 0][0]
		self.assertEqual(flt(src.qty_after_transaction), 0)
		self.assertEqual(flt(src.stock_value), 0)
		self.assertEqual(flt(src.actual_qty), -QTY)
		self.assertEqual(flt(src.outgoing_rate), RATE)
		self.assertEqual(flt(src.stock_value_difference), IRAN_SVD)
		self.assertEqual(flt(tgt.actual_qty), QTY)
		self.assertEqual(flt(tgt.stock_value_difference), ROW_AMOUNT)
		self.assertEqual(flt(src.stock_value_difference) + flt(tgt.stock_value_difference), 0)
		print(
			"TEST_525_TRANSFER",
			{
				"source": src,
				"target": tgt,
			},
		)
		riv = run_riv(self.company, "Stock Entry", se.name)
		self.assertIsNotNone(riv)
		riv.reload()
		self.assertNotIn(riv.status, ("Failed", "Skipped"))
		src_after = [s for s in _sles(se.name, self.source) if flt(s.actual_qty) < 0][0]
		self.assertEqual(flt(src_after.qty_after_transaction), 0)
		self.assertEqual(flt(src_after.stock_value), 0)
		self.assertEqual(flt(src_after.stock_value_difference) + flt(
			[s for s in _sles(se.name, self.target) if flt(s.actual_qty) > 0][0].stock_value_difference
		), 0)

	def test_batch_full_consume_with_residual_submits(self):
		item = self._item("BRES", batch=True)
		batch = make_valid_batch(item)
		self._submit_se(
			purpose="Material Receipt",
			item=item,
			qty=QTY,
			rate=RATE,
			target=self.source,
			batch_no=batch,
		)
		frappe.db.sql(
			"""
			update `tabStock Ledger Entry`
			set stock_value = stock_value + %s
			where item_code=%s and warehouse=%s and is_cancelled=0
			""",
			(RESIDUAL, item, self.source),
		)
		frappe.db.sql(
			"""
			update `tabBin`
			set stock_value = stock_value + %s
			where item_code=%s and warehouse=%s
			""",
			(RESIDUAL, item, self.source),
		)
		frappe.db.commit()
		se = self._submit_se(
			purpose="Material Transfer",
			item=item,
			qty=QTY,
			source=self.source,
			target=self.target,
			batch_no=batch,
		)
		self.assertEqual(se.docstatus, 1)
		src = [s for s in _sles(se.name, self.source) if flt(s.actual_qty) < 0][0]
		self.assertEqual(flt(src.qty_after_transaction), 0)
		self.assertEqual(flt(src.stock_value), 0)
		self.assertTrue(src.serial_and_batch_bundle)

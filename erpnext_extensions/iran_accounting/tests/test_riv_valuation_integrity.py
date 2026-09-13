# Copyright (c) 2026, ERPNext Extensions contributors
"""5.2.0 Manufacture RIV valuation integrity + SLE sign contract."""

from __future__ import annotations

import unittest
from decimal import Decimal
from unittest import mock

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.domain.riv_valuation_guard import (
	ValuationIntegrityError,
	assert_incoming_rate_not_negative,
	assert_manufacture_value_pool,
	assert_sle_valuation_integrity_before_vanilla,
	assert_stock_entry_valuation_integrity,
	assert_svd_direction,
	assert_zero_qty_stock_value,
	is_final_zero_qty_consume_state,
	make_recalculate_amounts_wrapper,
	mark_sle_running_balance_processed,
	mark_sle_running_balance_processed_after_vanilla,
	previous_running_qty,
	sle_running_balance_is_processed,
	should_assert_zero_qty_stock_value,
	vanilla_process_sle_assigned_running_balance,
)
from erpnext_extensions.iran_accounting.domain.stock_entry_sync import (
	signed_movement_from_row_amount,
)


class _Row:
	def __init__(self, **fields):
		self.__dict__.update(fields)

	def get(self, key, default=None):
		return self.__dict__.get(key, default)


class _Doc:
	def __init__(self, **fields):
		self.__dict__.setdefault("doctype", "Stock Entry")
		self.__dict__.setdefault("purpose", "Manufacture")
		self.__dict__.setdefault("company", "اسپاد فارمد دارو")
		self.__dict__.update(fields)

	def get(self, key, default=None):
		return self.__dict__.get(key, default)


def _incoming(**fields):
	base = dict(
		qty=10,
		transfer_qty=10,
		amount=1000,
		basic_amount=1000,
		basic_rate=100,
		valuation_rate=100,
		additional_cost=0,
		landed_cost_voucher_amount=0,
		s_warehouse=None,
		t_warehouse="FG",
		is_finished_item=1,
		item_code="FG-1",
	)
	base.update(fields)
	return _Row(**base)


def _outgoing(**fields):
	base = dict(
		qty=10,
		transfer_qty=10,
		amount=1000,
		basic_amount=1000,
		basic_rate=100,
		valuation_rate=100,
		additional_cost=0,
		landed_cost_voucher_amount=0,
		s_warehouse="RM",
		t_warehouse=None,
		is_finished_item=0,
		item_code="RM-1",
	)
	base.update(fields)
	return _Row(**base)


class TestSignedMovementContract(unittest.TestCase):
	def test_incoming_positive_amount(self):
		self.assertEqual(signed_movement_from_row_amount(1500.0, 10.0), 1500.0)

	def test_outgoing_positive_amount(self):
		self.assertEqual(signed_movement_from_row_amount(1500.0, -10.0), -1500.0)

	def test_zero_qty(self):
		self.assertEqual(signed_movement_from_row_amount(1500.0, 0.0), 0.0)

	def test_incoming_negative_amount_raises(self):
		with self.assertRaises(ValuationIntegrityError) as ctx:
			signed_movement_from_row_amount(-1500.0, 10.0)
		msg = str(ctx.exception)
		self.assertIn("I2", msg)
		self.assertIn("not a rounding residual", msg)
		self.assertIn("blocked before persistence", msg)

	def test_outgoing_negative_magnitude_raises(self):
		with self.assertRaises(ValuationIntegrityError):
			signed_movement_from_row_amount(-1500.0, -10.0)


class TestValuationInvariants(unittest.TestCase):
	def test_negative_incoming_rate_raises(self):
		sle = frappe._dict(actual_qty=10, incoming_rate=-5, item_code="X")
		with self.assertRaises(ValuationIntegrityError) as ctx:
			assert_incoming_rate_not_negative(sle)
		self.assertIn("I1", str(ctx.exception))

	def test_sign_inverted_svd_incoming_raises(self):
		sle = frappe._dict(actual_qty=10, stock_value_difference=-1000, company="CO")
		with mock.patch(
			"erpnext_extensions.iran_accounting.domain.riv_valuation_guard.is_irr_company",
			return_value=True,
		), mock.patch(
			"erpnext_extensions.iran_accounting.domain.riv_valuation_guard.get_currency_precision",
			return_value=0,
		), mock.patch(
			"erpnext_extensions.iran_accounting.domain.riv_valuation_guard.get_company_currency",
			return_value="IRR",
		):
			with self.assertRaises(ValuationIntegrityError) as ctx:
				assert_svd_direction(sle)
		self.assertIn("I3", str(ctx.exception))

	def test_sign_inverted_svd_outgoing_raises(self):
		sle = frappe._dict(actual_qty=-10, stock_value_difference=1000, company="CO")
		with mock.patch(
			"erpnext_extensions.iran_accounting.domain.riv_valuation_guard.get_currency_precision",
			return_value=0,
		), mock.patch(
			"erpnext_extensions.iran_accounting.domain.riv_valuation_guard.get_company_currency",
			return_value="IRR",
		):
			with self.assertRaises(ValuationIntegrityError):
				assert_svd_direction(sle)

	def test_healthy_incoming_svd(self):
		assert_svd_direction(frappe._dict(actual_qty=10, stock_value_difference=1000))

	def test_zero_qty_large_leftover_raises(self):
		# Case G: final processed consume-to-zero with leftover warehouse value.
		sle = frappe._dict(
			actual_qty=-300,
			qty_after_transaction=0,
			stock_value=100_000,
			stock_value_difference=-4_554_000_000,
			company="CO",
			voucher_type="Stock Entry",
			valuation_method="Moving Average",
		)
		mark_sle_running_balance_processed(sle)
		self.assertEqual(previous_running_qty(sle), 300)
		with mock.patch(
			"erpnext_extensions.iran_accounting.domain.riv_valuation_guard.get_currency_precision",
			return_value=0,
		), mock.patch(
			"erpnext_extensions.iran_accounting.domain.riv_valuation_guard.get_company_currency",
			return_value="IRR",
		):
			with self.assertRaises(ValuationIntegrityError) as ctx:
				assert_zero_qty_stock_value(sle)
		self.assertIn("I4", str(ctx.exception))
		self.assertIn("Stock valuation integrity (I4)", str(ctx.exception))

	def test_unprocessed_zero_qty_leftover_is_not_final_i4(self):
		# Insert-time / early-process SLE still has default qty_after=0.
		sle = frappe._dict(
			actual_qty=-300,
			qty_after_transaction=0,
			stock_value=-4_554_000_000,
			stock_value_difference=-4_554_000_000,
			company="CO",
			voucher_type="Stock Entry",
		)
		with mock.patch(
			"erpnext_extensions.iran_accounting.domain.riv_valuation_guard.get_currency_precision",
			return_value=0,
		), mock.patch(
			"erpnext_extensions.iran_accounting.domain.riv_valuation_guard.get_company_currency",
			return_value="IRR",
		):
			self.assertFalse(should_assert_zero_qty_stock_value(sle))
			assert_zero_qty_stock_value(sle)

	def test_incident_incoming_transient_zero_qty_is_not_i4(self):
		# Production false-positive shape: incoming movement, insert qty_after=0,
		# Iran sync wrote stock_value = SVD = movement.
		sle = frappe._dict(
			actual_qty=300,
			qty_after_transaction=0,
			incoming_rate=15_180_000,
			outgoing_rate=0,
			valuation_rate=15_180_000,
			stock_value_difference=4_554_000_000,
			stock_value=4_554_000_000,
			company="CO",
			voucher_type="Stock Entry",
			valuation_method="Moving Average",
		)
		self.assertEqual(previous_running_qty(sle), -300)
		with mock.patch(
			"erpnext_extensions.iran_accounting.domain.riv_valuation_guard.get_currency_precision",
			return_value=0,
		), mock.patch(
			"erpnext_extensions.iran_accounting.domain.riv_valuation_guard.get_company_currency",
			return_value="IRR",
		):
			self.assertFalse(is_final_zero_qty_consume_state(sle))
			assert_zero_qty_stock_value(sle)
			mark_sle_running_balance_processed(sle)
			self.assertFalse(should_assert_zero_qty_stock_value(sle))
			assert_zero_qty_stock_value(sle)

	def test_vanilla_engine_identity_marks_processed_only_on_assigned_sle(self):
		class _Engine:
			def __init__(self):
				self.prev_sle_dict = {}

		sle = frappe._dict(item_code="ITEM", warehouse="WH", qty_after_transaction=0)
		engine = _Engine()
		engine.prev_sle_dict[("ITEM", "WH")] = frappe._dict(qty_after_transaction=0)
		self.assertFalse(vanilla_process_sle_assigned_running_balance(engine, sle))
		mark_sle_running_balance_processed_after_vanilla(engine, sle)
		self.assertFalse(sle_running_balance_is_processed(sle))
		engine.prev_sle_dict[("ITEM", "WH")] = sle
		self.assertTrue(vanilla_process_sle_assigned_running_balance(engine, sle))
		mark_sle_running_balance_processed_after_vanilla(engine, sle)
		self.assertTrue(sle_running_balance_is_processed(sle))

	def test_zero_qty_plus_one_residue_allowed(self):
		sle = frappe._dict(
			actual_qty=-10,
			qty_after_transaction=0,
			stock_value=1,
			company="CO",
			voucher_type="Stock Entry",
			valuation_method="Moving Average",
		)
		mark_sle_running_balance_processed(sle)
		with mock.patch(
			"erpnext_extensions.iran_accounting.domain.riv_valuation_guard.get_currency_precision",
			return_value=0,
		), mock.patch(
			"erpnext_extensions.iran_accounting.domain.riv_valuation_guard.get_company_currency",
			return_value="IRR",
		):
			assert_zero_qty_stock_value(sle)

	def test_stock_reconciliation_zero_qty_exempt(self):
		sle = frappe._dict(
			qty_after_transaction=0,
			stock_value=9_000_000,
			company="CO",
			voucher_type="Stock Reconciliation",
		)
		assert_zero_qty_stock_value(sle)

	def test_i5_independent_by_product_over_pool_raises(self):
		doc = _Doc(
			items=[
				_outgoing(amount=1000, basic_amount=1000),
				_incoming(amount=-500, basic_amount=-500, is_finished_item=1),
				_incoming(
					amount=1500,
					basic_amount=1500,
					is_finished_item=0,
					item_code="BY-1",
					secondary_item_type="By-Product",
				),
			]
		)
		with mock.patch(
			"erpnext_extensions.iran_accounting.domain.riv_valuation_guard.is_irr_company",
			return_value=True,
		), mock.patch(
			"erpnext_extensions.iran_accounting.domain.riv_valuation_guard.get_currency_precision",
			return_value=0,
		), mock.patch(
			"erpnext_extensions.iran_accounting.domain.riv_valuation_guard.get_company_currency",
			return_value="IRR",
		):
			with self.assertRaises(ValuationIntegrityError) as ctx:
				assert_manufacture_value_pool(doc)
		self.assertIn("I5", str(ctx.exception))

	def test_i2_negative_manufacture_incoming_amount(self):
		doc = _Doc(items=[_outgoing(), _incoming(amount=-10)])
		with mock.patch(
			"erpnext_extensions.iran_accounting.domain.riv_valuation_guard.is_irr_company",
			return_value=True,
		):
			with self.assertRaises(ValuationIntegrityError) as ctx:
				assert_stock_entry_valuation_integrity(doc)
		self.assertIn("I2", str(ctx.exception))

	def test_transfer_skips_manufacture_pool(self):
		doc = _Doc(purpose="Material Transfer", items=[_outgoing(), _incoming(is_finished_item=0)])
		with mock.patch(
			"erpnext_extensions.iran_accounting.domain.riv_valuation_guard.is_irr_company",
			return_value=True,
		):
			assert_stock_entry_valuation_integrity(doc)


class TestRecalculateWrapperNoPersist(unittest.TestCase):
	def test_invalid_fg_blocked_before_db_update(self):
		fg = _incoming(amount=-2_000_000, basic_amount=-2_000_000, valuation_rate=-2000)
		scrap = _incoming(
			amount=3_000_000,
			basic_amount=3_000_000,
			is_finished_item=0,
			item_code="BY-1",
			secondary_item_type="By-Product",
			valuation_type="Valuation Rate",
		)
		doc = _Doc(
			name="STE-POISON",
			items=[_outgoing(amount=1_000_000, basic_amount=1_000_000), fg, scrap],
			additional_costs=[],
		)
		doc.db_update = mock.Mock()
		for row in doc.items:
			row.db_update = mock.Mock()
			row.name = f"row-{row.item_code}"
			row.s_warehouse = row.get("s_warehouse")
			row.t_warehouse = row.get("t_warehouse")

		def calculate(*_a, **_k):
			return None

		doc.calculate_rate_and_amount = calculate
		doc.set_total_incoming_outgoing_value = mock.Mock()

		original = mock.Mock()
		wrapped = make_recalculate_amounts_wrapper(original)
		engine = mock.Mock()

		with (
			mock.patch("frappe.db.get_value", return_value="اسپاد فارمد دارو"),
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.riv_valuation_guard.is_irr_company",
				return_value=True,
			),
			mock.patch("frappe.get_lazy_doc", return_value=doc),
			mock.patch(
				"erpnext_extensions.iran_accounting.scrap_costing.apply_iran_manufacture_output_contract",
				return_value=False,
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.qty_rate_amount.align_stock_entry_item_amounts",
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.manufacture_rounding.align_manufacture_finished_good_residual",
			),
		):
			with self.assertRaises(ValuationIntegrityError):
				wrapped(engine, "STE-POISON", "row-FG-1")
		doc.db_update.assert_not_called()
		original.assert_not_called()
		for row in doc.items:
			row.db_update.assert_not_called()

	def test_l2_blocks_negative_se_row_before_vanilla(self):
		sle = frappe._dict(
			actual_qty=10,
			incoming_rate=0,
			voucher_type="Stock Entry",
			voucher_detail_no="d1",
			item_code="FG",
		)
		row = frappe._dict(
			name="d1",
			idx=2,
			item_code="FG",
			qty=10,
			transfer_qty=10,
			amount=-1000,
			basic_amount=-1000,
			basic_rate=-100,
			valuation_rate=-100,
			additional_cost=0,
			landed_cost_voucher_amount=0,
			t_warehouse="FG",
			s_warehouse=None,
			is_finished_item=1,
			secondary_item_type=None,
			valuation_type=None,
			parent="STE-1",
		)
		engine = mock.Mock()
		engine.company = "اسپاد فارمد دارو"
		with (
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.riv_valuation_guard.is_irr_company",
				return_value=True,
			),
			mock.patch(
				"frappe.db.get_value",
				side_effect=lambda dt, name, field=None, **k: row
				if dt == "Stock Entry Detail"
				else "Manufacture",
			),
		):
			with self.assertRaises(ValuationIntegrityError) as ctx:
				assert_sle_valuation_integrity_before_vanilla(engine, sle)
		self.assertIn("I2", str(ctx.exception))
		self.assertIn("before SLE write", str(ctx.exception))


class TestManufactureRivIntegrityIntegration(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		from erpnext_extensions.iran_accounting.integration.bootstrap import apply

		apply()
		frappe.set_user("Administrator")
		frappe.flags.iran_gate_defaults = True
		from erpnext_extensions.iran_accounting.e2e_bootstrap import (
			enable_perpetual_inventory,
			get_irr_company,
			get_second_warehouse,
			get_warehouse,
		)

		cls.company = get_irr_company("ESPAD")
		enable_perpetual_inventory(cls.company)
		cls.wh = get_warehouse(cls.company)
		cls.wh2 = get_second_warehouse(cls.company, cls.wh)

	def _cost_center(self):
		cc = frappe.get_cached_value("Company", self.company, "cost_center")
		return cc or frappe.db.get_value(
			"Cost Center", {"company": self.company, "is_group": 0}, "name"
		)

	def _submit_manufacture(self, *, rm, fg, rm_qty, rm_rate, fg_qty, scrap=None, extra_incoming=None, additional_cost=0):
		from erpnext_extensions.iran_accounting.e2e_bootstrap import ensure_test_item
		from erpnext_extensions.iran_accounting.tests.hardening.builders import submit_receipt

		cc = self._cost_center()
		submit_receipt(self.company, rm, rm_qty + 20, rm_rate, self.wh)
		se = frappe.new_doc("Stock Entry")
		se.company = self.company
		se.stock_entry_type = "Manufacture"
		se.purpose = "Manufacture"
		se.set_posting_time = 1
		se.append(
			"items",
			{
				"item_code": rm,
				"qty": rm_qty,
				"transfer_qty": rm_qty,
				"conversion_factor": 1,
				"uom": frappe.db.get_value("Item", rm, "stock_uom"),
				"s_warehouse": self.wh,
				"basic_rate": rm_rate,
				"cost_center": cc,
			},
		)
		se.append(
			"items",
			{
				"item_code": fg,
				"qty": fg_qty,
				"transfer_qty": fg_qty,
				"conversion_factor": 1,
				"uom": frappe.db.get_value("Item", fg, "stock_uom"),
				"t_warehouse": self.wh2,
				"is_finished_item": 1,
				"cost_center": cc,
			},
		)
		if scrap:
			item, qty, secondary = scrap
			se.append(
				"items",
				{
					"item_code": item,
					"qty": qty,
					"transfer_qty": qty,
					"conversion_factor": 1,
					"uom": frappe.db.get_value("Item", item, "stock_uom"),
					"t_warehouse": self.wh2,
					"is_finished_item": 0,
					"secondary_item_type": secondary or "Scrap",
					"allow_zero_valuation_rate": 1,
					"cost_center": cc,
				},
			)
		if extra_incoming:
			item, qty, secondary, rate = extra_incoming
			se.append(
				"items",
				{
					"item_code": item,
					"qty": qty,
					"transfer_qty": qty,
					"conversion_factor": 1,
					"uom": frappe.db.get_value("Item", item, "stock_uom"),
					"t_warehouse": self.wh2,
					"is_finished_item": 0,
					"secondary_item_type": secondary,
					"valuation_type": "Valuation Rate",
					"basic_rate": rate,
					"cost_center": cc,
				},
			)
		if additional_cost:
			oh = frappe.db.get_value(
				"Account", {"company": self.company, "root_type": "Expense", "is_group": 0}, "name"
			)
			se.append(
				"additional_costs",
				{
					"expense_account": oh,
					"description": "v520 overhead",
					"amount": additional_cost,
					"base_amount": additional_cost,
				},
			)
		se.insert(ignore_permissions=True)
		se.submit()
		frappe.db.commit()
		return se

	def test_manufacture_without_scrap(self):
		from erpnext_extensions.iran_accounting.e2e_bootstrap import ensure_test_item

		rm = ensure_test_item(self.company, "V520-NS-RM")
		fg = ensure_test_item(self.company, "V520-NS-FG")
		se = self._submit_manufacture(rm=rm, fg=fg, rm_qty=10, rm_rate=1000, fg_qty=10)
		se.reload()
		fg_row = next(r for r in se.items if r.is_finished_item)
		self.assertGreaterEqual(flt(fg_row.amount), 0)
		self.assertGreaterEqual(flt(fg_row.valuation_rate), 0)

	def test_component_scrap_uses_issued_rate_not_poisoned_warehouse(self):
		from erpnext_extensions.iran_accounting.e2e_bootstrap import ensure_test_item
		from erpnext_extensions.iran_accounting.tests.hardening.builders import submit_receipt

		rm = ensure_test_item(self.company, "V520-CS-RM")
		fg = ensure_test_item(self.company, "V520-CS-FG")
		# Poison target-warehouse avg for the component item.
		submit_receipt(self.company, rm, 5, 50_000_000, self.wh2)
		se = self._submit_manufacture(
			rm=rm, fg=fg, rm_qty=20, rm_rate=3505, fg_qty=18, scrap=(rm, 2, "Scrap")
		)
		se.reload()
		scrap_row = next(r for r in se.items if not r.is_finished_item and r.t_warehouse)
		fg_row = next(r for r in se.items if r.is_finished_item)
		self.assertEqual(flt(scrap_row.basic_rate), 3505)
		self.assertGreaterEqual(flt(fg_row.amount), 0)
		self.assertGreaterEqual(flt(fg_row.valuation_rate), 0)
		incoming_sles = frappe.get_all(
			"Stock Ledger Entry",
			filters={"voucher_no": se.name, "is_cancelled": 0, "actual_qty": (">", 0)},
			fields=["incoming_rate", "stock_value_difference", "item_code"],
		)
		for sle in incoming_sles:
			self.assertGreaterEqual(flt(sle.incoming_rate), 0, sle)
			self.assertGreaterEqual(flt(sle.stock_value_difference), 0, sle)

	def test_product_reject_pool_split(self):
		from erpnext_extensions.iran_accounting.e2e_bootstrap import ensure_test_item
		from erpnext_extensions.iran_accounting.tests.hardening.builders import submit_receipt

		rm = ensure_test_item(self.company, "V520-PR-RM")
		fg = ensure_test_item(self.company, "V520-PR-FG")
		# Product-reject is costed-out; ERPNext looks up target-warehouse rate first.
		submit_receipt(self.company, fg, 1, 1, self.wh2)
		se = self._submit_manufacture(
			rm=rm, fg=fg, rm_qty=100, rm_rate=1000, fg_qty=90, scrap=(fg, 10, "Scrap")
		)
		se.reload()
		fg_row = next(r for r in se.items if r.is_finished_item)
		scrap_row = next(r for r in se.items if not r.is_finished_item and r.t_warehouse)
		self.assertAlmostEqual(flt(fg_row.basic_rate) / flt(scrap_row.basic_rate), 1.0, places=2)
		self.assertGreaterEqual(flt(fg_row.amount), 0)

	def test_independent_by_product_within_pool_allowed(self):
		from erpnext_extensions.iran_accounting.e2e_bootstrap import ensure_test_item
		from erpnext_extensions.iran_accounting.tests.hardening.builders import submit_receipt

		rm = ensure_test_item(self.company, "V520-BP-RM")
		fg = ensure_test_item(self.company, "V520-BP-FG")
		by_item = ensure_test_item(self.company, "V520-BP-BY")
		submit_receipt(self.company, by_item, 10, 100, self.wh2)
		se = self._submit_manufacture(
			rm=rm,
			fg=fg,
			rm_qty=10,
			rm_rate=1000,
			fg_qty=9,
			extra_incoming=(by_item, 1, "By-Product", 100),
		)
		se.reload()
		fg_row = next(r for r in se.items if r.is_finished_item)
		self.assertGreaterEqual(flt(fg_row.amount), 0)

	def test_independent_by_product_over_pool_fail_closed(self):
		from erpnext_extensions.iran_accounting.e2e_bootstrap import ensure_test_item
		from erpnext_extensions.iran_accounting.tests.hardening.builders import submit_receipt

		rm = ensure_test_item(self.company, "V520-BPX-RM")
		fg = ensure_test_item(self.company, "V520-BPX-FG")
		by_item = ensure_test_item(self.company, "V520-BPX-BY")
		submit_receipt(self.company, by_item, 10, 5_000_000, self.wh2)
		with self.assertRaises(ValuationIntegrityError):
			self._submit_manufacture(
				rm=rm,
				fg=fg,
				rm_qty=10,
				rm_rate=1000,
				fg_qty=9,
				extra_incoming=(by_item, 1, "By-Product", 5_000_000),
			)
		frappe.db.rollback()

	def test_manufacture_additional_cost(self):
		from erpnext_extensions.iran_accounting.e2e_bootstrap import ensure_test_item

		rm = ensure_test_item(self.company, "V520-AC-RM")
		fg = ensure_test_item(self.company, "V520-AC-FG")
		se = self._submit_manufacture(
			rm=rm, fg=fg, rm_qty=10, rm_rate=1000, fg_qty=10, additional_cost=500
		)
		se.reload()
		fg_row = next(r for r in se.items if r.is_finished_item)
		self.assertEqual(flt(fg_row.additional_cost), 500)
		self.assertEqual(flt(se.value_difference), 500)

	def test_component_scrap_riv_parity(self):
		from erpnext_extensions.iran_accounting.e2e_bootstrap import ensure_test_item
		from erpnext_extensions.iran_accounting.tests.hardening.builders import run_riv, submit_receipt

		rm = ensure_test_item(self.company, "V520-RIV-RM")
		fg = ensure_test_item(self.company, "V520-RIV-FG")
		submit_receipt(self.company, rm, 5, 80_000_000, self.wh2)
		se = self._submit_manufacture(
			rm=rm, fg=fg, rm_qty=20, rm_rate=4000, fg_qty=18, scrap=(rm, 2, "Scrap")
		)
		se.reload()
		before = {
			(r.item_code, r.t_warehouse or r.s_warehouse): (
				flt(r.basic_rate),
				flt(r.amount),
				flt(r.valuation_rate),
			)
			for r in se.items
		}
		run_riv(self.company, "Stock Entry", se.name)
		se.reload()
		after = {
			(r.item_code, r.t_warehouse or r.s_warehouse): (
				flt(r.basic_rate),
				flt(r.amount),
				flt(r.valuation_rate),
			)
			for r in se.items
		}
		self.assertEqual(before, after)
		run_riv(self.company, "Stock Entry", se.name)
		se.reload()
		after2 = {
			(r.item_code, r.t_warehouse or r.s_warehouse): (
				flt(r.basic_rate),
				flt(r.amount),
				flt(r.valuation_rate),
			)
			for r in se.items
		}
		self.assertEqual(after, after2)
		fg_row = next(r for r in se.items if r.is_finished_item)
		self.assertGreaterEqual(flt(fg_row.amount), 0)

	def test_transfer_not_using_manufacture_contract(self):
		from erpnext_extensions.iran_accounting.e2e_bootstrap import ensure_test_item
		from erpnext_extensions.iran_accounting.tests.hardening.builders import make_transfer, run_riv

		item = ensure_test_item(self.company, "V520-MT")
		with mock.patch(
			"erpnext_extensions.iran_accounting.scrap_costing.apply_iran_manufacture_output_contract",
			wraps=__import__(
				"erpnext_extensions.iran_accounting.scrap_costing",
				fromlist=["apply_iran_manufacture_output_contract"],
			).apply_iran_manufacture_output_contract,
		) as spy:
			se = make_transfer(self.company, item, Decimal("3"), Decimal("111"), self.wh, self.wh2)
			# submit path may call apply; it must no-op (purpose != Manufacture) and not change value identity
			se.reload()
			outgoing = sum(flt(r.amount) for r in se.items if r.s_warehouse)
			incoming = sum(flt(r.amount) for r in se.items if r.t_warehouse)
			self.assertEqual(outgoing, incoming)
			run_riv(self.company, "Stock Entry", se.name)
			se.reload()
			outgoing2 = sum(flt(r.amount) for r in se.items if r.s_warehouse)
			incoming2 = sum(flt(r.amount) for r in se.items if r.t_warehouse)
			self.assertEqual(outgoing2, incoming2)
			self.assertEqual(outgoing, outgoing2)
		_ = spy

	def test_mtfm_value_neutral(self):
		from erpnext_extensions.iran_accounting.e2e_bootstrap import ensure_test_item
		from erpnext_extensions.iran_accounting.tests.hardening.builders import make_transfer

		item = ensure_test_item(self.company, "V520-MTFM")
		se = make_transfer(
			self.company,
			item,
			Decimal("4"),
			Decimal("250"),
			self.wh,
			self.wh2,
			purpose="Material Transfer for Manufacture",
		)
		se.reload()
		self.assertEqual(
			sum(flt(r.amount) for r in se.items if r.s_warehouse),
			sum(flt(r.amount) for r in se.items if r.t_warehouse),
		)

	def test_repack_sign_safety_no_scrap_allocator(self):
		from erpnext_extensions.iran_accounting.e2e_bootstrap import ensure_test_item
		from erpnext_extensions.iran_accounting.tests.hardening.builders import make_repack, run_riv

		item_in = ensure_test_item(self.company, "V520-RPIN")
		item_out = ensure_test_item(self.company, "V520-RPOUT")
		se = make_repack(
			self.company,
			item_in=item_in,
			item_out=item_out,
			warehouse=self.wh,
			qty_in=Decimal("8"),
			rate_in=Decimal("111"),
			qty_out=Decimal("8"),
		)
		run_riv(self.company, "Stock Entry", se.name)
		se.reload()
		for row in se.items:
			if row.t_warehouse and not row.s_warehouse:
				self.assertGreaterEqual(flt(row.amount), 0)

	def test_recalculate_wrapper_installed_and_original_fingerprinted(self):
		import erpnext.stock.stock_ledger as sl

		from erpnext_extensions.iran_accounting.domain.riv_rate_guard import (
			assert_erpnext_riv_rate_patch_supported,
			collect_fingerprint_report,
		)

		assert_erpnext_riv_rate_patch_supported()
		fn = sl.update_entries_after.recalculate_amounts_in_stock_entry
		self.assertTrue(getattr(fn, "_iran_riv_recalculate_wrapper", False))
		orig = sl.update_entries_after._iran_original_recalculate_amounts_in_stock_entry
		self.assertFalse(getattr(orig, "_iran_riv_recalculate_wrapper", False))
		report = collect_fingerprint_report()
		self.assertEqual(
			report["methods"]["recalculate_amounts_in_stock_entry"]["signature"],
			"(self, voucher_no, voucher_detail_no)",
		)

	def test_25333_equivalent_component_scrap_no_negative_fg(self):
		"""Synthetic 25333: huge warehouse scrap vs small issued RM."""
		from erpnext_extensions.iran_accounting.e2e_bootstrap import ensure_test_item
		from erpnext_extensions.iran_accounting.tests.hardening.builders import submit_receipt

		rm = ensure_test_item(self.company, "V520-25333-RM")
		fg = ensure_test_item(self.company, "V520-25333-FG")
		submit_receipt(self.company, rm, 20, 17_000_000, self.wh2)
		se = self._submit_manufacture(
			rm=rm, fg=fg, rm_qty=50, rm_rate=3505, fg_qty=40, scrap=(rm, 5, "Scrap")
		)
		se.reload()
		fg_row = next(r for r in se.items if r.is_finished_item)
		scrap_row = next(r for r in se.items if not r.is_finished_item and r.t_warehouse)
		self.assertEqual(flt(scrap_row.basic_rate), 3505)
		self.assertGreaterEqual(flt(fg_row.amount), 0)
		self.assertGreaterEqual(flt(fg_row.valuation_rate), 0)

	def test_25304_equivalent_product_reject_and_component(self):
		from erpnext_extensions.iran_accounting.e2e_bootstrap import ensure_test_item
		from erpnext_extensions.iran_accounting.tests.hardening.builders import submit_receipt

		rm = ensure_test_item(self.company, "V520-25304-RM")
		fg = ensure_test_item(self.company, "V520-25304-FG")
		submit_receipt(self.company, rm, 10, 9_000_000, self.wh2)
		se = self._submit_manufacture(
			rm=rm, fg=fg, rm_qty=30, rm_rate=2000, fg_qty=25, scrap=(rm, 2, "Scrap")
		)
		# add product-reject on a second document (same FG item)
		se2 = self._submit_manufacture(
			rm=rm, fg=fg, rm_qty=40, rm_rate=2000, fg_qty=35, scrap=(fg, 5, "Scrap")
		)
		se.reload()
		se2.reload()
		for doc in (se, se2):
			fg_row = next(r for r in doc.items if r.is_finished_item)
			self.assertGreaterEqual(flt(fg_row.amount), 0)


def run_v520_valuation_integrity_suite():
	from erpnext_extensions.iran_accounting.integration.bootstrap import apply

	apply()
	loader = unittest.defaultTestLoader
	suite = unittest.TestSuite()
	suite.addTests(loader.loadTestsFromTestCase(TestSignedMovementContract))
	suite.addTests(loader.loadTestsFromTestCase(TestValuationInvariants))
	suite.addTests(loader.loadTestsFromTestCase(TestRecalculateWrapperNoPersist))
	suite.addTests(loader.loadTestsFromTestCase(TestManufactureRivIntegrityIntegration))
	result = unittest.TextTestRunner(verbosity=2).run(suite)
	return {
		"ok": result.wasSuccessful(),
		"tests": result.testsRun,
		"failures": len(result.failures),
		"errors": len(result.errors),
	}

"""v5.5.25 — indivisible Manufacture FG pool stays on the amount.

Component-scrap finished-good pools that do not divide by qty keep
basic_amount / amount as the pool. The integer rate may differ.
SLE mirrors the amount. That residual is not a Stock Adjustment.
"""

from __future__ import annotations

import unittest

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.e2e_bootstrap import (
	enable_perpetual_inventory,
	ensure_test_item,
	get_irr_company,
	get_second_warehouse,
	get_warehouse,
	submit_material_receipt,
)
from erpnext_extensions.iran_accounting.tests.hardening.builders import submit_receipt


def _sa_net(company: str, voucher: str) -> float:
	account = frappe.get_cached_value("Company", company, "stock_adjustment_account")
	rows = frappe.get_all(
		"GL Entry",
		filters={"voucher_no": voucher, "is_cancelled": 0, "account": account},
		fields=["debit", "credit"],
	)
	return sum(flt(row.debit) - flt(row.credit) for row in rows)


def _gl_totals(voucher: str) -> tuple[float, float]:
	rows = frappe.get_all(
		"GL Entry",
		filters={"voucher_no": voucher, "is_cancelled": 0},
		fields=["debit", "credit"],
	)
	return sum(flt(row.debit) for row in rows), sum(flt(row.credit) for row in rows)


def _fg_sle(voucher: str, item: str) -> float:
	rows = frappe.get_all(
		"Stock Ledger Entry",
		filters={"voucher_no": voucher, "item_code": item, "is_cancelled": 0},
		fields=["actual_qty", "stock_value_difference"],
	)
	incoming = [row for row in rows if flt(row.actual_qty) > 0]
	return flt(incoming[0].stock_value_difference)


def _sle_sum(voucher: str) -> float:
	rows = frappe.get_all(
		"Stock Ledger Entry",
		filters={"voucher_no": voucher, "is_cancelled": 0},
		fields=["stock_value_difference"],
	)
	return sum(flt(row.stock_value_difference) for row in rows)


class TestManufactureFgPoolResidual(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		from erpnext_extensions.iran_accounting.integration.bootstrap import apply

		apply()
		frappe.set_user("Administrator")
		frappe.flags.iran_gate_defaults = True
		cls.company = get_irr_company("ESPAD")
		enable_perpetual_inventory(cls.company)
		cls.wh = get_warehouse(cls.company)
		cls.wh2 = get_second_warehouse(cls.company, cls.wh)
		from erpnext_extensions.iran_accounting.tests.test_riv_valuation_integrity import (
			TestManufactureRivIntegrityIntegration,
		)

		cls.host = TestManufactureRivIntegrityIntegration()
		cls.host.company = cls.company
		cls.host.wh = cls.wh
		cls.host.wh2 = cls.wh2

	def _manufacture(self, tag, *, rm_qty, rm_rate, fg_qty, scrap_qty, additional_cost=0):
		rm = ensure_test_item(self.company, f"POOL5525-{tag}-RM")
		fg = ensure_test_item(self.company, f"POOL5525-{tag}-FG")
		submit_receipt(self.company, rm, 20, 17_000_000, self.wh2)
		scrap = (rm, scrap_qty, "Scrap") if scrap_qty else None
		se = self.host._submit_manufacture(
			rm=rm,
			fg=fg,
			rm_qty=rm_qty,
			rm_rate=rm_rate,
			fg_qty=fg_qty,
			scrap=scrap,
			additional_cost=additional_cost,
		)
		se.reload()
		return se, fg

	def _assert_pool_owned(self, se, fg_item, expected_amount, expected_rate):
		fg_row = next(row for row in se.items if row.is_finished_item)
		self.assertEqual(flt(fg_row.qty), flt(fg_row.transfer_qty))
		self.assertEqual(flt(fg_row.basic_rate), expected_rate)
		self.assertEqual(flt(fg_row.valuation_rate), expected_rate)
		self.assertEqual(flt(fg_row.basic_amount), expected_amount)
		self.assertEqual(flt(fg_row.amount), expected_amount)
		self.assertEqual(_fg_sle(se.name, fg_item), expected_amount)
		self.assertEqual(_sle_sum(se.name), 0)
		self.assertEqual(flt(se.value_difference), 0)
		self.assertEqual(_sa_net(self.company, se.name), 0)
		debit, credit = _gl_totals(se.name)
		self.assertEqual(debit, credit)
		self.assertEqual(debit, sum(flt(row.amount) for row in se.items if row.s_warehouse))

	def test_exact_25333_pool_owned_by_fg_amount(self):
		se, fg = self._manufacture("25333", rm_qty=50, rm_rate=3505, fg_qty=40, scrap_qty=5)
		consume = next(row for row in se.items if row.s_warehouse)
		scrap = next(row for row in se.items if row.secondary_item_type == "Scrap")
		self.assertEqual(flt(consume.amount), 175250)
		self.assertEqual(flt(scrap.basic_rate), 3505)
		self.assertEqual(flt(scrap.amount), 17525)
		self._assert_pool_owned(se, fg, 157725, 3943)
		debit, credit = _gl_totals(se.name)
		self.assertEqual(debit, 175250)
		self.assertEqual(credit, 175250)

	def test_fg_qty_39_residual_plus_9(self):
		se, fg = self._manufacture("Q39", rm_qty=50, rm_rate=3505, fg_qty=39, scrap_qty=5)
		# 157725 / 39 = 4044.2307 → 4044; 4044×39 = 157716; residual +9
		self._assert_pool_owned(se, fg, 157725, 4044)

	def test_fg_qty_41_residual_minus_2(self):
		se, fg = self._manufacture("Q41", rm_qty=50, rm_rate=3505, fg_qty=41, scrap_qty=5)
		# 157725 / 41 = 3846.9512 → 3847; 3847×41 = 157727; residual −2
		self._assert_pool_owned(se, fg, 157725, 3847)

	def test_fg_qty_45_divides_evenly(self):
		se, fg = self._manufacture("Q45", rm_qty=50, rm_rate=3505, fg_qty=45, scrap_qty=5)
		self._assert_pool_owned(se, fg, 157725, 3505)

	def test_rate_3504_divides_evenly(self):
		se, fg = self._manufacture("R3504", rm_qty=50, rm_rate=3504, fg_qty=40, scrap_qty=5)
		self._assert_pool_owned(se, fg, 157680, 3942)

	def test_rate_3506_residual_plus_10(self):
		se, fg = self._manufacture("R3506", rm_qty=50, rm_rate=3506, fg_qty=40, scrap_qty=5)
		# pool 157770 / 40 = 3944.25 → 3944; 3944×40 = 157760; residual +10
		self._assert_pool_owned(se, fg, 157770, 3944)

	def test_scrap_qty_4_residual_minus_10(self):
		se, fg = self._manufacture("S4", rm_qty=50, rm_rate=3505, fg_qty=40, scrap_qty=4)
		# pool 161230 / 40 = 4030.75 → 4031; 4031×40 = 161240; residual −10
		self._assert_pool_owned(se, fg, 161230, 4031)

	def test_scrap_qty_6_residual_minus_20(self):
		se, fg = self._manufacture("S6", rm_qty=50, rm_rate=3505, fg_qty=40, scrap_qty=6)
		# pool 154220 / 40 = 3855.5 → 3856; 3856×40 = 154240; residual −20
		self._assert_pool_owned(se, fg, 154220, 3856)

	def test_additional_cost_does_not_create_fake_stock_adjustment(self):
		se, fg = self._manufacture(
			"AC1", rm_qty=50, rm_rate=3505, fg_qty=40, scrap_qty=5, additional_cost=1
		)
		fg_row = next(row for row in se.items if row.is_finished_item)
		self.assertEqual(_fg_sle(se.name, fg), flt(fg_row.amount))
		self.assertEqual(_sa_net(self.company, se.name), 0)
		debit, credit = _gl_totals(se.name)
		self.assertEqual(debit, credit)

	def test_no_scrap_rate_gap_remains_real_stock_adjustment(self):
		"""Without component scrap the qty×rate gap is a real value_difference.

		It must stay on Stock Adjustment. It must not be absorbed into the FG
		amount, and the document must still match the SLE.
		"""
		se, fg = self._manufacture("NOSCRAP", rm_qty=50, rm_rate=3505, fg_qty=40, scrap_qty=None)
		fg_row = next(row for row in se.items if row.is_finished_item)
		self.assertEqual(flt(fg_row.amount), 175240)
		self.assertEqual(flt(fg_row.basic_rate) * flt(fg_row.qty), 175240)
		self.assertEqual(_fg_sle(se.name, fg), 175240)
		self.assertEqual(_sle_sum(se.name), flt(se.value_difference))
		self.assertEqual(flt(se.value_difference), -10)
		self.assertEqual(_sa_net(self.company, se.name), 10)
		debit, credit = _gl_totals(se.name)
		self.assertEqual(debit, credit)
		self.assertEqual(debit, 175250)

	def test_multi_source_component_scrap_pool(self):
		rm_a = ensure_test_item(self.company, "POOL5525-MULTI-A")
		rm_b = ensure_test_item(self.company, "POOL5525-MULTI-B")
		fg = ensure_test_item(self.company, "POOL5525-MULTI-FG")
		submit_material_receipt(self.company, rm_a, 30, 1000, self.wh)
		submit_material_receipt(self.company, rm_b, 30, 2000, self.wh)
		submit_receipt(self.company, rm_a, 20, 17_000_000, self.wh2)
		cc = frappe.get_cached_value("Company", self.company, "cost_center")
		se = frappe.new_doc("Stock Entry")
		se.company = self.company
		se.stock_entry_type = "Manufacture"
		se.purpose = "Manufacture"
		se.set_posting_time = 1
		for item, qty, rate in ((rm_a, 10, 1000), (rm_b, 10, 2000)):
			se.append(
				"items",
				{
					"item_code": item,
					"qty": qty,
					"transfer_qty": qty,
					"conversion_factor": 1,
					"uom": frappe.db.get_value("Item", item, "stock_uom"),
					"s_warehouse": self.wh,
					"basic_rate": rate,
					"cost_center": cc,
				},
			)
		se.append(
			"items",
			{
				"item_code": fg,
				"qty": 7,
				"transfer_qty": 7,
				"conversion_factor": 1,
				"uom": frappe.db.get_value("Item", fg, "stock_uom"),
				"t_warehouse": self.wh2,
				"is_finished_item": 1,
				"cost_center": cc,
			},
		)
		se.append(
			"items",
			{
				"item_code": rm_a,
				"qty": 1,
				"transfer_qty": 1,
				"conversion_factor": 1,
				"uom": frappe.db.get_value("Item", rm_a, "stock_uom"),
				"t_warehouse": self.wh2,
				"is_finished_item": 0,
				"secondary_item_type": "Scrap",
				"allow_zero_valuation_rate": 1,
				"cost_center": cc,
			},
		)
		from erpnext_extensions.iran_accounting.e2e_bootstrap import apply_stock_entry_site_defaults

		frappe.flags.iran_gate_defaults = True
		apply_stock_entry_site_defaults(se)
		se.insert(ignore_permissions=True)
		se.submit()
		frappe.db.commit()
		se.reload()
		# Pool 10×1000 + 10×2000 − 1×1000 = 29000. 29000/7 = 4142.857 → 4143.
		# 4143×7 = 29001. Residual −1. Amount stays 29000.
		self._assert_pool_owned(se, fg, 29000, 4143)
		scrap = next(row for row in se.items if row.secondary_item_type == "Scrap")
		self.assertEqual(flt(scrap.amount), 1000)
		self.assertEqual(flt(scrap.basic_rate), 1000)

	def test_repack_does_not_take_manufacture_pool_policy(self):
		rm = ensure_test_item(self.company, "POOL5525-RPK-RM")
		fg = ensure_test_item(self.company, "POOL5525-RPK-FG")
		submit_material_receipt(self.company, rm, 80, 3505, self.wh)
		cc = frappe.get_cached_value("Company", self.company, "cost_center")
		se = frappe.new_doc("Stock Entry")
		se.company = self.company
		se.stock_entry_type = "Repack"
		se.purpose = "Repack"
		se.set_posting_time = 1
		se.append(
			"items",
			{
				"item_code": rm,
				"qty": 50,
				"transfer_qty": 50,
				"conversion_factor": 1,
				"uom": frappe.db.get_value("Item", rm, "stock_uom"),
				"s_warehouse": self.wh,
				"basic_rate": 3505,
				"cost_center": cc,
			},
		)
		se.append(
			"items",
			{
				"item_code": fg,
				"qty": 40,
				"transfer_qty": 40,
				"conversion_factor": 1,
				"uom": frappe.db.get_value("Item", fg, "stock_uom"),
				"t_warehouse": self.wh2,
				"is_finished_item": 1,
				"cost_center": cc,
			},
		)
		from erpnext_extensions.iran_accounting.e2e_bootstrap import apply_stock_entry_site_defaults

		frappe.flags.iran_gate_defaults = True
		apply_stock_entry_site_defaults(se)
		se.insert(ignore_permissions=True)
		se.submit()
		frappe.db.commit()
		se.reload()
		fg_row = next(row for row in se.items if row.is_finished_item)
		self.assertEqual(se.purpose, "Repack")
		self.assertEqual(_fg_sle(se.name, fg), flt(fg_row.amount))
		self.assertEqual(_sle_sum(se.name), flt(se.value_difference))
		debit, credit = _gl_totals(se.name)
		self.assertEqual(debit, credit)
		self.assertNotEqual(flt(fg_row.amount), 157725)

	def test_second_recalculate_does_not_collapse_pool(self):
		from erpnext.stock.doctype.repost_item_valuation.repost_item_valuation import repost

		from erpnext_extensions.iran_accounting.domain.riv_valuation_guard import (
			apply_irr_stock_entry_contract_after_calculate,
		)

		se, fg = self._manufacture("IDEM", rm_qty=50, rm_rate=3505, fg_qty=40, scrap_qty=5)
		self._assert_pool_owned(se, fg, 157725, 3943)
		se.calculate_rate_and_amount(reset_outgoing_rate=False, raise_error_if_no_rate=False)
		apply_irr_stock_entry_contract_after_calculate(se)
		fg_row = next(row for row in se.items if row.is_finished_item)
		self.assertEqual(flt(fg_row.basic_amount), 157725)
		self.assertEqual(flt(fg_row.amount), 157725)
		self.assertEqual(flt(fg_row.basic_rate), 3943)

		riv = frappe.new_doc("Repost Item Valuation")
		riv.company = self.company
		riv.based_on = "Item and Warehouse"
		riv.item_code = fg
		riv.warehouse = self.wh2
		riv.posting_date = se.posting_date
		riv.posting_time = se.posting_time
		riv.allow_negative_stock = 1
		riv.flags.ignore_permissions = True
		riv.insert(ignore_permissions=True)
		repost(riv)
		se.reload()
		self._assert_pool_owned(se, fg, 157725, 3943)
		self.assertEqual(flt(se.value_difference), 0)
		self.assertEqual(_sa_net(self.company, se.name), 0)

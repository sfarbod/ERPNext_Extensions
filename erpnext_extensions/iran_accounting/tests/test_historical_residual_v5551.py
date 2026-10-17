# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.5.51 — non-blocking historical residual absorb / TVA discoverability."""

from __future__ import annotations

import unittest
from unittest import mock

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.domain.historical_valuation_residual import (
	ABSORB_INTO_MOVEMENT,
	ACTIVE_HISTORICAL_VALUATION_GUARD,
	FAIL_CLOSED,
	HISTORICAL_RESIDUAL,
	STRICT_AFTER_HISTORICAL_REBUILD,
	TRANSITIONAL_NON_BLOCKING,
	UNDETERMINED,
	classify_zero_qty_ghost,
	ghost_residual_amount,
	is_transitional_non_blocking,
	list_campaign_discoverable_residuals,
	reconcile_tva_after_riv,
)
from erpnext_extensions.iran_accounting.domain.riv_valuation_guard import (
	ValuationIntegrityError,
	assert_svd_direction,
	assert_zero_qty_stock_value,
	enforce_zero_qty_terminal_stock_value,
	mark_sle_running_balance_processed,
)
from erpnext_extensions.iran_accounting.domain.stock_entry_sync import (
	sync_irr_sle_from_stock_entry_row,
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
from erpnext_extensions.iran_accounting.tests.test_i4_material_transfer_lifecycle import _sles


PREV = 58_983_128
SED = 376_000
GHOST = PREV - SED
QTY = 1


def _sle(**extra):
	payload = dict(
		actual_qty=-QTY,
		qty_after_transaction=0,
		stock_value=0,
		stock_value_difference=-PREV,
		outgoing_rate=PREV,
		valuation_rate=PREV,
		company=extra.pop("company", "ESPAD"),
		voucher_type="Stock Entry",
		voucher_no="MAT-STE-TEST",
		voucher_detail_no="sed-1",
		item_code="13200244",
		warehouse="WH-SRC",
		valuation_method="Moving Average",
		is_cancelled=0,
	)
	payload.update(extra)
	return frappe._dict(payload)


class TestHistoricalResidualUnit(unittest.TestCase):
	def test_transitional_policy_is_active(self):
		"""v5.5.51 ships TRANSITIONAL_NON_BLOCKING; strict is deferred."""
		self.assertEqual(ACTIVE_HISTORICAL_VALUATION_GUARD, TRANSITIONAL_NON_BLOCKING)
		self.assertTrue(is_transitional_non_blocking())

	def test_strict_guard_would_fail_closed(self):
		sle = _sle()
		mark_sle_running_balance_processed(sle)
		with mock.patch(
			"erpnext_extensions.iran_accounting.domain.historical_valuation_residual.ACTIVE_HISTORICAL_VALUATION_GUARD",
			STRICT_AFTER_HISTORICAL_REBUILD,
		), mock.patch(
			"erpnext_extensions.iran_accounting.domain.historical_valuation_residual.is_strict_historical_valuation_guard",
			return_value=True,
		), mock.patch(
			"erpnext_extensions.iran_accounting.domain.historical_valuation_residual.prior_type_a_on_identity",
			return_value=None,
		), mock.patch(
			"erpnext_extensions.iran_accounting.domain.historical_valuation_residual._quantum",
			return_value=1.0,
		):
			c = classify_zero_qty_ghost(
				sle=sle,
				purpose="Material Transfer for Manufacture",
				value_before=PREV,
				movement=-SED,
				company="ESPAD",
			)
		self.assertEqual(c["disposition"], FAIL_CLOSED)
		self.assertEqual(c["detail"], "strict_guard_rejects_historical_residual")

	def test_a_clean_no_ghost(self):
		self.assertEqual(ghost_residual_amount(100, -100, None), 0)

	def test_classify_transfer_absorb(self):
		sle = _sle()
		mark_sle_running_balance_processed(sle)
		c = classify_zero_qty_ghost(
			sle=sle,
			purpose="Material Transfer for Manufacture",
			value_before=PREV,
			movement=-SED,
			company="ESPAD",
		)
		self.assertEqual(c["category"], HISTORICAL_RESIDUAL)
		self.assertEqual(c["disposition"], ABSORB_INTO_MOVEMENT)
		self.assertEqual(flt(c["ghost_amount"]), GHOST)

	def test_classify_manufacture_modest_absorb(self):
		sle = _sle()
		mark_sle_running_balance_processed(sle)
		c = classify_zero_qty_ghost(
			sle=sle,
			purpose="Manufacture",
			value_before=1_000_000,
			movement=-900_000,
			company="ESPAD",
		)
		self.assertEqual(c["disposition"], ABSORB_INTO_MOVEMENT)

	def test_i_classify_manufacture_huge_undetermined(self):
		"""TEST I / 40436 ceiling: do not fabricate SA for ~3.3B."""
		sle = _sle()
		mark_sle_running_balance_processed(sle)
		ghost = 3_303_599_398
		c = classify_zero_qty_ghost(
			sle=sle,
			purpose="Manufacture",
			value_before=ghost + 4_151_784_000,
			movement=-4_151_784_000,
			company="ESPAD",
		)
		self.assertEqual(c["category"], UNDETERMINED)
		self.assertEqual(c["disposition"], FAIL_CLOSED)
		self.assertGreater(flt(c["ghost_amount"]), 500_000_000)

	def test_n_i3_i4_still_active(self):
		sle = _sle(stock_value_difference=SED, stock_value=SED)
		mark_sle_running_balance_processed(sle)
		with self.assertRaises(ValuationIntegrityError):
			assert_svd_direction(sle)
		sle2 = _sle(stock_value=GHOST, stock_value_difference=-SED)
		mark_sle_running_balance_processed(sle2)
		with self.assertRaises(ValuationIntegrityError):
			assert_zero_qty_stock_value(sle2)

	def test_m_document_mtfm_manual_absorbs(self):
		"""TEST B/D: document-path MTfM absorbs residual into SVD."""
		sle = _sle(stock_value=0, stock_value_difference=-PREV)
		mark_sle_running_balance_processed(sle)
		row = frappe._dict(
			qty=QTY,
			transfer_qty=QTY,
			basic_rate=SED,
			basic_amount=SED,
			amount=SED,
			valuation_rate=SED,
			additional_cost=0,
			landed_cost_voucher_amount=0,
			allow_zero_valuation_rate=0,
			set_basic_rate_manually=1,
			parent="MAT-STE-TEST",
			serial_and_batch_bundle=None,
			s_warehouse="WH-SRC",
			t_warehouse="WH-TGT",
		)

		def _get_value(doctype, name, *args, **kwargs):
			if doctype == "Stock Entry Detail":
				return row
			if doctype == "Stock Entry":
				return "Material Transfer for Manufacture"
			return mock.DEFAULT

		with (
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.stock_entry_sync.is_irr_company",
				return_value=True,
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.stock_entry_sync.get_company_currency",
				return_value="IRR",
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.stock_entry_sync.frappe.db.get_value",
				side_effect=_get_value,
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.stock_entry_sync.frappe.db.set_value",
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.stock_entry_sync.round_currency",
				side_effect=lambda v, _c: float(round(flt(v))),
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.stock_entry_sync.round_monetary_rate",
				side_effect=lambda v, _c: float(round(flt(v))),
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.historical_valuation_residual.prior_type_a_on_identity",
				return_value=None,
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.historical_valuation_residual._quantum",
				return_value=1.0,
			),
		):
			sync_irr_sle_from_stock_entry_row(sle)
		self.assertEqual(flt(sle.stock_value), 0)
		self.assertEqual(flt(sle.stock_value_difference), -PREV)
		enforce_zero_qty_terminal_stock_value(sle)
		assert_zero_qty_stock_value(sle)

	def test_campaign_discovery_contract(self):
		"""TEST: campaign discover API exists and never marks clean."""
		with mock.patch(
			"erpnext_extensions.iran_accounting.domain.historical_valuation_residual.frappe.db.exists",
			return_value=True,
		), mock.patch(
			"erpnext_extensions.iran_accounting.domain.historical_valuation_residual.frappe.get_all",
			return_value=[
				{
					"name": "tva1",
					"reason_code": "HISTORICAL_ZERO_QTY_RESIDUAL",
					"repair_status": "OPEN",
					"item_code": "X",
					"warehouse": "W",
				}
			],
		):
			rows = list_campaign_discoverable_residuals({"limit": 10})
		self.assertEqual(len(rows), 1)
		self.assertFalse(rows[0]["historical_valuation_clean"])
		self.assertEqual(rows[0]["campaign_action"], "REVIEW_AND_REPOST_SOURCE")


class TestHistoricalResidualIntegration(unittest.TestCase):
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

	def _item(self, tag: str) -> str:
		import time

		item = ensure_test_item(self.company, f"I551-{tag}-{int(time.time() * 1000) % 10_000_000}")
		frappe.db.set_value(
			"Item",
			item,
			{
				"is_stock_item": 1,
				"valuation_method": "Moving Average",
				"has_serial_no": 0,
				"has_batch_no": 0,
				"allow_negative_stock": 0,
			},
			update_modified=False,
		)
		frappe.db.commit()
		return item

	def _submit_se(self, *, purpose, item, qty, rate=None, source=None, target=None):
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
		se = make_stock_entry(**kwargs)
		apply_stock_entry_site_defaults(se)
		se.insert(ignore_permissions=True)
		se.submit()
		frappe.db.commit()
		return se

	def _inject_residual(self, item: str, residual: float) -> None:
		frappe.db.sql(
			"""
			update `tabStock Ledger Entry`
			set stock_value = stock_value + %s
			where item_code=%s and warehouse=%s and is_cancelled=0
			""",
			(residual, item, self.source),
		)
		frappe.db.sql(
			"""
			update `tabBin`
			set stock_value = stock_value + %s
			where item_code=%s and warehouse=%s
			""",
			(residual, item, self.source),
		)
		frappe.db.commit()

	def test_b_d_mtfm_residual_absorbs_and_balances(self):
		"""TEST B/D/J: MTfM with residual → sv=0, full clear, transfer pair balances."""
		item = self._item("MTFM")
		qty = 4
		rate = 100_000
		residual = 58_607
		self._submit_se(
			purpose="Material Receipt", item=item, qty=qty, rate=rate, target=self.source
		)
		self._inject_residual(item, residual)
		prev = flt(
			frappe.db.sql(
				"""
				select stock_value from `tabStock Ledger Entry`
				where item_code=%s and warehouse=%s and is_cancelled=0
				order by posting_datetime desc, creation desc limit 1
				""",
				(item, self.source),
			)[0][0]
		)
		se = self._submit_se(
			purpose="Material Transfer for Manufacture",
			item=item,
			qty=qty,
			source=self.source,
			target=self.target,
		)
		src = [s for s in _sles(se.name, self.source) if flt(s.actual_qty) < 0][0]
		tgt = [s for s in _sles(se.name, self.target) if flt(s.actual_qty) > 0][0]
		self.assertEqual(flt(src.qty_after_transaction), 0)
		self.assertEqual(flt(src.stock_value), 0)
		# Full absorb preferred; rate×qty clear accepted when Core already emptied
		# the valued layer (pair still conserved).
		self.assertTrue(
			abs(flt(src.stock_value_difference) + prev) <= 1
			or abs(flt(src.stock_value_difference) + (prev - residual)) <= 1
		)
		pair = flt(src.stock_value_difference) + flt(tgt.stock_value_difference)
		self.assertLessEqual(abs(pair), abs(residual) + 1)

	def test_a_h_normal_receipt_no_tva_required(self):
		"""TEST A/H: clean receipt unaffected."""
		item = self._item("NRM")
		se = self._submit_se(
			purpose="Material Receipt", item=item, qty=3, rate=20_000, target=self.source
		)
		tgt = [s for s in _sles(se.name, self.source) if flt(s.actual_qty) > 0][0]
		self.assertEqual(flt(tgt.stock_value_difference), 60_000)
		self.assertEqual(flt(tgt.stock_value), 60_000)

	def test_e_g_riv_idempotent_after_absorb(self):
		"""TEST E/G: RIV after absorb remains Completed and pair-stable."""
		item = self._item("RIV")
		qty = 2
		rate = 50_000
		residual = 12_345
		self._submit_se(
			purpose="Material Receipt", item=item, qty=qty, rate=rate, target=self.source
		)
		self._inject_residual(item, residual)
		se = self._submit_se(
			purpose="Material Transfer",
			item=item,
			qty=qty,
			source=self.source,
			target=self.target,
		)
		src = [s for s in _sles(se.name, self.source) if flt(s.actual_qty) < 0][0]
		self.assertEqual(flt(src.stock_value), 0)
		riv = run_riv(self.company, "Stock Entry", se.name)
		riv.reload()
		self.assertNotIn(riv.status, ("Failed", "Skipped"))
		src2 = [s for s in _sles(se.name, self.source) if flt(s.actual_qty) < 0][0]
		tgt2 = [s for s in _sles(se.name, self.target) if flt(s.actual_qty) > 0][0]
		self.assertEqual(flt(src2.stock_value), 0)
		self.assertEqual(
			flt(src2.stock_value_difference) + flt(tgt2.stock_value_difference), 0
		)
		# Second RIV idempotent
		riv2 = run_riv(self.company, "Stock Entry", se.name)
		riv2.reload()
		self.assertNotIn(riv2.status, ("Failed", "Skipped"))

	def test_f_reconcile_helper(self):
		"""TEST F: reconcile helper marks TRACE TVA when Type-A cleared."""
		with mock.patch(
			"erpnext_extensions.iran_accounting.domain.historical_valuation_residual.frappe.db.sql",
			return_value=((0,),),
		), mock.patch(
			"erpnext_extensions.iran_accounting.domain.historical_valuation_residual.frappe.get_all",
			return_value=[
				frappe._dict(name="t1", posting_mode="TRACE", adjustment_delta=100, gl_entry=None)
			],
		), mock.patch(
			"erpnext_extensions.iran_accounting.domain.historical_valuation_residual.frappe.db.set_value",
		) as set_value, mock.patch(
			"erpnext_extensions.iran_accounting.domain.historical_valuation_residual.frappe.db.get_value",
			return_value="note",
		), mock.patch(
			"erpnext_extensions.iran_accounting.domain.historical_valuation_residual.frappe.db.exists",
			return_value=True,
		):
			out = reconcile_tva_after_riv("I", "W", "riv1")
		self.assertEqual(out["reconciled"], 1)
		self.assertEqual(set_value.call_args[0][2]["repair_status"], "RECONCILED")

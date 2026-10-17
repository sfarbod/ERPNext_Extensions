# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.5.50 — zero-qty stock_value integrity (PHASE 1 preventive contract).

Iran sync must not reintroduce nonzero stock_value after Core empties a
warehouse (qty_after=0). I3/I4 remain enabled. SED SVD / transfer conservation
is preserved; only terminal stock_value is cleared.
"""

from __future__ import annotations

import unittest
from unittest import mock

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.domain.riv_valuation_guard import (
	ValuationIntegrityError,
	assert_svd_direction,
	assert_zero_qty_stock_value,
	enforce_zero_qty_terminal_stock_value,
	mark_sle_running_balance_processed,
	restore_vanilla_zero_qty_terminal_stock_value,
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
from erpnext_extensions.iran_accounting.tests.test_i4_material_transfer_lifecycle import (
	_sles,
	make_valid_batch,
)


# MAT-STE-2026-40942 economics (consume last 1 unit with sticky residual)
PREV_VALUE = 58_983_128
SED_AMOUNT = 376_000
LEFTOVER = PREV_VALUE - SED_AMOUNT  # 58_607_128
QTY = 1
RATE = SED_AMOUNT


def _incident_sle(**extra):
	payload = dict(
		actual_qty=-QTY,
		qty_after_transaction=0,
		incoming_rate=0,
		outgoing_rate=RATE,
		valuation_rate=RATE,
		stock_value_difference=-PREV_VALUE,
		stock_value=0,
		company=extra.pop("company", "ESPAD"),
		voucher_type="Stock Entry",
		voucher_no="MAT-STE-2026-40942",
		voucher_detail_no="sed-40942",
		item_code="13200244",
		warehouse="انبار پایکار خط تولید اسپاد فارمد",
		valuation_method="Moving Average",
		is_cancelled=0,
	)
	payload.update(extra)
	return frappe._dict(payload)


def _sed_row(**extra):
	payload = dict(
		qty=QTY,
		transfer_qty=QTY,
		basic_rate=RATE,
		basic_amount=SED_AMOUNT,
		additional_cost=0,
		landed_cost_voucher_amount=0,
		valuation_rate=RATE,
		amount=SED_AMOUNT,
		allow_zero_valuation_rate=0,
		set_basic_rate_manually=0,
		parent="MAT-STE-2026-40942",
		serial_and_batch_bundle=None,
		s_warehouse="انبار پایکار خط تولید اسپاد فارمد",
		t_warehouse=None,
	)
	payload.update(extra)
	return frappe._dict(payload)


def _sync_ctx(row, purpose: str):
	"""Stack of patches for sync_irr_sle_from_stock_entry_row unit tests."""
	return mock.patch.multiple(
		"erpnext_extensions.iran_accounting.domain.stock_entry_sync",
		is_irr_company=mock.Mock(return_value=True),
		get_company_currency=mock.Mock(return_value="IRR"),
		round_currency=mock.Mock(side_effect=lambda v, _c: float(round(flt(v)))),
		round_monetary_rate=mock.Mock(side_effect=lambda v, _c: float(round(flt(v)))),
	)


class TestZeroQtyEnforceUnit(unittest.TestCase):
	"""Unit contract for enforce + document-authority stamp."""

	def test_enforce_clears_leftover_keeps_svd(self):
		sle = _incident_sle(stock_value=LEFTOVER, stock_value_difference=-SED_AMOUNT)
		mark_sle_running_balance_processed(sle)
		self.assertTrue(enforce_zero_qty_terminal_stock_value(sle))
		self.assertEqual(flt(sle.stock_value), 0)
		self.assertEqual(flt(sle.stock_value_difference), -SED_AMOUNT)
		assert_zero_qty_stock_value(sle)

	def test_enforce_noop_when_already_zero(self):
		sle = _incident_sle()
		mark_sle_running_balance_processed(sle)
		self.assertFalse(enforce_zero_qty_terminal_stock_value(sle))
		self.assertEqual(flt(sle.stock_value), 0)

	def test_enforce_skips_unprocessed_transient(self):
		sle = _incident_sle(stock_value=LEFTOVER, stock_value_difference=-SED_AMOUNT)
		# no mark → not final consume state
		self.assertFalse(enforce_zero_qty_terminal_stock_value(sle))
		self.assertEqual(flt(sle.stock_value), LEFTOVER)

	def _run_sync(self, sle, row, purpose: str):
		real_get_value = frappe.db.get_value

		def _get_value_safe(*args, **kwargs):
			doctype = kwargs.get("doctype")
			if args:
				doctype = args[0]
			if doctype == "Stock Entry Detail":
				return row
			if doctype == "Stock Entry":
				return purpose
			return real_get_value(*args, **kwargs)

		with (
			_sync_ctx(row, purpose),
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.stock_entry_sync.frappe.db.get_value",
				side_effect=_get_value_safe,
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.historical_valuation_residual.frappe.db.get_value",
				side_effect=_get_value_safe,
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.stock_entry_sync.frappe.db.set_value",
			),
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.stock_entry_sync._lookup_previous_stock_value",
				return_value=None,
			),
		):
			sync_irr_sle_from_stock_entry_row(sle)

	def test_document_manufacture_stamp_does_not_reintroduce_leftover(self):
		"""TEST C/D: Manufacture absorb residual into SVD; sv stays 0 (v5.5.51)."""
		sle = _incident_sle()
		mark_sle_running_balance_processed(sle)
		with mock.patch(
			"erpnext_extensions.iran_accounting.domain.historical_valuation_residual.prior_type_a_on_identity",
			return_value=None,
		), mock.patch(
			"erpnext_extensions.iran_accounting.domain.historical_valuation_residual._quantum",
			return_value=1.0,
		):
			self._run_sync(sle, _sed_row(), "Manufacture")
		self.assertEqual(flt(sle.stock_value), 0)
		# Residual absorbed: full previous layer cleared (not SED-only ghost).
		self.assertEqual(flt(sle.stock_value_difference), -PREV_VALUE)
		assert_zero_qty_stock_value(sle)

	def test_mtfm_manual_document_path_clears_terminal_value(self):
		"""TEST A: MTfM document path absorbs residual; sv=0; full clear SVD."""
		sle = _incident_sle()
		mark_sle_running_balance_processed(sle)
		with mock.patch(
			"erpnext_extensions.iran_accounting.domain.historical_valuation_residual.prior_type_a_on_identity",
			return_value=None,
		), mock.patch(
			"erpnext_extensions.iran_accounting.domain.historical_valuation_residual._quantum",
			return_value=1.0,
		):
			self._run_sync(
				sle, _sed_row(set_basic_rate_manually=1), "Material Transfer for Manufacture"
			)
		self.assertEqual(flt(sle.stock_value), 0)
		self.assertEqual(flt(sle.stock_value_difference), -PREV_VALUE)

	def test_native_mtfm_preserves_vanilla_clear(self):
		"""TEST A/H: native MTfM keeps Core empty-warehouse clear + full SVD."""
		sle = _incident_sle()
		mark_sle_running_balance_processed(sle)
		# Vanilla already cleared full prev; SED is stale partial — native must keep SVD.
		self._run_sync(sle, _sed_row(), "Material Transfer for Manufacture")
		self.assertEqual(flt(sle.stock_value), 0)
		self.assertEqual(flt(sle.stock_value_difference), -PREV_VALUE)

	def test_outgoing_positive_svd_still_i3(self):
		"""TEST F/G: I3 remains active for invalid outgoing + positive SVD."""
		sle = _incident_sle(stock_value_difference=SED_AMOUNT, stock_value=SED_AMOUNT)
		mark_sle_running_balance_processed(sle)
		with self.assertRaises(ValuationIntegrityError) as ctx:
			assert_svd_direction(sle)
		self.assertIn("I3", str(ctx.exception))

	def test_i4_still_fires_when_leftover_not_cleared(self):
		"""TEST G: I4 remains active if leftover somehow survives."""
		sle = _incident_sle(stock_value=LEFTOVER, stock_value_difference=-SED_AMOUNT)
		mark_sle_running_balance_processed(sle)
		with self.assertRaises(ValuationIntegrityError) as ctx:
			assert_zero_qty_stock_value(sle)
		self.assertIn("I4", str(ctx.exception))

	def test_restore_still_requires_vanilla_empty(self):
		"""Vanilla leftover is not silently restored (I4 must still see it)."""
		sle = _incident_sle(stock_value=LEFTOVER, stock_value_difference=-SED_AMOUNT)
		mark_sle_running_balance_processed(sle)
		restore_vanilla_zero_qty_terminal_stock_value(
			sle, vanilla_stock_value=LEFTOVER, vanilla_qty_after=0
		)
		self.assertEqual(flt(sle.stock_value), LEFTOVER)
		with self.assertRaises(ValuationIntegrityError):
			assert_zero_qty_stock_value(sle)

	def test_sabb_row_document_path_clears(self):
		"""TEST B: Serial and Batch Bundle row on document path clears terminal value."""
		sle = _incident_sle(serial_and_batch_bundle="SABB-1")
		mark_sle_running_balance_processed(sle)
		with mock.patch(
			"erpnext_extensions.iran_accounting.domain.historical_valuation_residual.prior_type_a_on_identity",
			return_value=None,
		), mock.patch(
			"erpnext_extensions.iran_accounting.domain.historical_valuation_residual._quantum",
			return_value=1.0,
		):
			self._run_sync(
				sle,
				_sed_row(serial_and_batch_bundle="SABB-1", set_basic_rate_manually=1),
				"Material Transfer for Manufacture",
			)
		self.assertEqual(flt(sle.stock_value), 0)
		self.assertEqual(flt(sle.stock_value_difference), -PREV_VALUE)


class TestZeroQtyIntegrationV5550(unittest.TestCase):
	"""Live submit/RIV regression for residual consume-to-zero."""

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
		item = ensure_test_item(self.company, f"I550-{tag}")
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

	def test_a_mtfm_with_previous_residual_submits_clean(self):
		"""TEST A: MTfM consuming last qty with prior residual → sv=0, no I4."""
		item = self._item("MTFM")
		qty = 5
		rate = 100_000
		residual = 58_607_128
		self._submit_se(
			purpose="Material Receipt", item=item, qty=qty, rate=rate, target=self.source
		)
		self._inject_residual(item, residual)
		se = self._submit_se(
			purpose="Material Transfer for Manufacture",
			item=item,
			qty=qty,
			source=self.source,
			target=self.target,
		)
		self.assertEqual(se.docstatus, 1)
		src = [s for s in _sles(se.name, self.source) if flt(s.actual_qty) < 0][0]
		self.assertEqual(flt(src.qty_after_transaction), 0)
		self.assertEqual(flt(src.stock_value), 0)
		probe = frappe._dict(
			{**src, "company": self.company, "valuation_method": "Moving Average"}
		)
		mark_sle_running_balance_processed(probe)
		assert_zero_qty_stock_value(probe)

	def test_b_batch_mtfm_with_residual(self):
		"""TEST B: MTfM + batch / SABB fields with residual → sv=0."""
		item = self._item("BMTFM", batch=True)
		batch = make_valid_batch(item)
		qty = 3
		rate = 50_000
		residual = 1_000_000
		self._submit_se(
			purpose="Material Receipt",
			item=item,
			qty=qty,
			rate=rate,
			target=self.source,
			batch_no=batch,
		)
		self._inject_residual(item, residual)
		se = self._submit_se(
			purpose="Material Transfer for Manufacture",
			item=item,
			qty=qty,
			source=self.source,
			target=self.target,
			batch_no=batch,
		)
		src = [s for s in _sles(se.name, self.source) if flt(s.actual_qty) < 0][0]
		self.assertEqual(flt(src.qty_after_transaction), 0)
		self.assertEqual(flt(src.stock_value), 0)

	def test_d_e_transfer_submit_vs_riv_consistent(self):
		"""TEST D/E: consume-to-zero submit and RIV both leave empty warehouse."""
		item = self._item("RIV")
		qty = 2
		rate = 376_000
		residual = 10_000
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
		self.assertIsNotNone(riv)
		riv.reload()
		self.assertNotIn(riv.status, ("Failed", "Skipped"))
		src_after = [s for s in _sles(se.name, self.source) if flt(s.actual_qty) < 0][0]
		self.assertEqual(flt(src_after.qty_after_transaction), 0)
		self.assertEqual(flt(src_after.stock_value), 0)

	def test_h_normal_receipt_unaffected(self):
		"""TEST H: normal Material Receipt still posts positive stock_value."""
		item = self._item("NRM")
		se = self._submit_se(
			purpose="Material Receipt", item=item, qty=4, rate=25_000, target=self.source
		)
		tgt = [s for s in _sles(se.name, self.source) if flt(s.actual_qty) > 0][0]
		self.assertEqual(flt(tgt.stock_value), 100_000)
		self.assertEqual(flt(tgt.stock_value_difference), 100_000)

	def test_f_outgoing_issue_no_positive_svd(self):
		"""TEST F: Material Issue outgoing SVD is never positive."""
		item = self._item("ISS")
		self._submit_se(
			purpose="Material Receipt", item=item, qty=2, rate=40_000, target=self.source
		)
		se = self._submit_se(
			purpose="Material Issue", item=item, qty=1, source=self.source
		)
		src = [s for s in _sles(se.name, self.source) if flt(s.actual_qty) < 0][0]
		self.assertLessEqual(flt(src.stock_value_difference), 0)
		assert_svd_direction(
			frappe._dict({**src, "company": self.company}),
		)

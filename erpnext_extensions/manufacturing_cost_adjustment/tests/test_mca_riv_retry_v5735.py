# Copyright (c) 2026, ERPNext Extensions contributors
"""RIV retry / completion control unit+integration tests — v5.7.35."""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from erpnext_extensions.manufacturing_cost_adjustment import constants as C
from erpnext_extensions.manufacturing_cost_adjustment.repost_engine import (
	_monitor_riv_and_finalize,
	process_pending_mca_reposts,
)


class TestMCARIVRetryV5735(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		frappe.flags.in_test = True

	def _stub_mca(self, **kwargs):
		name = frappe.generate_hash(length=10)
		# Insert minimal row via SQL-less new_doc then db_insert fields
		doc = frappe.get_doc(
			{
				"doctype": C.MCA_DOCTYPE,
				"naming_series": "MCA-.YYYY.-",
				"company": frappe.db.get_single_value("Global Defaults", "default_company")
				or frappe.db.get_value("Company", {}, "name"),
				"stock_entry": kwargs.get("stock_entry") or "STE-DOES-NOT-MATTER",
				"posting_date": frappe.utils.nowdate(),
				"processing_status": kwargs.get("processing_status", C.PS_PENDING_RIV),
				"repost_required": 1,
				"repost_status": C.RS_QUEUED,
				"checkpoint": C.CP_RIV_QUEUED,
				"idempotency_key": f"test-{name}",
				"costs": [
					{
						"expense_account": frappe.db.get_value("Account", {"is_group": 0}, "name"),
						"amount": 1,
						"exchange_rate": 1,
						"description": "stub",
					}
				],
			}
		)
		# May fail validate SE — insert with ignore
		doc.flags.ignore_validate = True
		doc.flags.ignore_mandatory = True
		try:
			doc.insert(ignore_permissions=True, ignore_mandatory=True)
		except Exception:
			# Force insert parent only for monitor tests
			frappe.db.sql(
				f"""
				INSERT INTO `tab{C.MCA_DOCTYPE}`
				(name, creation, modified, modified_by, owner, docstatus,
				 company, stock_entry, posting_date, processing_status, checkpoint,
				 repost_required, repost_status, riv_voucher_nos, idempotency_key)
				VALUES (%s, NOW(), NOW(), %s, %s, 1, %s, %s, %s, %s, %s, 1, %s, %s, %s)
				""",
				(
					name,
					"Administrator",
					"Administrator",
					doc.company,
					kwargs.get("stock_entry") or "STE-X",
					frappe.utils.nowdate(),
					kwargs.get("processing_status", C.PS_PENDING_RIV),
					C.CP_RIV_QUEUED,
					kwargs.get("repost_status", C.RS_QUEUED),
					kwargs.get("riv_voucher_nos", ""),
					f"test-{name}",
				),
			)
			return name
		frappe.db.set_value(
			C.MCA_DOCTYPE,
			doc.name,
			{
				"docstatus": 1,
				"processing_status": kwargs.get("processing_status", C.PS_PENDING_RIV),
				"checkpoint": C.CP_RIV_QUEUED,
				"repost_required": 1,
				"repost_status": kwargs.get("repost_status", C.RS_QUEUED),
				"riv_voucher_nos": kwargs.get("riv_voucher_nos", ""),
			},
			update_modified=False,
		)
		return doc.name

	def test_pending_while_riv_in_progress(self):
		if not frappe.db.exists("DocType", C.MCA_DOCTYPE):
			self.skipTest("MCA DocType not migrated")
		riv = "RIV-TEST-INPROG"
		# fake RIV status via mock
		mca = self._stub_mca(riv_voucher_nos=riv)
		with patch(
			"frappe.db.get_value",
			side_effect=lambda *a, **k: self._gv(a, k, riv, "In Progress", mca),
		):
			# Too fragile — use targeted patch on monitor internals
			pass

		with patch(
			"erpnext_extensions.manufacturing_cost_adjustment.repost_engine.frappe.db.get_value"
		) as gv, patch(
			"erpnext_extensions.manufacturing_cost_adjustment.repost_engine.frappe.db.exists",
			return_value=True,
		), patch(
			"erpnext_extensions.manufacturing_cost_adjustment.repost_engine._schedule_retry"
		) as sched, patch(
			"erpnext_extensions.manufacturing_cost_adjustment.repost_engine._set_status"
		):
			def _get_value(doctype, name, field=None, **kwargs):
				if doctype == "Repost Item Valuation":
					return "In Progress"
				if doctype == C.MCA_DOCTYPE and field == "riv_voucher_nos":
					return riv
				if doctype == C.MCA_DOCTYPE:
					# get_doc path won't be used
					return None
				return None

			gv.side_effect = _get_value
			# Build real doc-like via get_doc if possible
			try:
				doc = frappe.get_doc(C.MCA_DOCTYPE, mca)
			except Exception:
				self.skipTest("stub MCA unavailable")
			doc.riv_voucher_nos = riv
			with patch(
				"erpnext_extensions.manufacturing_cost_adjustment.repost_engine.frappe.get_doc",
				return_value=doc,
			):
				result = _monitor_riv_and_finalize(mca)
			self.assertEqual(result.get("reason"), "riv_pending")
			sched.assert_called()

	def test_completed_clears_repost_required(self):
		if not frappe.db.exists("DocType", C.MCA_DOCTYPE):
			self.skipTest("MCA DocType not migrated")
		riv = "RIV-TEST-DONE"
		mca = self._stub_mca(riv_voucher_nos=riv)
		try:
			doc = frappe.get_doc(C.MCA_DOCTYPE, mca)
		except Exception:
			self.skipTest("stub MCA unavailable")
		doc.riv_voucher_nos = riv
		doc.stock_entry = frappe.db.get_value("Stock Entry", {"docstatus": 1, "purpose": "Manufacture"}, "name") or doc.stock_entry

		with patch(
			"erpnext_extensions.manufacturing_cost_adjustment.repost_engine.frappe.db.get_value"
		) as gv, patch(
			"erpnext_extensions.manufacturing_cost_adjustment.repost_engine.frappe.db.exists",
			return_value=True,
		), patch(
			"erpnext_extensions.manufacturing_cost_adjustment.repost_engine._final_reconcile"
		), patch(
			"erpnext_extensions.manufacturing_cost_adjustment.repost_engine.release_se_lock"
		), patch(
			"erpnext_extensions.manufacturing_cost_adjustment.repost_engine.frappe.get_doc",
			return_value=doc,
		), patch(
			"erpnext_extensions.manufacturing_cost_adjustment.repost_engine.frappe.db.set_value"
		) as sv, patch(
			"erpnext_extensions.manufacturing_cost_adjustment.repost_engine.frappe.db.commit"
		):

			def _get_value(doctype, name, field=None, **kwargs):
				if doctype == "Repost Item Valuation":
					return "Completed"
				return None

			gv.side_effect = _get_value
			result = _monitor_riv_and_finalize(mca)
			self.assertTrue(result.get("ok"))
			# set_value called with Completed
			args_list = [c.args for c in sv.call_args_list]
			found = False
			for a in args_list:
				if len(a) >= 3 and isinstance(a[2], dict) and a[2].get("processing_status") == C.PS_COMPLETED:
					self.assertEqual(a[2].get("repost_required"), 0)
					self.assertEqual(a[2].get("repost_status"), C.RS_COMPLETED)
					found = True
			self.assertTrue(found)

	def _gv(self, a, k, riv, status, mca):
		return status

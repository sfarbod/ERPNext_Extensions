from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.exceptions import ValidationError

import erpnext_extensions.cheque_management.pdc_invoice_advance_application as app


class _ThrowCtx:
	def __enter__(self):
		self._p = patch.object(
			frappe,
			"throw",
			side_effect=lambda msg, *a, **k: (_ for _ in ()).throw(ValidationError(msg)),
		)
		self._p.start()
		return self

	def __exit__(self, exc_type, exc, tb):
		self._p.stop()
		return False


class TestPDCInvoiceAdvanceApplication(unittest.TestCase):
	def test_submit_posts_and_marks_rows_posted(self) -> None:
		row = SimpleNamespace(
			name="ROW-1",
			post_dated_cheque="PDC-1",
			advance_scope="order_based",
			order_doctype="Purchase Order",
			order_name="PO-1",
			amount=100.0,
			amount_in_pdc_currency=100.0,
			fx_rate=1.0,
			application_status="draft",
			posted_je=None,
			reversal_je=None,
		)
		doc = SimpleNamespace(
			doctype="Purchase Invoice",
			name="PINV-1",
			company="_TC",
			posting_date="2026-04-01",
			currency="INR",
			supplier="SUP-1",
			credit_to="ACC-AP",
			items=[SimpleNamespace(purchase_order="PO-1")],
			pdc_invoice_applications=[row],
		)

		fake_db = SimpleNamespace(
			get_value=lambda dt, nm, field: "ACC-ADV-PAID"
			if field == "default_advance_paid_account"
			else None,
			set_value=lambda *a, **k: None,
			sql=lambda *a, **k: [],
		)
		fake_je = SimpleNamespace(
			flags=SimpleNamespace(), append=lambda *a, **k: None, submit=lambda: None, name="JV-1"
		)
		fake_frappe = SimpleNamespace(
			db=fake_db,
			new_doc=lambda dt: fake_je,
			utils=SimpleNamespace(today=lambda: "2026-04-01"),
		)

		with _ThrowCtx(), patch.object(app, "frappe", fake_frappe), patch.object(app, "_", lambda s: s):
			app.on_invoice_submit(doc)

		self.assertEqual(row.application_status, "posted")
		self.assertEqual(row.posted_je, "JV-1")

	def test_submit_general_row_without_order_link_does_not_require_order(self) -> None:
		row = SimpleNamespace(
			name="ROW-G1",
			post_dated_cheque="PDC-1",
			advance_scope="general",
			order_doctype="",
			order_name="",
			amount=100.0,
			amount_in_pdc_currency=100.0,
			fx_rate=1.0,
			application_status="draft",
			posted_je=None,
			reversal_je=None,
			source_bucket_label="",
		)
		doc = SimpleNamespace(
			doctype="Sales Invoice",
			name="SINV-1",
			company="_TC",
			posting_date="2026-04-01",
			currency="INR",
			customer="CUST-1",
			debit_to="ACC-AR",
			# No sales_order link
			items=[],
			pdc_invoice_applications=[row],
		)

		fake_db = SimpleNamespace(
			get_value=lambda dt, nm, field: "ACC-ADV-REC"
			if field == "default_advance_received_account"
			else None,
			set_value=lambda *a, **k: None,
			sql=lambda *a, **k: [],
		)
		fake_je = SimpleNamespace(
			flags=SimpleNamespace(), append=lambda *a, **k: None, submit=lambda: None, name="JV-1"
		)
		fake_frappe = SimpleNamespace(
			db=fake_db,
			new_doc=lambda dt: fake_je,
			utils=SimpleNamespace(today=lambda: "2026-04-01"),
		)

		with _ThrowCtx(), patch.object(app, "frappe", fake_frappe), patch.object(app, "_", lambda s: s):
			app.on_invoice_submit(doc)

		self.assertEqual(row.application_status, "posted")
		self.assertEqual(row.posted_je, "JV-1")
		self.assertEqual(row.order_doctype, "")
		self.assertEqual(row.order_name, "")

	def test_cancel_reverses_rows(self) -> None:
		row = SimpleNamespace(
			name="ROW-CANCEL-1",
			post_dated_cheque="PDC-1",
			order_doctype="Sales Order",
			order_name="SO-1",
			amount=50.0,
			amount_in_pdc_currency=50.0,
			fx_rate=1.0,
			application_status="posted",
			posted_je="JV-1",
			reversal_je=None,
		)
		doc = SimpleNamespace(
			doctype="Sales Invoice",
			name="SINV-1",
			company="_TC",
			posting_date="2026-04-01",
			currency="INR",
			customer="CUST-1",
			debit_to="ACC-AR",
			items=[SimpleNamespace(sales_order="SO-1")],
			pdc_invoice_applications=[row],
		)

		fake_db = SimpleNamespace(
			get_value=lambda dt, nm, field: "ACC-ADV-REC"
			if field == "default_advance_received_account"
			else None,
			set_value=lambda *a, **k: None,
			sql=lambda *a, **k: [],
		)
		fake_je = SimpleNamespace(
			flags=SimpleNamespace(), append=lambda *a, **k: None, submit=lambda: None, name="JV-REV"
		)
		fake_frappe = SimpleNamespace(
			db=fake_db,
			new_doc=lambda dt: fake_je,
			utils=SimpleNamespace(today=lambda: "2026-04-01"),
		)
		with _ThrowCtx(), patch.object(app, "frappe", fake_frappe), patch.object(app, "_", lambda s: s):
			app.on_invoice_cancel(doc)
		self.assertEqual(row.application_status, "reversed")
		self.assertEqual(row.reversal_je, "JV-REV")

	def test_cancel_reuses_existing_reversal_je_by_idempotency_key(self) -> None:
		row = SimpleNamespace(
			name="ROW-1",
			post_dated_cheque="PDC-1",
			advance_scope="general",
			order_doctype="",
			order_name="",
			amount=50.0,
			amount_in_pdc_currency=50.0,
			fx_rate=1.0,
			application_status="posted",
			posted_je="JV-APPLY",
			reversal_je=None,
		)
		doc = SimpleNamespace(
			doctype="Sales Invoice",
			name="SINV-1",
			company="_TC",
			posting_date="2026-04-01",
			currency="INR",
			customer="CUST-1",
			debit_to="ACC-AR",
			items=[],
			pdc_invoice_applications=[row],
		)

		def _sql(query, params=None, as_dict=False):
			q = " ".join((query or "").split())
			if "FROM `tabJournal Entry`" in q:
				return [{"name": "JV-REV-EXIST"}]
			if "FROM `tabJournal Entry Account`" in q:
				# validation fallback: net movement on eligible advance account(s)
				# v5.7.42 uses IN (...); older single-account path used account =
				if "AND account IN" in q or "AND account =" in q:
					return [{"dr": 50.0, "cr": 0.0}]
				return [{"ref_amt": 0.0}]
			return []

		calls = []

		def _set_value(doctype, name, values, update_modified=False):
			calls.append((doctype, name, values, update_modified))

		fake_db = SimpleNamespace(
			get_value=lambda dt, nm, field: "ACC-ADV-REC"
			if field == "default_advance_received_account"
			else None,
			set_value=_set_value,
			sql=_sql,
		)
		fake_frappe = SimpleNamespace(
			db=fake_db,
			new_doc=lambda dt: (_ for _ in ()).throw(
				AssertionError("Should not create new JE when existing reversal exists")
			),
			utils=SimpleNamespace(today=lambda: "2026-04-01"),
		)

		with _ThrowCtx(), patch.object(app, "frappe", fake_frappe), patch.object(app, "_", lambda s: s):
			app.on_invoice_cancel(doc)

		self.assertEqual(row.application_status, "reversed")
		self.assertEqual(row.reversal_je, "JV-REV-EXIST")
		self.assertTrue(calls)

	def test_submit_persists_child_status_via_db_set_value(self) -> None:
		calls = []
		row = SimpleNamespace(
			name="ROW-1",
			post_dated_cheque="PDC-1",
			advance_scope="order_based",
			order_doctype="Purchase Order",
			order_name="PO-1",
			amount=100.0,
			amount_in_pdc_currency=100.0,
			fx_rate=1.0,
			application_status="draft",
			posted_je=None,
			reversal_je=None,
		)
		doc = SimpleNamespace(
			doctype="Purchase Invoice",
			name="PINV-1",
			company="_TC",
			posting_date="2026-04-01",
			currency="INR",
			supplier="SUP-1",
			credit_to="ACC-AP",
			items=[SimpleNamespace(purchase_order="PO-1")],
			pdc_invoice_applications=[row],
		)

		def _set_value(doctype, name, values, update_modified=False):
			calls.append((doctype, name, values, update_modified))

		fake_db = SimpleNamespace(
			get_value=lambda dt, nm, field: "ACC-ADV-PAID"
			if field == "default_advance_paid_account"
			else None,
			set_value=_set_value,
			sql=lambda *a, **k: [],
		)
		fake_je = SimpleNamespace(
			flags=SimpleNamespace(), append=lambda *a, **k: None, submit=lambda: None, name="JV-1"
		)
		fake_frappe = SimpleNamespace(
			db=fake_db,
			new_doc=lambda dt: fake_je,
			utils=SimpleNamespace(today=lambda: "2026-04-01"),
		)

		with _ThrowCtx(), patch.object(app, "frappe", fake_frappe), patch.object(app, "_", lambda s: s):
			app.on_invoice_submit(doc)

		self.assertTrue(calls)
		self.assertEqual(calls[0][0], "PDC Invoice Application")
		self.assertEqual(calls[0][1], "ROW-1")
		self.assertEqual(calls[0][2]["application_status"], "posted")

	def test_submit_reuses_existing_application_je_by_idempotency_marker(self) -> None:
		calls = []
		row = SimpleNamespace(
			name="ROW-1",
			post_dated_cheque="PDC-1",
			advance_scope="order_based",
			order_doctype="Purchase Order",
			order_name="PO-1",
			amount=100.0,
			amount_in_pdc_currency=100.0,
			fx_rate=1.0,
			application_status="draft",
			posted_je=None,
			reversal_je=None,
		)
		doc = SimpleNamespace(
			doctype="Purchase Invoice",
			name="PINV-1",
			company="_TC",
			posting_date="2026-04-01",
			currency="INR",
			supplier="SUP-1",
			credit_to="ACC-AP",
			items=[SimpleNamespace(purchase_order="PO-1")],
			pdc_invoice_applications=[row],
		)

		def _sql(query, params=None, as_dict=False):
			q = " ".join((query or "").split())
			if "FROM `tabJournal Entry`" in q:
				return [{"name": "JV-EXIST"}]
			if "FROM `tabJournal Entry Account`" in q:
				return [{"ref_amt": 100.0}]
			return []

		def _set_value(doctype, name, values, update_modified=False):
			calls.append((doctype, name, values, update_modified))

		fake_db = SimpleNamespace(
			get_value=lambda dt, nm, field: "ACC-ADV-PAID"
			if field == "default_advance_paid_account"
			else None,
			set_value=_set_value,
			sql=_sql,
		)
		fake_frappe = SimpleNamespace(
			db=fake_db,
			new_doc=lambda dt: (_ for _ in ()).throw(
				AssertionError("Should not create new JE when existing exists")
			),
			utils=SimpleNamespace(today=lambda: "2026-04-01"),
		)

		with _ThrowCtx(), patch.object(app, "frappe", fake_frappe), patch.object(app, "_", lambda s: s):
			app.on_invoice_submit(doc)

		self.assertEqual(row.application_status, "posted")
		self.assertEqual(row.posted_je, "JV-EXIST")
		self.assertTrue(calls)

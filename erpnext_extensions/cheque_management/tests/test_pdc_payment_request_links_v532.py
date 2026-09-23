# Copyright (c) 2026, Farbod Siyahpoosh and contributors
# For license information, please see license.txt

"""v5.3.1 — Payment Request dashboard links to related Post Dated Cheques.

Covers count/list identity (A–G) without mutating settlement logic.

Run::

    bench --site development.localhost run-tests --module \\
        erpnext_extensions.cheque_management.tests.test_pdc_payment_request_links_v532
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import frappe

import erpnext_extensions.cheque_management.pdc_payment_request_links as links
from erpnext_extensions.cheque_management.payment_request_dashboard import get_data as pr_dashboard_get_data


class TestGetPdcNamesForPaymentRequest(unittest.TestCase):
	"""Unit A–G against the link query helper (mocked DB)."""

	def test_a_one_related_pdc(self):
		fake = MagicMock()
		fake.db.sql = MagicMock(return_value=[("PDC-1",)])
		with patch.object(links, "frappe", fake):
			names = links.get_post_dated_cheque_names_for_payment_request("PR-A")
			self.assertEqual(names, ["PDC-1"])
			self.assertEqual(links.count_post_dated_cheques_for_payment_request("PR-A"), 1)

	def test_b_multiple_pdcs_same_count_and_names(self):
		fake = MagicMock()
		fake.db.sql = MagicMock(
			side_effect=[
				[("PDC-1",), ("PDC-2",), ("PDC-3",)],
				[[3]],
			]
		)
		with patch.object(links, "frappe", fake):
			names = links.get_post_dated_cheque_names_for_payment_request("PR-A")
			# rebuild payload with a fresh names+open sequence
			fake.db.sql = MagicMock(
				side_effect=[
					[("PDC-1",), ("PDC-2",), ("PDC-3",)],
					[[3]],
				]
			)
			payload = links.build_pdc_internal_link_payload("PR-A")
		self.assertEqual(names, ["PDC-1", "PDC-2", "PDC-3"])
		self.assertEqual(payload["count"], 3)
		self.assertEqual(payload["names"], ["PDC-1", "PDC-2", "PDC-3"])
		self.assertEqual(payload["open_count"], 3)

	def test_c_empty_for_unrelated_pr(self):
		fake = MagicMock()
		fake.db.sql = MagicMock(return_value=[])
		with patch.object(links, "frappe", fake):
			self.assertEqual(links.get_post_dated_cheque_names_for_payment_request("PR-B"), [])
			self.assertEqual(links.count_post_dated_cheques_for_payment_request("PR-B"), 0)

	def test_d_registered_included_in_query(self):
		"""Query has no docstatus filter — Registered PDCs remain discoverable."""
		fake = MagicMock()
		fake.db.sql = MagicMock(return_value=[("PDC-REG",)])
		with patch.object(links, "frappe", fake):
			names = links.get_post_dated_cheque_names_for_payment_request("PR-A")
		self.assertEqual(names, ["PDC-REG"])
		sql = fake.db.sql.call_args[0][0]
		self.assertNotIn("p.docstatus = 1", sql)
		self.assertIn("tabPDC Allocation", sql)
		self.assertIn("reference_doctype", sql)

	def test_e_settled_still_discoverable(self):
		"""No Register-JE exclusion — settled PDCs stay in the historical set."""
		fake = MagicMock()
		fake.db.sql = MagicMock(return_value=[("PDC-SETTLED",)])
		with patch.object(links, "frappe", fake):
			names = links.get_post_dated_cheque_names_for_payment_request("PR-A")
		self.assertEqual(names, ["PDC-SETTLED"])
		sql = fake.db.sql.call_args[0][0].lower()
		self.assertNotIn("pdc journal reference", sql)
		self.assertNotIn("not exists", sql)

	def test_f_cancelled_included_for_traceability(self):
		"""Cancelled PDCs remain in the name list (open badge excludes them separately)."""
		fake = MagicMock()
		# First sql: names query; second: open_count among names
		fake.db.sql = MagicMock(side_effect=[[("PDC-CAN",), ("PDC-OK",)], [[1]]])
		with patch.object(links, "frappe", fake):
			payload = links.build_pdc_internal_link_payload("PR-A")
		self.assertEqual(payload["count"], 2)
		self.assertIn("PDC-CAN", payload["names"])
		self.assertEqual(payload["open_count"], 1)

	def test_g_no_pdc_zero_payload(self):
		fake = MagicMock()
		fake.db.sql = MagicMock(return_value=[])
		with patch.object(links, "frappe", fake):
			payload = links.build_pdc_internal_link_payload("PR-EMPTY")
		self.assertEqual(payload["count"], 0)
		self.assertEqual(payload["names"], [])
		self.assertEqual(payload["open_count"], 0)
		self.assertEqual(payload["doctype"], "Post Dated Cheque")

	def test_blank_pr_returns_empty_without_sql(self):
		fake = MagicMock()
		fake.db.sql = MagicMock()
		with patch.object(links, "frappe", fake):
			self.assertEqual(links.get_post_dated_cheque_names_for_payment_request(""), [])
			self.assertEqual(links.get_post_dated_cheque_names_for_payment_request(None), [])
		fake.db.sql.assert_not_called()


class TestPaymentRequestOpenCountInjection(unittest.TestCase):
	def test_injects_pdc_as_internal_link_and_strips_external(self):
		core = {
			"count": {
				"external_links_found": [
					{"doctype": "Payment Entry", "count": 1, "open_count": 0},
					{"doctype": "Post Dated Cheque", "count": 99, "open_count": 0},
				],
				"internal_links_found": [],
			}
		}
		with (
			patch.object(links, "get_open_count", create=True),
			patch(
				"frappe.desk.notifications.get_open_count",
				return_value=core,
			),
			patch.object(
				links,
				"build_pdc_internal_link_payload",
				return_value={
					"doctype": "Post Dated Cheque",
					"count": 2,
					"open_count": 1,
					"names": ["PDC-1", "PDC-2"],
				},
			),
		):
			out = links.get_payment_request_open_count(
				"Payment Request",
				"PR-A",
				["Payment Entry", "Post Dated Cheque", "Payment Order"],
			)
		ext = out["count"]["external_links_found"]
		intl = out["count"]["internal_links_found"]
		self.assertTrue(all(r["doctype"] != "Post Dated Cheque" for r in ext))
		pdc_rows = [r for r in intl if r["doctype"] == "Post Dated Cheque"]
		self.assertEqual(len(pdc_rows), 1)
		self.assertEqual(pdc_rows[0]["count"], 2)
		self.assertEqual(pdc_rows[0]["names"], ["PDC-1", "PDC-2"])

	def test_non_payment_request_delegates(self):
		with patch(
			"frappe.desk.notifications.get_open_count",
			return_value={"count": {"external_links_found": [], "internal_links_found": []}},
		) as core:
			links.get_payment_request_open_count("Sales Invoice", "SINV-1", None)
		core.assert_called_once()


class TestPaymentRequestDashboardGetData(unittest.TestCase):
	def test_adds_pdc_under_payment_and_sets_method(self):
		base = {
			"fieldname": "payment_request",
			"transactions": [
				{"label": "Payment", "items": ["Payment Entry", "Payment Order"]},
			],
		}
		data = pr_dashboard_get_data(base)
		self.assertIn(
			"pdc_payment_request_links.get_payment_request_open_count",
			data.method,
		)
		payment = data.transactions[0]
		self.assertEqual(payment["items"], ["Payment Entry", "Post Dated Cheque", "Payment Order"])


class TestPdcPaymentRequestLinksIntegration(unittest.TestCase):
	"""DB-backed checks when run via bench --site."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		try:
			frappe.db.exists("DocType", "Post Dated Cheque")
		except Exception:
			raise unittest.SkipTest("No Frappe DB") from None
		if not frappe.db.exists("DocType", "Post Dated Cheque"):
			raise unittest.SkipTest("Post Dated Cheque not installed")
		if not frappe.db.exists("DocType", "Payment Request"):
			raise unittest.SkipTest("Payment Request not installed")
		frappe.set_user("Administrator")

	def test_live_query_matches_payload_for_known_pr(self):
		row = frappe.db.sql(
			"""
			select reference_name
			from `tabPDC Allocation`
			where reference_doctype = 'Payment Request' and ifnull(reference_name, '') != ''
			limit 1
			"""
		)
		if not row:
			row = frappe.db.sql(
				"""
				select reference_name from `tabPost Dated Cheque`
				where reference_doctype = 'Payment Request' and ifnull(reference_name, '') != ''
				limit 1
				"""
			)
		if not row:
			self.skipTest("No Payment Request–linked PDC on site")
		pr = row[0][0]
		names = links.get_post_dated_cheque_names_for_payment_request(pr)
		payload = links.build_pdc_internal_link_payload(pr)
		self.assertEqual(payload["names"], names)
		self.assertEqual(payload["count"], len(names))
		self.assertGreaterEqual(len(names), 1)

	def test_dashboard_hook_resolves(self):
		meta = frappe.get_meta("Payment Request")
		data = meta.get_dashboard_data()
		self.assertIn("Post Dated Cheque", str(data.get("transactions")))
		self.assertIn("get_payment_request_open_count", data.get("method") or "")

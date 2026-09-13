# Copyright (c) 2026, ERPNext Extensions contributors
"""JSON error transport: diagnostic BrokenPipe/EPIPE must not become Werkzeug HTML 500."""

from __future__ import annotations

import errno
import json
import unittest
from unittest.mock import patch

import frappe
from erpnext.stock.doctype.serial_and_batch_bundle.serial_and_batch_bundle import (
	BatchNegativeStockError,
)
from erpnext.stock.stock_ledger import NegativeStockError
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request

from erpnext_extensions.iran_accounting.integration.bootstrap import apply as apply_bootstrap
from erpnext_extensions.safe_error_transport import apply_safe_error_transport


def _as_json_api_request():
	builder = EnvironBuilder(
		method="POST",
		path="/api/method/frappe.model.workflow.apply_workflow",
		headers={
			"Accept": "application/json",
			"Host": "development.localhost",
			"X-Requested-With": "XMLHttpRequest",
		},
	)
	frappe.local.request = Request(builder.get_environ())
	frappe.local.is_ajax = True
	frappe.local.response = frappe._dict({"docs": []})
	frappe.local.message_log = []
	frappe.local.error_log = []
	if not getattr(frappe.session, "user", None):
		frappe.set_user("Administrator")


def _handle(exc):
	from frappe.app import handle_exception

	return handle_exception(exc)


def _response_json(response):
	raw = response.get_data(as_text=True)
	self_check = raw.lstrip()[:1]
	if self_check == "<":
		raise AssertionError(f"response began with HTML: {raw[:180]!r}")
	return json.loads(raw), raw


class TestSafeErrorTransport(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		apply_bootstrap()
		apply_safe_error_transport()
		apply_safe_error_transport()

	def test_patch_is_idempotent_no_stacking(self):
		inner = frappe.errprint
		self.assertTrue(getattr(inner, "_ee_safe_broken_pipe", False))
		apply_safe_error_transport()
		self.assertIs(frappe.errprint, inner)
		self.assertFalse(getattr(inner._ee_original, "_ee_safe_broken_pipe", False))

	def test_errprint_swallows_broken_pipe_and_keeps_error_log(self):
		frappe.local.error_log = []
		with patch("builtins.print", side_effect=BrokenPipeError(32, "Broken pipe")):
			frappe.errprint("stock validation traceback")
		self.assertTrue(frappe.local.error_log)
		self.assertIn("stock validation traceback", frappe.local.error_log[-1]["exc"])

	def test_errprint_swallows_oserror_epipe(self):
		frappe.local.error_log = []
		with patch("builtins.print", side_effect=OSError(errno.EPIPE, "Broken pipe")):
			frappe.errprint("epipe traceback")
		self.assertIn("epipe traceback", frappe.local.error_log[-1]["exc"])

	def test_errprint_does_not_swallow_unrelated_oserror(self):
		with patch("builtins.print", side_effect=OSError(errno.ENOSPC, "No space left")):
			with self.assertRaises(OSError) as ctx:
				frappe.errprint("disk full traceback")
		self.assertEqual(ctx.exception.errno, errno.ENOSPC)

	def test_validation_error_snapshot_is_skipped(self):
		import frappe.utils.error as error_mod

		with patch("frappe.utils.error.log_error") as log_error:
			error_mod.log_error_snapshot(frappe.ValidationError("no"))
			log_error.assert_not_called()

	def test_batch_negative_stock_snapshot_is_skipped(self):
		import frappe.utils.error as error_mod

		with patch("frappe.utils.error.log_error") as log_error:
			error_mod.log_error_snapshot(BatchNegativeStockError("batch short"))
			log_error.assert_not_called()

	def test_negative_stock_snapshot_is_skipped(self):
		import frappe.utils.error as error_mod

		with patch("frappe.utils.error.log_error") as log_error:
			error_mod.log_error_snapshot(NegativeStockError("warehouse short"))
			log_error.assert_not_called()

	def test_non_validation_snapshot_still_delegates(self):
		import frappe.utils.error as error_mod

		with patch("frappe.utils.error.log_error") as log_error:
			error_mod.log_error_snapshot(RuntimeError("boom"))
			log_error.assert_called()

	def test_permission_error_snapshot_still_delegates(self):
		import frappe.utils.error as error_mod

		with patch("frappe.utils.error.log_error") as log_error:
			error_mod.log_error_snapshot(frappe.PermissionError("nope"))
			log_error.assert_called()

	def _assert_json_exc(self, exc, *, status, exc_type, broken_pipe=False):
		_as_json_api_request()
		side = BrokenPipeError(32, "Broken pipe") if broken_pipe else None
		print_patch = (
			patch("builtins.print", side_effect=side) if broken_pipe else patch("builtins.print")
		)
		try:
			frappe.throw(str(exc) or exc.__class__.__name__, exc.__class__)
		except Exception as raised:
			self.assertIsInstance(raised, type(exc))
			with print_patch:
				response = _handle(raised)
		body, raw = _response_json(response)
		self.assertEqual(response.status_code, status)
		self.assertIn("application/json", response.mimetype or "")
		self.assertEqual(body.get("exc_type"), exc_type)
		self.assertNotEqual(body.get("exc_type"), "BrokenPipeError")
		self.assertTrue(raw.lstrip().startswith("{"))
		return body, response

	def test_handle_exception_validation_error_json_417(self):
		self._assert_json_exc(frappe.ValidationError("invalid"), status=417, exc_type="ValidationError")

	def test_handle_exception_batch_negative_json_417(self):
		body, _ = self._assert_json_exc(
			BatchNegativeStockError("batch 13200040 short"),
			status=417,
			exc_type="BatchNegativeStockError",
		)
		self.assertTrue(body.get("_server_messages") or body.get("exception"))

	def test_handle_exception_negative_stock_json_417(self):
		self._assert_json_exc(
			NegativeStockError("warehouse short"),
			status=417,
			exc_type="NegativeStockError",
		)

	def test_broken_pipe_does_not_replace_batch_negative_stock_error(self):
		body, response = self._assert_json_exc(
			BatchNegativeStockError("batch short"),
			status=417,
			exc_type="BatchNegativeStockError",
			broken_pipe=True,
		)
		self.assertEqual(response.status_code, 417)
		self.assertNotIn("BrokenPipeError", json.dumps(body))

	def test_epipe_does_not_replace_validation_error(self):
		_as_json_api_request()
		try:
			frappe.throw("invalid qty", frappe.ValidationError)
		except frappe.ValidationError as raised:
			with patch("builtins.print", side_effect=OSError(errno.EPIPE, "Broken pipe")):
				response = _handle(raised)
		body, raw = _response_json(response)
		self.assertEqual(response.status_code, 417)
		self.assertEqual(body.get("exc_type"), "ValidationError")
		self.assertTrue(raw.lstrip().startswith("{"))

	def test_handle_exception_permission_error_json_403(self):
		self._assert_json_exc(
			frappe.PermissionError("not permitted"),
			status=403,
			exc_type="PermissionError",
		)

	def test_handle_exception_authentication_error_json_401(self):
		self._assert_json_exc(
			frappe.AuthenticationError("bad credentials"),
			status=401,
			exc_type="AuthenticationError",
		)

	def test_unexpected_runtime_error_keeps_exc_type_json_500(self):
		_as_json_api_request()
		try:
			raise RuntimeError("unexpected boom")
		except RuntimeError as raised:
			with patch("frappe.utils.error.log_error"):
				response = _handle(raised)
		body, raw = _response_json(response)
		self.assertEqual(response.status_code, 500)
		self.assertEqual(body.get("exc_type"), "RuntimeError")
		self.assertNotEqual(body.get("exc_type"), "ValidationError")
		self.assertTrue(raw.lstrip().startswith("{"))

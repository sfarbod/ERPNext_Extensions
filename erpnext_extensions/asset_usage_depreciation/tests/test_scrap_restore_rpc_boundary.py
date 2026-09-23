# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

"""Regression: scrap/restore whitelist wrappers must not forward RPC ``cmd``.

Frappe Desk calls whitelisted methods via ``frappe.call(method, **form_dict)``.
``form_dict`` always includes ``cmd``. When an override wrapper uses ``**kwargs``,
``frappe.get_newargs`` passes ``cmd`` through; forwarding ``**kwargs`` to native
ERPNext ``scrap_asset`` / ``restore_asset`` then raises::

    TypeError: scrap_asset() got an unexpected keyword argument 'cmd'

These tests lock the fixed-signature compatibility boundary. Run::

    bench --site development.localhost run-tests \\
        --module erpnext_extensions.asset_usage_depreciation.tests.test_scrap_restore_rpc_boundary \\
        --skip-before-tests
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

import frappe

from erpnext_extensions.asset_usage_depreciation import integration_hooks


class TestScrapRestoreRpcBoundary(unittest.TestCase):
	def test_get_newargs_strips_cmd_for_fixed_signature_wrapper(self):
		"""Fixed signature must cause Frappe to drop RPC ``cmd`` before call."""
		form_dict = {
			"cmd": "erpnext.assets.doctype.asset.depreciation.scrap_asset",
			"asset_name": "2854",
			"scrap_date": "2026-09-23",
		}
		newargs = frappe.get_newargs(integration_hooks.scrap_asset, form_dict)
		self.assertNotIn("cmd", newargs)
		self.assertEqual(newargs.get("asset_name"), "2854")
		self.assertEqual(newargs.get("scrap_date"), "2026-09-23")

		restore_form = {
			"cmd": "erpnext.assets.doctype.asset.depreciation.restore_asset",
			"asset_name": "2854",
		}
		restore_args = frappe.get_newargs(integration_hooks.restore_asset, restore_form)
		self.assertNotIn("cmd", restore_args)
		self.assertEqual(restore_args.get("asset_name"), "2854")

	def test_rpc_style_frappe_call_does_not_forward_cmd_to_native_scrap(self):
		"""Desk/RPC path: wrapper receives filtered kwargs; native never sees cmd."""
		native_calls = []
		reapply_calls = []

		def fake_native(asset_name, scrap_date=None):
			native_calls.append({"asset_name": asset_name, "scrap_date": scrap_date})
			return "ok-scrap"

		def fake_reapply(asset_name, trigger_doc=None):
			reapply_calls.append(asset_name)

		form_dict = {
			"cmd": "erpnext.assets.doctype.asset.depreciation.scrap_asset",
			"asset_name": "2854",
			"scrap_date": "2026-09-23",
		}

		with (
			patch(
				"erpnext.assets.doctype.asset.depreciation.scrap_asset",
				side_effect=fake_native,
			),
			patch.object(integration_hooks, "_reapply_for_asset", side_effect=fake_reapply),
		):
			result = frappe.call(integration_hooks.scrap_asset, **form_dict)

		self.assertEqual(result, "ok-scrap")
		self.assertEqual(len(native_calls), 1, "native scrap must run exactly once")
		self.assertEqual(native_calls[0]["asset_name"], "2854")
		self.assertEqual(native_calls[0]["scrap_date"], "2026-09-23")
		self.assertNotIn("cmd", native_calls[0])
		self.assertEqual(reapply_calls, ["2854"])

	def test_scrap_without_cmd_still_works(self):
		"""Direct/non-RPC calls without cmd must keep working."""
		native_calls = []
		reapply_calls = []

		def fake_native(asset_name, scrap_date=None):
			native_calls.append((asset_name, scrap_date))
			return None

		with (
			patch(
				"erpnext.assets.doctype.asset.depreciation.scrap_asset",
				side_effect=fake_native,
			),
			patch.object(
				integration_hooks,
				"_reapply_for_asset",
				side_effect=lambda asset_name, trigger_doc=None: reapply_calls.append(asset_name),
			),
		):
			integration_hooks.scrap_asset("ASSET-1", "2026-01-15")

		self.assertEqual(native_calls, [("ASSET-1", "2026-01-15")])
		self.assertEqual(reapply_calls, ["ASSET-1"])

	def test_rpc_style_frappe_call_does_not_forward_cmd_to_native_restore(self):
		native_calls = []
		reapply_calls = []

		def fake_native(asset_name):
			native_calls.append({"asset_name": asset_name})
			return "ok-restore"

		form_dict = {
			"cmd": "erpnext.assets.doctype.asset.depreciation.restore_asset",
			"asset_name": "2854",
		}

		with (
			patch(
				"erpnext.assets.doctype.asset.depreciation.restore_asset",
				side_effect=fake_native,
			),
			patch.object(
				integration_hooks,
				"_reapply_for_asset",
				side_effect=lambda asset_name, trigger_doc=None: reapply_calls.append(asset_name),
			),
		):
			result = frappe.call(integration_hooks.restore_asset, **form_dict)

		self.assertEqual(result, "ok-restore")
		self.assertEqual(len(native_calls), 1)
		self.assertEqual(native_calls[0], {"asset_name": "2854"})
		self.assertEqual(reapply_calls, ["2854"])

	def test_override_whitelisted_methods_point_at_wrappers(self):
		overrides = frappe.get_hooks("override_whitelisted_methods") or {}
		# get_hooks returns list values for multi-app merge
		self.assertIn(
			"erpnext_extensions.asset_usage_depreciation.integration_hooks.scrap_asset",
			overrides.get("erpnext.assets.doctype.asset.depreciation.scrap_asset") or [],
		)
		self.assertIn(
			"erpnext_extensions.asset_usage_depreciation.integration_hooks.restore_asset",
			overrides.get("erpnext.assets.doctype.asset.depreciation.restore_asset") or [],
		)


if __name__ == "__main__":
	unittest.main()

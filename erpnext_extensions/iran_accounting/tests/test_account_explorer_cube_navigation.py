"""Wave 3B-3 Cube Navigation Model — controller / graph / URL contracts."""

from __future__ import annotations

import os
import unittest

import frappe


class TestAccountExplorerCubeNavigation(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		app_root = frappe.get_app_path("erpnext_extensions")
		base = os.path.join(app_root, "erpnext_extensions", "page", "account_explorer")
		cls.paths = {
			"page": os.path.join(base, "account_explorer.js"),
			"css": os.path.join(base, "account_explorer.css"),
			"graph": os.path.join(base, "core", "explorer_drill_graph.js"),
			"af": os.path.join(base, "core", "explorer_analysis_filters.js"),
			"af_summary": os.path.join(base, "core", "explorer_analysis_filter_summary.js"),
			"ws": os.path.join(base, "core", "explorer_workspace_state.js"),
			"events": os.path.join(base, "core", "explorer_events.js"),
			"adr": os.path.join(
				app_root, "iran_accounting", "docs", "adr", "ADR-3B-004-cube-navigation.md"
			),
		}
		cls.src = {}
		for key, path in cls.paths.items():
			with open(path, encoding="utf-8") as handle:
				cls.src[key] = handle.read()

	def test_adr_accepted(self):
		self.assertIn("Accepted", self.src["adr"])
		self.assertIn("`GROUP BY`", self.src["adr"])
		self.assertIn("analysis_filters", self.src["adr"])
		self.assertIn("ae_v=2", self.src["adr"])
		self.assertIn("Compatibility strategy", self.src["adr"])

	def test_core_modules_included(self):
		page = self.src["page"]
		self.assertIn("explorer_analysis_filters.js", page)
		self.assertIn("explorer_analysis_filter_summary.js", page)
		self.assertIn("explorer_drill_graph.js", page)
		self.assertIn("ExplorerDrillGraph.create_default", page)

	def test_drill_graph_registry(self):
		graph = self.src["graph"]
		for node in (
			"AccountGroup",
			"GeneralLedger",
			"SubsidiaryLedger",
			"DimensionValue",
			"CurrencyValue",
			"PartyValue",
			"Voucher",
			"GLDetail",
		):
			self.assertIn(node, graph)
		for name in (
			"register_node",
			"register_edge",
			"resolve",
			"get_default_intent",
			"duplicate_node",
			"duplicate_edge",
			"unknown_intent",
			"invalid_policy",
			"no_matching_edge",
			"classify_row",
			"list_intents",
		):
			self.assertIn(name, graph)

	def test_workspace_modules_split(self):
		app_root = frappe.get_app_path("erpnext_extensions")
		base = os.path.join(app_root, "erpnext_extensions", "page", "account_explorer", "core")
		for name, limit in (
			("explorer_workspace_state.js", 800),
			("explorer_workspace_codec.js", 800),
			("explorer_workspace_tokens.js", 400),
		):
			path = os.path.join(base, name)
			with open(path, encoding="utf-8") as handle:
				lines = handle.read().count("\n") + 1
			self.assertLessEqual(lines, limit, f"{name} has {lines} lines")

	def test_intent_mapping_in_controller(self):
		page = self.src["page"]
		self.assertIn("resolve_default_intent_for_row", page)
		self.assertIn("row_to_drill_node", page)
		self.assertIn("apply_drill_graph_resolution", page)
		self.assertIn("drill_row(row, explicit_intent", page)
		# No entity-specific gesture hardcoding as primary double-click path
		self.assertNotIn("if (axis === \"dimension\") {\n\t\t\tif (row.is_virtual_group", page)

	def test_axis_switch_preserves_session_filters(self):
		page = self.src["page"]
		# presentation-only axis switch must not clear analysis_filters bag
		start = page.index("switch_axis(view_axis")
		end = page.index("\n\tis_datatable_summary_enabled", start)
		block = page[start:end]
		self.assertIn("evaluate_analysis_filter_lifetimes", block)
		self.assertIn("render_filter_summary", block)
		self.assertNotIn("analysis_filters =", block)
		self.assertNotIn("selected_dimension_value = null;", block)

	def test_filter_summary_grouped(self):
		page = self.src["page"]
		self.assertIn('__("Document Scope")', page)
		self.assertIn('__("Analysis Filters")', page)
		self.assertIn('__("Presentation")', page)
		self.assertIn("remove_analysis_filter_chip", page)
		self.assertIn("clear_all_analysis_filters", page)
		css = self.src["css"]
		self.assertIn(".ae-filter-summary", css)
		self.assertNotIn(".ae-filter-summary {\n\tdisplay: none;", css)

	def test_payload_compatibility_mapper(self):
		page = self.src["page"]
		start = page.index("build_payload()")
		end = page.index("\n\tget_summary_method", start)
		block = page[start:end]
		self.assertIn("build_legacy_scope_payload_from_analysis_filters", block)

	def test_workspace_url_v2(self):
		app_root = frappe.get_app_path("erpnext_extensions")
		codec_path = os.path.join(
			app_root, "erpnext_extensions", "page", "account_explorer", "core", "explorer_workspace_codec.js"
		)
		with open(codec_path, encoding="utf-8") as handle:
			codec = handle.read()
		ws = self.src["ws"]
		self.assertIn("AE_URL_VERSION = 2", codec)
		self.assertIn("AE_URL_VERSION_LEGACY = 1", codec)
		self.assertIn('"af"', codec)
		self.assertIn("analysis_filters", codec + ws)
		self.assertIn("legacy_url", codec)
		self.assertIn("AEWorkspaceCodec", ws + codec)

	def test_saved_view_compat_hydrate(self):
		page = self.src["page"]
		self.assertIn('hydrate_analysis_filters_from_legacy({ origin: "saved_view" })', page)

	def test_events_namespaced(self):
		events = self.src["events"]
		for name in (
			"analysis_filters:changed",
			"analysis_filter:added",
			"analysis_filter:removed",
			"analysis_filters:cleared",
			"intent:resolved",
			"drill_graph:transition",
			"filter_summary:rendered",
		):
			self.assertIn(name, events)

	def test_breadcrumb_not_sole_filter_host(self):
		page = self.src["page"]
		self.assertIn("get_analysis_filters", page)
		# dimension filter intents no longer push breadcrumb as sole owner
		self.assertIn("apply_drill_graph_resolution", page)

	def test_account_default_intent_is_session_filter(self):
		graph = self.src["graph"]
		self.assertIn('default_intent: "filter"', graph)
		self.assertIn('filter_key: "account"', graph)
		self.assertIn('lifetime: "session"', graph)
		self.assertIn('edge_type: "apply_filter"', graph)
		self.assertIn("advance_level", graph)
		# Account hierarchy defaults must apply session filter, not navigate-only drill.
		for node in ("AccountGroup", "GeneralLedger", "SubsidiaryLedger"):
			idx = graph.index(f'id: "{node}"')
			snippet = graph[idx : idx + 80]
			self.assertIn('default_intent: "filter"', snippet)

	def test_filter_intent_does_not_advance_account_level(self):
		"""v4.6.3 — Analyze / filter keeps presentation level; navigate alone advances."""
		graph = self.src["graph"]
		self.assertIn("presentation level unchanged", graph)
		self.assertIn("advance_level is navigate-only", graph)
		for node in ("AccountGroup", "GeneralLedger"):
			# filter edges for these nodes must be apply_filter only (no advance_level).
			needle = f'from_node: "{node}",\n\t\t\t\tintent: "filter",\n\t\t\t\tedge_type: "advance_level"'
			self.assertNotIn(needle, graph)
			apply_needle = f'from_node: "{node}",\n\t\t\t\tintent: "filter",\n\t\t\t\tedge_type: "apply_filter"'
			self.assertIn(apply_needle, graph)
			nav_needle = f'from_node: "{node}",\n\t\t\t\tintent: "navigate",\n\t\t\t\tedge_type: "advance_level"'
			self.assertIn(nav_needle, graph)

	def test_explicit_analyze_actions(self):
		page = self.src["page"]
		self.assertIn("analyze_row_as_filter", page)
		self.assertIn("build_analyze_action_html", page)
		self.assertIn('__("Analyze This Voucher")', page)
		self.assertIn('action === "analyze"', page)
		self.assertIn("1 row selected", page)
		self.assertIn("checkbox selection does not filter axes", page)

	def test_selection_does_not_mutate_analysis_filters(self):
		page = self.src["page"]
		start = page.index("handle_datatable_selection_change")
		end = page.index("\n\tget_datatable_grid_options", start)
		block = page[start:end]
		self.assertIn("checked_row_keys", block)
		self.assertNotIn("set_analysis_filters", block)
		self.assertNotIn("apply_drill_graph", block)
		self.assertNotIn("analysis_filters", block)

	def test_navigated_drill_still_refreshes_when_filters_change(self):
		page = self.src["page"]
		start = page.index("drill_row(row, explicit_intent")
		end = page.index("\n\tanalyze_row_as_filter", start)
		block = page[start:end]
		self.assertIn("result.changed_filters || !result.navigated", block)
		self.assertIn("render_filter_summary", block)

	def test_account_filter_chip_uses_key_label(self):
		af = self.src["af"] + "\n" + self.src["af_summary"]
		self.assertIn('__("Account Group")', af)
		self.assertIn("source_axis_label", af)
		self.assertIn("_lifetime_label", af)
		self.assertIn("origin_label", af)
		self.assertIn("format_account_summary_label", af)
		self.assertIn("display_code", af)
		# Must combine code + title for Account filter summary chips
		self.assertIn("${c} - ${t}", af)
		self.assertIn("enrich_account_filter_summary_labels", self.src["page"])

	def test_auto_amount_mode_follows_settings_not_magnitude(self):
		page = self.src["page"]
		self.assertIn("ae_resolve_effective_number_format_mode", page)
		self.assertIn("settings_scale", page)
		# Magnitude auto-scale branches must not drive Auto mode.
		self.assertNotIn("abs >= 1e12", page.split("ae_format_amount_with_mode")[1].split("function ae_format_compact_amount")[0])

"""Prove Costing English desk patch + repair succeed with DocType ensure."""

from __future__ import annotations

import json
from pathlib import Path

import frappe

from erpnext_extensions.desk_navigation import assert_sidebar_link_targets_exist, collect_sidebar_link_targets
from erpnext_extensions.patches.post_model_sync.ensure_costing_english_desk_v5727 import (
	REQUIRED_COSTING_DOCTYPES,
	_SIDEBAR_ITEMS,
	ensure_now,
	execute,
)


def main():
	# Fresh run of fixed patch
	execute()
	result = ensure_now()

	# Verify every sidebar link target exists
	assert_sidebar_link_targets_exist(_SIDEBAR_ITEMS)
	targets = collect_sidebar_link_targets(_SIDEBAR_ITEMS)

	# Verify installed DocTypes
	doctypes = {dt: bool(frappe.db.exists("DocType", dt)) for dt in REQUIRED_COSTING_DOCTYPES}

	# Sidebar item rows on modern + archive
	ws_items = 0
	if frappe.db.exists("Workspace Sidebar", "Costing"):
		ws_items = frappe.db.count("Workspace Sidebar Item", {"parent": "Costing"})
	sb_items = 0
	if frappe.db.exists("Sidebar", "Costing"):
		sb_items = frappe.db.count("Sidebar Item", {"parent": "Costing"})

	# Idempotent second run
	execute()
	result2 = ensure_now()

	out = {
		"version": __import__("erpnext_extensions").__version__,
		"doctypes": doctypes,
		"all_doctypes_present": all(doctypes.values()),
		"link_targets": [{"link_type": a, "link_to": b} for a, b in targets],
		"workspace_sidebar_items": ws_items,
		"sidebar_items": sb_items,
		"ensure_now": result,
		"ensure_now_rerun": result2,
		"idempotent_item_counts": ws_items == frappe.db.count("Workspace Sidebar Item", {"parent": "Costing"})
		if frappe.db.exists("Workspace Sidebar", "Costing")
		else True,
		"rules_untouched": frappe.db.count("Expense Account Dimension Rule"),
		"gl_untouched": frappe.db.count("GL Entry"),
	}
	Path("/tmp/costing_qa_v5727").mkdir(parents=True, exist_ok=True)
	Path("/tmp/costing_qa_v5727/sidebar_migrate.json").write_text(
		json.dumps(out, indent=2, default=str, ensure_ascii=False), encoding="utf-8"
	)
	print(json.dumps(out, indent=2, default=str, ensure_ascii=False))
	return out

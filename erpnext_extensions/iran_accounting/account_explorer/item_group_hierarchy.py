# Copyright (c) 2026, Farbod Siyahpoosh and contributors
"""Item Group nested-set helpers (ERPNext NestedSet via lft/rgt)."""

from __future__ import annotations

import frappe
from frappe import _


def get_item_group_node(item_group: str) -> dict | None:
	if not item_group:
		return None
	row = frappe.db.get_value(
		"Item Group",
		item_group,
		["name", "parent_item_group", "is_group", "lft", "rgt"],
		as_dict=True,
	)
	return row


def get_descendant_item_groups(item_group: str, *, include_self: bool = True) -> list[str]:
	"""Return Item Group names in the subtree of ``item_group`` via lft/rgt."""
	node = get_item_group_node(item_group)
	if not node:
		frappe.throw(_("Item Group {0} does not exist").format(item_group))
	op_lft = ">=" if include_self else ">"
	op_rgt = "<=" if include_self else "<"
	return frappe.db.sql_list(
		f"""
		select name from `tabItem Group`
		where lft {op_lft} %s and rgt {op_rgt} %s
		order by lft
		""",
		(node.lft, node.rgt),
	)


def get_direct_child_item_groups(item_group: str | None) -> list[dict]:
	"""Direct children of a parent Item Group (or roots when parent is None)."""
	filters = {"parent_item_group": item_group} if item_group else {"parent_item_group": ["in", ["", None]]}
	# Empty parent: roots typically have parent_item_group = "" or "All Item Groups"
	if item_group:
		return frappe.get_all(
			"Item Group",
			filters={"parent_item_group": item_group},
			fields=["name", "is_group", "lft", "rgt", "parent_item_group"],
			order_by="lft",
		)
	# Prefer children of the standard root when present
	root = frappe.db.get_value("Item Group", {"parent_item_group": ["in", ["", None]]}, "name")
	all_item_groups = frappe.db.get_value("Item Group", "All Item Groups", "name")
	parent = all_item_groups or root
	if parent:
		return frappe.get_all(
			"Item Group",
			filters={"parent_item_group": parent},
			fields=["name", "is_group", "lft", "rgt", "parent_item_group"],
			order_by="lft",
		)
	return frappe.get_all(
		"Item Group",
		filters={"parent_item_group": ["in", ["", None]]},
		fields=["name", "is_group", "lft", "rgt", "parent_item_group"],
		order_by="lft",
	)


def resolve_item_group_scope_names(item_groups: list[str] | None) -> list[str]:
	"""Expand selected Item Groups to include all descendants (deduped)."""
	if not item_groups:
		return []
	resolved: list[str] = []
	seen: set[str] = set()
	for name in item_groups:
		if not name or name in seen:
			continue
		for child in get_descendant_item_groups(name, include_self=True):
			if child not in seen:
				seen.add(child)
				resolved.append(child)
	return resolved


def get_leaf_item_groups(item_groups: list[str]) -> list[str]:
	"""Return only leaf (is_group=0) Item Groups from a list (may include parents)."""
	if not item_groups:
		return []
	rows = frappe.get_all(
		"Item Group",
		filters={"name": ["in", item_groups], "is_group": 0},
		pluck="name",
		order_by="lft",
	)
	return rows


def map_leaf_to_presentation_group(
	leaf_item_group: str, presentation_parents: list[dict]
) -> str | None:
	"""Map a leaf Item Group to the presentation parent whose subtree contains it."""
	if not leaf_item_group or not presentation_parents:
		return None
	for parent in presentation_parents:
		if parent.get("name") == leaf_item_group:
			return parent["name"]
	node = get_item_group_node(leaf_item_group)
	if not node:
		return None
	for parent in presentation_parents:
		plft = parent.get("lft")
		prgt = parent.get("rgt")
		if plft is None or prgt is None:
			continue
		if node.lft >= plft and node.rgt <= prgt:
			return parent["name"]
	return None

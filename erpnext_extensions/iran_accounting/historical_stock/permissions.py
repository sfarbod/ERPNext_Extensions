# Copyright (c) 2026, ERPNext Extensions contributors
"""Historical Repair access: read / repair / administrator. No new engines."""

from __future__ import annotations

import frappe

LEVEL_READ = 1
LEVEL_REPAIR = 2
LEVEL_ADMIN = 3

READ_ROLES = ("Stock Manager", "Manufacturing Manager", "Accounts Manager")
REPAIR_ROLES = ("System Manager",)
ADMIN_IDENTITIES = ("Administrator",)

# A = production-ready (MVR), B = operational write (System Manager), C = experimental (Administrator)
FEATURE_MATURITY = {
	"scan": "A",
	"scan_all": "A",
	"dry_run": "A",
	"dashboard": "A",
	"expected_rate": "A",
	"impact_analysis": "A",
	"graph": "A",
	"integrity": "A",
	"repair_history": "A",
	"export": "A",
	"search_filter_sort": "A",
	"repair_selected": "B",
	"repair_i4_patient_zero": "B",
	"repair_current_scope": "B",
	"repair_current_filter": "B",
	"repair_current_page": "B",
	"repair_dependency_chain": "B",
	"replay_downstream": "B",
	"rebuild_affected": "B",
	"repost_selected": "B",
	"root_cause_explorer": "A",
	"identity_health": "A",
	"master_repair_plan": "A",
	"find_patient_zero": "A",
	"resume": "C",
	"cancel": "C",
	"advanced_mode": "C",
	"rollback": "C",
	"benchmark": "C",
	"snapshots": "C",
	"developer_tools": "C",
	"diagnose_batch": "C",
}


def access_level(user=None) -> int:
	user = user or frappe.session.user
	roles = set(frappe.get_roles(user))
	if user in ADMIN_IDENTITIES or "Administrator" in roles:
		return LEVEL_ADMIN
	if any(r in roles for r in REPAIR_ROLES):
		return LEVEL_REPAIR
	if any(r in roles for r in READ_ROLES):
		return LEVEL_READ
	frappe.throw("Not permitted to use Historical Repair", frappe.PermissionError)


def require_read() -> int:
	return access_level()


def require_repair() -> int:
	level = access_level()
	if level < LEVEL_REPAIR:
		frappe.throw("Repair requires System Manager", frappe.PermissionError)
	return level


def require_admin() -> int:
	level = access_level()
	if level < LEVEL_ADMIN:
		frappe.throw("This tool is Administrator only", frappe.PermissionError)
	return level


def require_write_if_applying(dry_run: bool) -> int:
	return require_read() if dry_run else require_repair()


def session_info() -> dict:
	level = access_level()
	can_repair = level >= LEVEL_REPAIR
	can_admin = level >= LEVEL_ADMIN
	return {
		"level": level,
		"level_name": {1: "operational_read", 2: "system_manager", 3: "administrator"}[level],
		"user": frappe.session.user,
		"can_read": True,
		"can_repair": can_repair,
		"can_admin": can_admin,
		"maturity": FEATURE_MATURITY,
		"visible": {
			"scan": True,
			"scan_all": True,
			"dry_run": True,
			"dashboard": True,
			"expected_rate": True,
			"impact": True,
			"graph": True,
			"integrity": True,
			"history": True,
			"export": True,
			"repair": can_repair,
			"repair_scope": can_repair,
			"repair_filter": can_repair,
			"repair_page": can_repair,
			"repair_chain": can_repair,
			"replay": can_repair,
			"rebuild": can_repair,
			"repost": can_repair,
			"resume": can_admin,
			"cancel": can_admin,
			"advanced": can_admin,
			"rollback": can_admin,
			"benchmark": can_admin,
			"snapshots": can_admin,
		},
	}

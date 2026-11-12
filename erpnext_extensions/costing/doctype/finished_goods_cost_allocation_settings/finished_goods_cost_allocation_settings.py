# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, now_datetime


class FinishedGoodsCostAllocationSettings(Document):
	def validate(self):
		self._validate_company_unique()
		self._sync_business_calendar()
		self._validate_rule_accounts_belong_to_company()
		self._validate_unique_item_primary_uom()
		self._validate_unique_expense_rules()
		self._refresh_uom_row_statuses()
		self.sync_uom_validation_summary_rows()

	def sync_uom_validation_summary_rows(self):
		"""Rebuild read-only Last UOM Validation Summary from Item Primary UOMs."""
		if not self.meta.has_field("uom_validation_summary"):
			return
		self.set("uom_validation_summary", [])
		for row in self.item_primary_uoms or []:
			self.append(
				"uom_validation_summary",
				{
					"item_code": row.item,
					"item_name": row.item_name,
					"stock_uom": row.stock_uom,
					"primary_uom": row.primary_uom,
					"source_conversion_factor": row.source_conversion_factor,
					"manual_override_factor": row.manual_override_factor,
					"applied_conversion_factor": row.conversion_factor,
					"validation_status": row.conversion_status,
					"validation_message": row.remarks,
				},
			)

	def replace_rule_generation_summary_rows(self, rows: list[dict], generation_date: str | None = None):
		"""Replace Last Rule Generation Summary child rows (read-only audit grid)."""
		if not self.meta.has_field("rule_generation_summary"):
			return
		self.set("rule_generation_summary", [])
		ts = generation_date or str(now_datetime())
		for r in rows or []:
			self.append(
				"rule_generation_summary",
				{
					"account_code": r.get("account_code"),
					"account_name": r.get("account_name"),
					"account": r.get("account"),
					"cost_center": r.get("cost_center") or "",
					"department": r.get("department") or "",
					"action": r.get("action"),
					"reason": r.get("reason"),
					"generation_date": r.get("generation_date") or ts,
					"generation_status": r.get("generation_status") or r.get("action"),
				},
			)

	def _validate_company_unique(self):
		if not self.company:
			return
		filters = {"company": self.company}
		if not self.is_new():
			filters["name"] = ("!=", self.name)
		if frappe.db.exists("Finished Goods Cost Allocation Settings", filters):
			frappe.throw(
				_("Finished Goods Cost Allocation Settings already exists for Company {0}.").format(
					frappe.bold(self.company)
				),
				title=_("Duplicate Company"),
			)

	def _sync_business_calendar(self):
		if not self.company:
			self.business_calendar = None
			return
		self.business_calendar = frappe.db.get_value("Company", self.company, "business_calendar")

	def _validate_rule_accounts_belong_to_company(self):
		if not self.company:
			return
		for row in self.expense_account_dimension_rules or []:
			if not row.account:
				continue
			account_company = frappe.db.get_value("Account", row.account, "company")
			if account_company and account_company != self.company:
				frappe.throw(
					_("Row {0}: Account {1} belongs to Company {2}, not {3}.").format(
						row.idx,
						frappe.bold(row.account),
						frappe.bold(account_company),
						frappe.bold(self.company),
					),
					title=_("Company Mismatch"),
				)

	def _validate_unique_item_primary_uom(self):
		seen = set()
		for row in self.item_primary_uoms or []:
			if not row.item:
				continue
			if row.item in seen:
				frappe.throw(_("Duplicate Primary UOM row for Item {0}.").format(row.item))
			seen.add(row.item)

	def _validate_unique_expense_rules(self):
		seen = set()
		for row in self.expense_account_dimension_rules or []:
			key = (
				(row.account or "").strip(),
				(row.cost_center or "").strip(),
				(row.department or "").strip(),
			)
			if key in seen:
				frappe.throw(
					_("Duplicate expense rule for Account/Cost Center/Department: {0} / {1} / {2}").format(
						key[0], key[1] or "(blank)", key[2] or "(blank)"
					)
				)
			seen.add(key)
		self._validate_overlapping_expense_rules()

	def _validate_overlapping_expense_rules(self):
		"""Reject enabled rules whose optional-dimension match sets can intersect.

		Blank Cost Center / Department means unrestricted filter, so a blank-CC rule
		overlaps any same-Account rule that shares (or also blanks) Department.
		"""
		from erpnext_extensions.iran_accounting.fg_cost_allocation.gl_extract import (
			RuleSpec,
			detect_overlapping_rules,
		)

		specs = []
		for row in self.expense_account_dimension_rules or []:
			if not row.account or not int(row.enabled or 0):
				continue
			specs.append(
				RuleSpec(
					name=f"row-{row.idx}",
					account=row.account,
					cost_center=row.cost_center or "",
					department=row.department or "",
					enabled=1,
				)
			)
		pairs = detect_overlapping_rules(specs)
		if not pairs:
			return
		sample = pairs[0]
		frappe.throw(
			_(
				"Overlapping Expense Account Dimension Rules are not allowed. "
				"Row pair on Account {0}: ({1} / {2}) overlaps ({3} / {4}). "
				"Blank Cost Center or Department means unrestricted matching and must not "
				"intersect another active rule for the same Account. "
				"({5} overlapping pair(s) detected.)"
			).format(
				frappe.bold(sample.get("account")),
				sample.get("rule_a_cost_center") or "(any Cost Center)",
				sample.get("rule_a_department") or "(any Department)",
				sample.get("rule_b_cost_center") or "(any Cost Center)",
				sample.get("rule_b_department") or "(any Department)",
				len(pairs),
			),
			title=_("Overlapping Rules"),
		)

	def _prior_uom_map(self) -> dict[str, dict]:
		if self.is_new() or not self.name:
			return {}
		rows = frappe.get_all(
			"FG Item Primary UOM",
			filters={"parent": self.name},
			fields=[
				"name",
				"item",
				"conversion_factor",
				"manual_override_factor",
				"source_conversion_factor",
				"conversion_status",
				"override_reason",
			],
		)
		return {r.item: r for r in rows if r.item}

	def _refresh_uom_row_statuses(self):
		from erpnext_extensions.iran_accounting.fg_cost_allocation.uom_config import apply_row_factors

		prior = self._prior_uom_map()
		audit_events = []

		for row in self.item_primary_uoms or []:
			if not row.item or not row.primary_uom:
				continue

			prev = prior.get(row.item)
			prev_applied = flt(prev.conversion_factor) if prev else flt(row.conversion_factor)

			raw_manual = row.manual_override_factor
			manual = flt(raw_manual) if raw_manual not in (None, "") else None
			if manual == 0:
				row.manual_override_factor = None
				manual = None
			if manual is not None and manual < 0:
				frappe.throw(
					_("Row {0}: Manual Override Factor must be a positive number.").format(row.idx)
				)

			# If user typed Applied Conversion Factor directly (differs from source path),
			# treat it as a manual override so Item master is never written.
			typed_applied = flt(row.conversion_factor) if row.conversion_factor not in (None, "") else None
			if manual is None and typed_applied is not None and typed_applied > 0:
				# Compare against source after resolve — apply_row_factors below needs override set first
				from erpnext_extensions.iran_accounting.fg_cost_allocation.uom_config import (
					resolve_conversion,
				)

				source, _status, _rem = resolve_conversion(row.item, row.primary_uom)
				source_f = float(source) if source is not None else None
				if source_f is None or abs(typed_applied - source_f) > 1e-12:
					if typed_applied <= 0:
						frappe.throw(
							_("Row {0}: Conversion Factor must be a positive number.").format(row.idx)
						)
					row.manual_override_factor = typed_applied
					manual = typed_applied

			if manual is not None and manual <= 0:
				frappe.throw(
					_("Row {0}: Manual Override Factor / Conversion Factor must be positive.").format(
						row.idx
					)
				)

			apply_row_factors(row)
			applied = flt(row.conversion_factor)
			if applied < 0:
				frappe.throw(_("Row {0}: Applied Conversion Factor cannot be negative.").format(row.idx))
			if row.conversion_status == "Invalid" and applied == 0 and manual is None:
				# Missing/invalid without override is allowed (blocks consumability later)
				pass

			if manual is not None and abs(applied - prev_applied) > 1e-12:
				row.previous_applied_factor = prev_applied
				row.override_by = frappe.session.user
				row.override_on = now_datetime()
				audit_events.append(
					{
						"item": row.item,
						"previous": prev_applied,
						"new": applied,
						"reason": row.override_reason or "",
						"user": frappe.session.user,
						"timestamp": str(now_datetime()),
					}
				)

		if audit_events:
			existing = {}
			try:
				existing = json.loads(self.last_uom_validation_summary or "{}")
				if not isinstance(existing, dict):
					existing = {"summary": existing}
			except Exception:
				existing = {"raw": self.last_uom_validation_summary}
			existing.setdefault("conversion_overrides", [])
			existing["conversion_overrides"].extend(audit_events)
			# keep last 200 events
			existing["conversion_overrides"] = existing["conversion_overrides"][-200:]
			self.last_uom_validation_summary = json.dumps(existing, ensure_ascii=False, indent=2, default=str)

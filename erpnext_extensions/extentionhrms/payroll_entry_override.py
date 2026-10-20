# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Runtime shim that makes the HRMS payroll accrual entry dimension-complete.

This is the *integration seam*: it reads Salary Slips / cost-centre splits /
company settings out of the database, hands them to the framework-free grouping
logic in :mod:`payroll_accrual_grouping`, and feeds the resulting rows back into
HRMS's own ``make_journal_entry`` (so multi-currency, salary-slip linking and
submission stay 100%% native).

Only ``make_accrual_jv_entry`` is overridden. Everything else on the Payroll
Entry controller is inherited unchanged. The heavy lifting — splitting by cost
centre, grouping by ``(account, cost_center, department)``, stamping Department
on P&L rows, and pushing the rounding residue to the round-off account — lives
in the pure module and is exercised by ``tests/test_payroll_accrual_grouping.py``.

Wire-up (``hooks.py``)::

    override_doctype_class = {
        "Payroll Entry": (
            "erpnext_extensions.extentionhrms.payroll_entry_override." "PayrollEntryWithAccountingDimensions"
        ),
    }

Assumption: ``Payroll Settings.process_payroll_accounting_entry_based_on_employee``
is **OFF** (aggregated accrual). When it is ON, HRMS books per-employee party
rows and we defer to the stock implementation.
"""

from __future__ import annotations

import frappe
from frappe.utils import cint, flt
from hrms.payroll.doctype.payroll_entry.payroll_entry import PayrollEntry

from erpnext_extensions.extentionhrms.custom_fields import PROCESS_BASED_ON_EMPLOYEE_FIELD
from erpnext_extensions.extentionhrms.payroll_accrual_grouping import (
	AccrualConfig,
	ComponentAmount,
	CostCenterSplit,
	SalarySlip,
	build_accrual_journal_accounts,
)

# Department stamped on the P&L round-off line (the round-off account is an
# Expense account, so it needs one). Configured per company via a custom field;
# there is no baked-in default, so a company must set it explicitly.
ROUND_OFF_DEPARTMENT_CUSTOM_FIELD = "custom_payroll_round_off_department"


class PayrollEntryWithAccountingDimensions(PayrollEntry):
	"""Payroll Entry that books the accrual grouped by Account + Cost Center +
	Department, sourcing Department from each Salary Slip."""

	def make_accrual_jv_entry(self, submitted_salary_slips):
		employee_wise = cint(
			frappe.db.get_single_value(
				"Payroll Settings",
				"process_payroll_accounting_entry_based_on_employee",
			)
		)
		if employee_wise:
			# Per-employee party accounting is a different structure; leave it to HRMS.
			return super().make_accrual_jv_entry(submitted_salary_slips)

		self.payroll_payable_account = self.get_payroll_payable_account_value()
		company_currency = frappe.get_cached_value("Company", self.company, "default_currency")

		slips = self._collect_salary_slips()
		self._assert_departments_present(slips)
		config = self._build_accrual_config(company_currency)
		result = build_accrual_journal_accounts(slips, config)

		if result.skipped_components:
			# Statistical / calculation-base components with no GL account are not
			# booked (same as stock HRMS). Record which ones for traceability.
			frappe.logger("payroll").info(
				f"[{self.name}] accrual skipped unmapped salary components: "
				+ ", ".join(result.skipped_components)
			)

		accounts = self._to_journal_entry_rows(result, config)

		# Save as Draft so finance can review debit/credit balance and dimensions
		# before posting. (Auto-submit previously left failed/unbalanced drafts when
		# currency re-rounding diverged expense vs per-employee liability rows.)
		return self.make_journal_entry(
			accounts,
			[company_currency],
			self.payroll_payable_account,
			voucher_type="Journal Entry",
			user_remark=frappe._("Accrual Journal Entry for salaries from {0} to {1}").format(
				self.start_date, self.end_date
			),
			submit_journal_entry=False,
			submitted_salary_slips=submitted_salary_slips,
			employee_wise_accounting_enabled=False,
		)

	# ------------------------------------------------------------------
	# Data collection (DB -> framework-free structures)
	# ------------------------------------------------------------------

	def _collect_salary_slips(self) -> list[SalarySlip]:
		rows = frappe.get_all(
			"Salary Slip",
			filters={"payroll_entry": self.name, "docstatus": 1},
			fields=["name", "employee", "department", "salary_structure"],
		)

		slips: list[SalarySlip] = []
		for r in rows:
			earnings = self._salary_details(r.name, "earnings")
			deductions = self._salary_details(r.name, "deductions")
			splits = self._cost_center_splits(r.employee, r.salary_structure)
			slips.append(
				SalarySlip.make(
					name=r.name,
					employee=r.employee,
					# Department per the accounting team's directive: the Salary
					# Slip's own department, NOT the Employee master.
					department=r.department,
					cost_center_splits=splits,
					earnings=earnings,
					deductions=deductions,
				)
			)
		return slips

	def _assert_departments_present(self, slips: list[SalarySlip]) -> None:
		"""Fail early, with the full list, if any slip lacks a Department — every
		P&L row needs one, so a blank Department would otherwise fail JV validation
		with a per-account error that is hard to trace back to the employee."""
		missing = [s for s in slips if not s.department]
		if not missing:
			return
		lines = "\n".join(f"- {s.name} ({s.employee})" for s in missing)
		frappe.throw(
			frappe._(
				"The following Salary Slips have no Department, which is mandatory "
				"for P&L accounts. Set a Department on each and regenerate:\n{0}"
			).format(lines)
		)

	def _salary_details(self, slip: str, parentfield: str) -> list[ComponentAmount]:
		rows = frappe.get_all(
			"Salary Detail",
			filters={
				"parent": slip,
				"parenttype": "Salary Slip",
				"parentfield": parentfield,
				"amount": [">", 0],
			},
			fields=["salary_component", "amount"],
		)
		return [ComponentAmount.make(x.salary_component, x.amount) for x in rows]

	def _cost_center_splits(self, employee: str, salary_structure: str) -> list[CostCenterSplit]:
		# Reuse HRMS's own resolver so the split percentages are identical to the
		# stock accrual (falls back to employee / department / payroll-entry CC).
		cost_centers = self.get_payroll_cost_centers_for_employee(employee, salary_structure)
		splits = [CostCenterSplit.make(cc, pct) for cc, pct in cost_centers.items()]
		if not splits:
			splits = [CostCenterSplit.make(self.cost_center, 100)]
		return splits

	# ------------------------------------------------------------------
	# Config
	# ------------------------------------------------------------------

	def _build_accrual_config(self, company_currency: str) -> AccrualConfig:
		component_accounts = self._component_account_map()
		account_root_type = self._account_root_type_map(component_accounts.values())

		round_off_account, round_off_cost_center = frappe.get_cached_value(
			"Company", self.company, ["round_off_account", "round_off_cost_center"]
		)

		return AccrualConfig(
			component_accounts=component_accounts,
			account_root_type=account_root_type,
			payable_account=self.payroll_payable_account,
			round_off_account=round_off_account,
			round_off_cost_center=round_off_cost_center or self.cost_center,
			round_off_department=self._round_off_department(),
			per_employee_components=self._per_employee_components(),
			account_parties=self._account_party_map(),
			precision=self._currency_precision(),
		)

	def _account_party_map(self) -> dict[str, tuple[str, str]]:
		"""``{account: (party_type, party)}`` from the Salary Component Account
		rows that carry a Party (e.g. the SSO / tax Supplier). Replaces the old
		party-assignment Server Script. Empty if the custom fields are absent."""
		meta = frappe.get_meta("Salary Component Account")
		if not (meta.has_field("custom_party_type") and meta.has_field("custom_party")):
			return {}
		rows = frappe.get_all(
			"Salary Component Account",
			filters={"company": self.company, "custom_party": ["is", "set"]},
			fields=["account", "custom_party_type", "custom_party"],
		)
		mapping: dict[str, tuple[str, str]] = {}
		for r in rows:
			if r.account and r.custom_party_type and r.custom_party:
				mapping.setdefault(r.account, (r.custom_party_type, r.custom_party))
		return mapping

	def _per_employee_components(self) -> frozenset[str]:
		"""Salary Components flagged ``Process Based on Employee`` — booked as a
		separate row per employee (with the Employee as Party) instead of being
		aggregated by cost centre / department. Empty if the custom field is
		absent (e.g. before migrate)."""
		if not frappe.get_meta("Salary Component").has_field(PROCESS_BASED_ON_EMPLOYEE_FIELD):
			return frozenset()
		rows = frappe.get_all(
			"Salary Component",
			filters={PROCESS_BASED_ON_EMPLOYEE_FIELD: 1},
			pluck="name",
		)
		return frozenset(rows)

	def _component_account_map(self) -> dict[str, str]:
		rows = frappe.get_all(
			"Salary Component Account",
			filters={"company": self.company},
			fields=["parent", "account"],
		)
		mapping: dict[str, str] = {}
		for r in rows:
			mapping.setdefault(r.parent, r.account)
		return mapping

	def _account_root_type_map(self, accounts) -> dict[str, str]:
		unique = {a for a in accounts if a}
		# include payable + round-off so is_pl_account() can classify them
		unique.add(self.payroll_payable_account)
		round_off = frappe.get_cached_value("Company", self.company, "round_off_account")
		if round_off:
			unique.add(round_off)
		if not unique:
			return {}
		rows = frappe.get_all(
			"Account",
			filters={"name": ["in", list(unique)]},
			fields=["name", "root_type"],
		)
		return {r.name: r.root_type for r in rows}

	def _round_off_department(self) -> str | None:
		"""The Department for the round-off line, read from a Company custom field.

		The round-off account is P&L, so the line needs a Department. There is no
		hard-coded default: if the field is missing/blank the round-off residue
		would fail the mandatory-dimension check, so we surface a clear error.
		"""
		if frappe.get_meta("Company").has_field(ROUND_OFF_DEPARTMENT_CUSTOM_FIELD):
			configured = frappe.get_cached_value("Company", self.company, ROUND_OFF_DEPARTMENT_CUSTOM_FIELD)
			if configured:
				return configured
		frappe.throw(
			frappe._(
				"Set the payroll round-off Department on the Company "
				"(field '{0}') — it is required to book the accrual rounding residue."
			).format(ROUND_OFF_DEPARTMENT_CUSTOM_FIELD)
		)

	def _currency_precision(self) -> int:
		"""Precision that Journal Entry Account currency fields will use on save.

		``frappe.get_precision("Journal Entry Account", ...)`` without a row
		context defaults to 2. On validate, ERPNext sets ``account_currency`` and
		Currency fields then resolve via System Settings ``currency_precision``,
		or — when that is empty and ``use_number_format_from_currency`` is on —
		via the company currency's number format (IRR ``#,###`` → 0).

		Building the accrual at a coarser precision than JE will apply (e.g. 2
		then re-round to 0) unbalances aggregated expense rows against
		per-employee liability rows. Match JE's eventual precision up front so
		round-off (if any) is computed on the same amounts the JE will store.
		"""
		from frappe.model.meta import get_precision_from_currency_format
		from frappe.utils.data import get_currency_precision

		system_precision = get_currency_precision()
		if system_precision is not None:
			return cint(system_precision)

		company_currency = frappe.get_cached_value("Company", self.company, "default_currency")
		if company_currency:
			return cint(get_precision_from_currency_format(company_currency))

		return cint(frappe.get_precision("Journal Entry Account", "debit_in_account_currency"))

	def get_payroll_payable_account_value(self) -> str:
		return self.payroll_payable_account or frappe.get_cached_value(
			"Company", self.company, "default_payroll_payable_account"
		)

	# ------------------------------------------------------------------
	# Result -> Journal Entry account dicts (HRMS shape)
	# ------------------------------------------------------------------

	def _to_journal_entry_rows(self, result, config: AccrualConfig) -> list[dict]:
		precision = config.precision
		accounts: list[dict] = []
		for row in result.rows:
			entry: dict = {
				"account": row.account,
				"exchange_rate": 1.0,
				"cost_center": row.cost_center,
				"project": self.project,
			}
			if row.debit:
				entry["debit_in_account_currency"] = flt(row.debit, precision)
			else:
				entry["credit_in_account_currency"] = flt(row.credit, precision)

			if row.department:
				entry["department"] = row.department
			if row.party_type:
				entry["party_type"] = row.party_type
				entry["party"] = row.party

			# Link the payable rows back to this Payroll Entry (as HRMS does), so
			# downstream tooling can still discover the accrual JV.
			if row.account == self.payroll_payable_account:
				entry["reference_type"] = self.doctype
				entry["reference_name"] = self.name

			accounts.append(entry)
		return accounts

# Copyright (c) 2026, Farbod Siyahpoosh and contributors
"""Deterministic GL fixtures for Analytical Filter ↔ Account Explorer parity."""

from __future__ import annotations

import frappe
from frappe.utils import cint, flt, getdate

from erpnext_extensions.iran_accounting.tests.test_account_explorer_fixtures import (
	current_fiscal_year,
	enable_wave2c_unified_party,
)


PARITY_MARKER = "AE-AF-PARITY"


def ensure_parity_company(test_case) -> dict:
	"""Return company/FY context on `_Test Company` with explorer features enabled."""
	if not frappe.db:
		test_case.skipTest("Database not available")
	company = "_Test Company"
	if not frappe.db.exists("Company", company):
		test_case.skipTest("ERPNext _Test Company not available")
	enable_wave2c_unified_party()
	fy = current_fiscal_year(company)
	if not fy:
		test_case.skipTest("No fiscal year for test company")
	fiscal_year, from_date, to_date = fy
	# Prefer current calendar year for new postings when FY spans it
	posting_date = str(to_date)
	today = getdate()
	if getdate(from_date) <= today <= getdate(to_date):
		posting_date = str(today)
	parent_cc = frappe.db.get_value(
		"Cost Center", {"company": company, "is_group": 1}, "name", order_by="lft"
	) or frappe.db.get_value("Company", company, "cost_center")
	return {
		"company": company,
		"fiscal_year": fiscal_year,
		"from_date": str(from_date),
		"to_date": str(to_date),
		"posting_date": posting_date,
		"parent_cost_center": parent_cc,
		"currency": frappe.db.get_value("Company", company, "default_currency") or "INR",
	}


def _account_by_type(company: str, account_type: str, currency: str | None = None) -> str | None:
	filters = {
		"company": company,
		"account_type": account_type,
		"is_group": 0,
		"disabled": 0,
	}
	if currency:
		filters["account_currency"] = currency
	return frappe.db.get_value("Account", filters, "name", order_by="lft")


def _non_party_leaf(company: str, need: int = 2, currency: str | None = None) -> list[str]:
	currency_clause = ""
	values: list = [company]
	if currency:
		currency_clause = " and account_currency=%s"
		values.append(currency)
	values.append(need)
	rows = frappe.db.sql(
		f"""
		select name from `tabAccount`
		where company=%s and is_group=0 and disabled=0
		  and ifnull(account_type,'') not in ('Receivable','Payable')
		  {currency_clause}
		order by lft
		limit %s
		""",
		tuple(values),
		as_dict=True,
	)
	return [row.name for row in rows]


def _ensure_customer(name: str) -> str:
	if frappe.db.exists("Customer", name):
		return name
	doc = frappe.get_doc(
		{
			"doctype": "Customer",
			"customer_name": name,
			"customer_type": "Company",
			"customer_group": frappe.db.get_value("Customer Group", {"is_group": 0}, "name")
			or "All Customer Groups",
			"territory": frappe.db.get_value("Territory", {"is_group": 0}, "name") or "All Territories",
			"tax_id": f"AE-{frappe.generate_hash(length=10)}",
		}
	)
	doc.flags.ignore_permissions = True
	doc.flags.ignore_mandatory = True
	doc.insert()
	return doc.name


def _ensure_supplier(name: str) -> str:
	if frappe.db.exists("Supplier", name):
		return name
	doc = frappe.get_doc(
		{
			"doctype": "Supplier",
			"supplier_name": name,
			"supplier_group": frappe.db.get_value("Supplier Group", {"is_group": 0}, "name")
			or "All Supplier Groups",
			"tax_id": f"AE-{frappe.generate_hash(length=10)}",
		}
	)
	doc.flags.ignore_permissions = True
	doc.flags.ignore_mandatory = True
	doc.insert()
	return doc.name


def _ensure_cost_center(company: str, name: str, parent: str | None) -> str:
	existing = frappe.db.get_value("Cost Center", {"company": company, "cost_center_name": name}, "name")
	if existing:
		return existing
	full_name = f"{name} - {frappe.get_cached_value('Company', company, 'abbr')}"
	if frappe.db.exists("Cost Center", full_name):
		return full_name
	doc = frappe.new_doc("Cost Center")
	doc.cost_center_name = name
	doc.company = company
	if parent:
		doc.parent_cost_center = parent
	doc.is_group = 0
	doc.flags.ignore_permissions = True
	doc.insert()
	return doc.name


def ensure_parity_dataset(ctx: dict) -> dict:
	"""Create two balancing Journal Entries tagged for parity tests.

	Use non-party GL lines only so fixture parties do not pollute other AE tests
	that discover customers/suppliers via company GL activity. Party-filter cases
	reuse an existing party that already has GL in the company.
	"""
	company = ctx["company"]
	currency = ctx["currency"]
	others = _non_party_leaf(company, 4, currency) or _non_party_leaf(company, 4)
	if len(others) < 4:
		frappe.throw("Need at least 4 non-party leaf accounts for parity fixtures")

	cost_center = _ensure_cost_center(
		company, f"{PARITY_MARKER}-CC", ctx.get("parent_cost_center")
	)

	# Cancel previous parity JEs so amounts stay deterministic (currency/FX-safe accounts).
	for old in frappe.get_all(
		"Journal Entry",
		filters={"company": company, "user_remark": ("like", f"{PARITY_MARKER}%"), "docstatus": 1},
		pluck="name",
	):
		doc = frappe.get_doc("Journal Entry", old)
		doc.flags.ignore_permissions = True
		doc.cancel()
	frappe.db.commit()

	def ensure_je(remark: str, lines: list[dict]) -> str:
		je = frappe.new_doc("Journal Entry")
		je.voucher_type = "Journal Entry"
		je.company = company
		je.posting_date = ctx["posting_date"]
		je.user_remark = remark
		for line in lines:
			je.append("accounts", line)
		je.flags.ignore_permissions = True
		je.insert()
		je.submit()
		return je.name

	je_a = ensure_je(
		f"{PARITY_MARKER}-JE-A",
		[
			{
				"account": others[0],
				"debit_in_account_currency": 1000,
				"debit": 1000,
				"cost_center": cost_center,
			},
			{
				"account": others[1],
				"credit_in_account_currency": 1000,
				"credit": 1000,
				"cost_center": cost_center,
			},
		],
	)
	je_b = ensure_je(
		f"{PARITY_MARKER}-JE-B",
		[
			{
				"account": others[2],
				"debit_in_account_currency": 2500,
				"debit": 2500,
				"cost_center": cost_center,
			},
			{
				"account": others[3],
				"credit_in_account_currency": 2500,
				"credit": 2500,
				"cost_center": cost_center,
			},
		],
	)

	# Party-filter coverage uses the stock ERPNext test customer so UAP discovery
	# sees a normal master (not a parity-named throwaway) when these rows exist.
	customer = "_Test Customer" if frappe.db.exists("Customer", "_Test Customer") else _ensure_customer(
		"_Test Customer"
	)
	receivable = _account_by_type(company, "Receivable", currency) or _account_by_type(
		company, "Receivable"
	)
	party_amount = 777.0
	je_party = None
	if receivable and customer:
		je_party = ensure_je(
			f"{PARITY_MARKER}-JE-PARTY",
			[
				{
					"account": receivable,
					"party_type": "Customer",
					"party": customer,
					"debit_in_account_currency": party_amount,
					"debit": party_amount,
					"cost_center": cost_center,
				},
				{
					"account": others[1],
					"credit_in_account_currency": party_amount,
					"credit": party_amount,
					"cost_center": cost_center,
				},
			],
		)
	frappe.db.commit()

	project = frappe.db.get_value("Project", {"company": company, "status": ("!=", "Cancelled")}, "name")
	if not project:
		project = frappe.db.get_value("Project", {"status": ("!=", "Cancelled")}, "name")

	return {
		**ctx,
		"accounts": others,
		"receivable": others[0],
		"payable": others[3],
		"party_receivable": receivable,
		"customer": customer if je_party else None,
		"customer_period_debit": party_amount if je_party else 0.0,
		"supplier": None,
		"cost_center": cost_center,
		"project": project,
		"je_a": je_a,
		"je_b": je_b,
		"je_party": je_party,
		"amount_a": 1000.0,
		"amount_b": 2500.0,
		"amount_party": party_amount if je_party else 0.0,
	}


def direct_gl_period_totals(
	company: str,
	from_date,
	to_date,
	*,
	account: str | list[str] | None = None,
	party_type: str | None = None,
	party: str | None = None,
	voucher_type: str | None = None,
	voucher_no: str | list[str] | None = None,
	cost_center: str | None = None,
	project: str | None = None,
	dimension_filters: dict | None = None,
	currency: str | None = None,
	include_opening_entries: int = 1,
	include_cancelled_entries: int = 0,
	include_period_closing_vouchers: int = 0,
) -> dict[str, float]:
	"""Authoritative GL period sums with explicit WHERE (not QuerySpec)."""
	conditions = [
		"company=%(company)s",
		"posting_date between %(from_date)s and %(to_date)s",
	]
	values = {
		"company": company,
		"from_date": from_date,
		"to_date": to_date,
	}
	if not cint(include_cancelled_entries):
		conditions.append("is_cancelled=0")
	if not cint(include_period_closing_vouchers):
		conditions.append("voucher_type != 'Period Closing Voucher'")
	if not cint(include_opening_entries):
		conditions.append("is_opening='No'")
	if account:
		if isinstance(account, (list, tuple, set)):
			accounts = [str(item) for item in account if item not in (None, "")]
			if not accounts:
				conditions.append("1=0")
			else:
				conditions.append("account in %(account)s")
				values["account"] = accounts
		else:
			conditions.append("account=%(account)s")
			values["account"] = account
	if party_type:
		conditions.append("party_type=%(party_type)s")
		values["party_type"] = party_type
	if party:
		conditions.append("party=%(party)s")
		values["party"] = party
	if voucher_type:
		conditions.append("voucher_type=%(voucher_type)s")
		values["voucher_type"] = voucher_type
	if voucher_no:
		if isinstance(voucher_no, (list, tuple, set)):
			vouchers = [str(item) for item in voucher_no if item not in (None, "")]
			if not vouchers:
				conditions.append("1=0")
			else:
				conditions.append("voucher_no in %(voucher_no)s")
				values["voucher_no"] = vouchers
		else:
			conditions.append("voucher_no=%(voucher_no)s")
			values["voucher_no"] = voucher_no
	if cost_center:
		conditions.append("cost_center=%(cost_center)s")
		values["cost_center"] = cost_center
	if project:
		conditions.append("project=%(project)s")
		values["project"] = project
	if currency:
		conditions.append("account_currency=%(currency)s")
		values["currency"] = currency
	for fieldname, value in (dimension_filters or {}).items():
		if value in (None, ""):
			continue
		# fieldname comes from Accounting Dimension metadata, not client SQL.
		conditions.append(f"`{fieldname}`=%({fieldname})s")
		values[fieldname] = value

	row = frappe.db.sql(
		f"""
		select coalesce(sum(debit),0) as period_debit, coalesce(sum(credit),0) as period_credit
		from `tabGL Entry`
		where {" and ".join(conditions)}
		""",
		values,
		as_dict=True,
	)[0]
	return {
		"period_debit": flt(row.period_debit),
		"period_credit": flt(row.period_credit),
	}


def direct_gl_opening_totals(
	company: str,
	from_date,
	to_date,
	*,
	account: str | list[str] | None = None,
	party_type: str | None = None,
	party: str | None = None,
	voucher_type: str | None = None,
	voucher_no: str | list[str] | None = None,
	cost_center: str | None = None,
	project: str | None = None,
	dimension_filters: dict | None = None,
	currency: str | None = None,
	include_opening_entries: int = 1,
	include_cancelled_entries: int = 0,
	include_period_closing_vouchers: int = 0,
) -> dict[str, float]:
	conditions = [
		"company=%(company)s",
	]
	if cint(include_opening_entries):
		conditions.append("posting_date < %(from_date)s")
	else:
		conditions.append("posting_date < %(from_date)s and is_opening='No'")
	values = {
		"company": company,
		"from_date": from_date,
		"to_date": to_date,
	}
	if not cint(include_cancelled_entries):
		conditions.append("is_cancelled=0")
	if not cint(include_period_closing_vouchers):
		conditions.append("voucher_type != 'Period Closing Voucher'")
	if account:
		if isinstance(account, (list, tuple, set)):
			accounts = [str(item) for item in account if item not in (None, "")]
			if not accounts:
				conditions.append("1=0")
			else:
				conditions.append("account in %(account)s")
				values["account"] = accounts
		else:
			conditions.append("account=%(account)s")
			values["account"] = account
	if party_type:
		conditions.append("party_type=%(party_type)s")
		values["party_type"] = party_type
	if party:
		conditions.append("party=%(party)s")
		values["party"] = party
	if voucher_type:
		conditions.append("voucher_type=%(voucher_type)s")
		values["voucher_type"] = voucher_type
	if voucher_no:
		if isinstance(voucher_no, (list, tuple, set)):
			vouchers = [str(item) for item in voucher_no if item not in (None, "")]
			if not vouchers:
				conditions.append("1=0")
			else:
				conditions.append("voucher_no in %(voucher_no)s")
				values["voucher_no"] = vouchers
		else:
			conditions.append("voucher_no=%(voucher_no)s")
			values["voucher_no"] = voucher_no
	if cost_center:
		conditions.append("cost_center=%(cost_center)s")
		values["cost_center"] = cost_center
	if project:
		conditions.append("project=%(project)s")
		values["project"] = project
	if currency:
		conditions.append("account_currency=%(currency)s")
		values["currency"] = currency
	for fieldname, value in (dimension_filters or {}).items():
		if value in (None, ""):
			continue
		conditions.append(f"`{fieldname}`=%({fieldname})s")
		values[fieldname] = value

	where_sql = " and ".join(conditions)
	if account:
		row = frappe.db.sql(
			f"""
			select coalesce(sum(debit),0) as opening_debit, coalesce(sum(credit),0) as opening_credit
			from `tabGL Entry`
			where {where_sql}
			""",
			values,
			as_dict=True,
		)[0]
		debit = flt(row.opening_debit)
		credit = flt(row.opening_credit)
		if debit > credit:
			return {"opening_debit": debit - credit, "opening_credit": 0.0}
		return {"opening_debit": 0.0, "opening_credit": credit - debit}

	rows = frappe.db.sql(
		f"""
		select coalesce(sum(debit),0) as opening_debit, coalesce(sum(credit),0) as opening_credit
		from `tabGL Entry`
		where {where_sql}
		group by account
		""",
		values,
		as_dict=True,
	)
	opening_debit = 0.0
	opening_credit = 0.0
	for row in rows:
		debit = flt(row.opening_debit)
		credit = flt(row.opening_credit)
		if debit > credit:
			opening_debit += debit - credit
		else:
			opening_credit += credit - debit
	return {"opening_debit": opening_debit, "opening_credit": opening_credit}


def full_measures_from_opening_period(opening: dict, period: dict) -> dict[str, float]:
	"""Build the seven measures AE reports; max abs diff vs AE must be 0."""
	od = flt(opening.get("opening_debit"))
	oc = flt(opening.get("opening_credit"))
	pd = flt(period.get("period_debit"))
	pc = flt(period.get("period_credit"))
	closing_net = od - oc + pd - pc
	return {
		"opening_debit": od,
		"opening_credit": oc,
		"period_debit": pd,
		"period_credit": pc,
		"closing_debit": max(closing_net, 0.0),
		"closing_credit": abs(min(closing_net, 0.0)),
		"net_balance": closing_net,
	}

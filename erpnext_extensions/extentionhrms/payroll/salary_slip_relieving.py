# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Salary Slip extension: exclusive relieving date for payment days.

Wired via ``extend_doctype_class`` (composes with stock HRMS ``SalarySlip``;
does not replace the controller). Relieving date is the first non-payable day.

Also aligns Leave-based LWP and attendance "days outside period" so the
relieving day is not deducted twice after the payable window is shortened.
"""

from __future__ import annotations

from frappe.utils import add_days, cint, date_diff, flt, getdate

from hrms.payroll.doctype.salary_slip.salary_slip import (
	HALF_DAY_HOLIDAY_FRACTION,
	get_lwp_or_ppl_for_date_range,
)

from erpnext_extensions.extentionhrms.payroll.exclusive_relieving import (
	exclusive_payable_end_date,
)


class SalarySlipExclusiveRelieving:
	"""Mixin applied through ``extend_doctype_class`` on Salary Slip."""

	@property
	def actual_end_date(self):
		"""Last payable date: ``min(end, relieving - 1)`` when relieving is in period.

		Unlike HRMS Core (inclusive relieving, and only when ``relieving < end``),
		this treats relieving on the last day of the period the same way and may
		return a date before ``start_date`` when relieving equals the period start
		(zero payment days).
		"""
		return exclusive_payable_end_date(self.start_date, self.end_date, self.relieving_date)

	def get_payment_days(self, include_holidays_in_total_working_days):
		# Zero-day short-circuit before holiday lookups with inverted ranges.
		if getdate(self.actual_end_date) < getdate(self.actual_start_date):
			return 0

		payment_days = super().get_payment_days(include_holidays_in_total_working_days)
		return max(0, flt(payment_days))

	def _get_days_outside_period(
		self,
		include_holidays_in_total_working_days: bool,
		holidays: list | None = None,
		half_day_holidays: list | None = None,
	):
		"""Days before DOJ or after the *payable* end (exclusive relieving).

		Core starts the post-employment window at ``relieving_date + 1``, which
		assumes inclusive relieving. With exclusive relieving the first
		non-payable day is ``relieving_date`` itself (== ``actual_end_date + 1``).
		"""
		half_day_holidays = half_day_holidays or []
		holidays = holidays or []

		def _get_days(start_date, end_date):
			no_of_days = date_diff(end_date, start_date) + 1
			if no_of_days <= 0:
				return 0

			if include_holidays_in_total_working_days:
				return no_of_days

			days = 0
			end_date = getdate(end_date)
			for day in range(no_of_days):
				date = add_days(end_date, -day)
				if date in half_day_holidays:
					days += HALF_DAY_HOLIDAY_FRACTION
				elif date not in holidays:
					days += 1
			return days

		days = 0
		if self.actual_start_date != self.start_date:
			days += _get_days(self.start_date, add_days(self.joining_date, -1))

		if getdate(self.actual_end_date) != getdate(self.end_date):
			days += _get_days(add_days(getdate(self.actual_end_date), 1), self.end_date)

		return days

	def _get_number_of_holidays(self, holidays: list | None = None) -> float:
		"""Guard inverted payable windows (relieving on period start)."""
		if getdate(self.actual_end_date) < getdate(self.actual_start_date):
			return 0
		return super()._get_number_of_holidays(holidays)

	def calculate_lwp_or_ppl_based_on_leave_application(
		self, holidays, half_day_holidays, working_days_list, daily_wages_fraction_for_half_day
	):
		"""Same as HRMS Core, but skip LWP on/after relieving (exclusive).

		Core breaks when ``d > relieving_date``, which still counts LWP on the
		relieving day. After exclusive ``get_payment_days``, that would double-count.
		"""
		lwp = 0
		leaves = get_lwp_or_ppl_for_date_range(
			self.employee,
			self.start_date,
			self.end_date,
		)

		for d in working_days_list:
			if self.relieving_date and getdate(d) >= getdate(self.relieving_date):
				break

			leave = leaves.get(d)
			if not leave:
				continue

			if not leave.include_holiday and getdate(d) in holidays:
				continue

			equivalent_lwp_count = 0
			fraction_of_daily_salary_per_leave = flt(leave.fraction_of_daily_salary_per_leave)

			is_half_day_leave = False
			if cint(leave.half_day) and (leave.half_day_date == d or leave.from_date == leave.to_date):
				is_half_day_leave = True

			equivalent_lwp_count = (1 - daily_wages_fraction_for_half_day) if is_half_day_leave else 1

			if cint(leave.is_ppl):
				equivalent_lwp_count *= (
					(1 - fraction_of_daily_salary_per_leave) if fraction_of_daily_salary_per_leave else 1
				)

			if not leave.include_holiday and getdate(d) in half_day_holidays:
				equivalent_lwp_count *= HALF_DAY_HOLIDAY_FRACTION

			lwp += equivalent_lwp_count

		return lwp

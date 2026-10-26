# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Unit tests for exclusive relieving-date payment-day math.

Plain ``pytest`` (no Frappe)::

    pytest erpnext_extensions/extentionhrms/tests/test_exclusive_relieving.py
"""

from __future__ import annotations

from datetime import date

import pytest

from erpnext_extensions.extentionhrms.payroll.exclusive_relieving import (
	calendar_payment_days,
	exclusive_payable_end_date,
	is_first_day_relieving,
)

# Mehr 1405 payroll window (Gregorian)
MEHR_START = date(2026, 9, 23)  # 1405/07/01
MEHR_END = date(2026, 10, 22)  # 1405/07/30
REL_6_MEHR = date(2026, 9, 28)  # 1405/07/06
REL_1_MEHR = MEHR_START
REL_30_MEHR = MEHR_END


class TestExclusivePayableEndDate:
	def test_relieving_6_mehr(self):
		assert exclusive_payable_end_date(MEHR_START, MEHR_END, REL_6_MEHR) == date(2026, 9, 27)

	def test_relieving_1_mehr(self):
		# Payable end is the day before period start → zero payment days with start.
		assert exclusive_payable_end_date(MEHR_START, MEHR_END, REL_1_MEHR) == date(2026, 9, 22)

	def test_relieving_30_mehr(self):
		assert exclusive_payable_end_date(MEHR_START, MEHR_END, REL_30_MEHR) == date(2026, 10, 21)

	def test_relieving_after_period(self):
		after = date(2026, 10, 23)
		assert exclusive_payable_end_date(MEHR_START, MEHR_END, after) == MEHR_END

	def test_relieving_before_period(self):
		# Outside the in-period branch → period end (eligibility handled elsewhere).
		before = date(2026, 9, 22)
		assert exclusive_payable_end_date(MEHR_START, MEHR_END, before) == MEHR_END

	def test_no_relieving(self):
		assert exclusive_payable_end_date(MEHR_START, MEHR_END, None) == MEHR_END

	def test_iso_strings_accepted(self):
		assert exclusive_payable_end_date("2026-09-23", "2026-10-22", "2026-09-28") == date(
			2026, 9, 27
		)


class TestCalendarPaymentDays:
	def test_emp_543_window(self):
		# 23–27 Sep inclusive = 5
		assert calendar_payment_days(MEHR_START, date(2026, 9, 27)) == 5

	def test_core_inclusive_would_be_six(self):
		assert calendar_payment_days(MEHR_START, REL_6_MEHR) == 6

	def test_first_day_relieving_zero(self):
		payable_end = exclusive_payable_end_date(MEHR_START, MEHR_END, REL_1_MEHR)
		assert calendar_payment_days(MEHR_START, payable_end) == 0

	def test_last_day_relieving(self):
		payable_end = exclusive_payable_end_date(MEHR_START, MEHR_END, REL_30_MEHR)
		# Full Mehr with holidays included would be 30 calendar days; exclusive → 29
		assert calendar_payment_days(MEHR_START, payable_end) == 29

	def test_join_and_relieve_same_day(self):
		day = date(2026, 10, 1)
		payable_end = exclusive_payable_end_date(MEHR_START, MEHR_END, day)
		assert calendar_payment_days(day, payable_end) == 0

	def test_never_negative(self):
		assert calendar_payment_days(MEHR_START, date(2026, 9, 22)) == 0

	def test_no_relieving_full_period(self):
		assert calendar_payment_days(MEHR_START, MEHR_END) == 30


class TestFirstDayRelievingFlag:
	def test_true_on_start(self):
		assert is_first_day_relieving(MEHR_START, REL_1_MEHR) is True

	def test_false_mid_period(self):
		assert is_first_day_relieving(MEHR_START, REL_6_MEHR) is False

	def test_false_when_missing(self):
		assert is_first_day_relieving(MEHR_START, None) is False


class TestMehrScenarioMatrix:
	"""Scenario table from the Phase 2 brief (calendar days, holidays included)."""

	@pytest.mark.parametrize(
		"relieving,expected_days",
		[
			(REL_6_MEHR, 5),
			(REL_1_MEHR, 0),
			(REL_30_MEHR, 29),
			(None, 30),
			(date(2026, 10, 23), 30),  # after period
		],
	)
	def test_payment_days_matrix(self, relieving, expected_days):
		payable_end = exclusive_payable_end_date(MEHR_START, MEHR_END, relieving)
		assert calendar_payment_days(MEHR_START, payable_end) == expected_days

	def test_mid_month_joining_inclusive(self):
		# Joining remains inclusive: actual_start = joining when in period.
		joining = date(2026, 10, 1)
		payable_end = exclusive_payable_end_date(MEHR_START, MEHR_END, None)
		assert calendar_payment_days(joining, payable_end) == 22

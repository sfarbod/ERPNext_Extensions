# Copyright (c) 2026, ERPNext Extensions contributors
"""5.7.18 — ULP skip draft superseded by integer VR enforcement.

Kept as a thin redirect so historical suite discovery still resolves; assertions
live in ``test_integer_vr_enforcement_v5719``.
"""

from __future__ import annotations

import unittest

from erpnext_extensions.iran_accounting.tests.test_integer_vr_enforcement_v5719 import (
	TestIntegerVrAlignV5719,
	TestIntegerVrClassifyV5719,
)


class TestUvrUlpDriftSupersededByV5719(TestIntegerVrClassifyV5719, TestIntegerVrAlignV5719):
	"""Alias — ULP tolerance removed; integer Class A path is mandatory."""


if __name__ == "__main__":
	unittest.main()

# Copyright (c) 2026, Farbod Siyahpoosh and contributors

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class VoucherFilter:
	voucher_type: str | None = None
	voucher_no: str | None = None
	against_voucher_type: str | None = None
	against_voucher_no: str | None = None
	reference_no: str | None = None


@dataclass
class AccountingFilter:
	account: str | list[str] | None = None
	party_type: str | None = None
	party: str | list[str] | None = None


@dataclass
class CurrencyFilter:
	currency_type: str = "account_currency"
	currency: str | None = None


@dataclass
class StatusFilter:
	include_opening_entries: bool = True
	include_cancelled_entries: bool = False
	include_default_finance_book_entries: bool = True
	include_period_closing_vouchers: bool = False


@dataclass
class InventoryFilter:
	item_group: str | list[str] | None = None
	item: str | list[str] | None = None
	warehouse: str | list[str] | None = None
	# Set by warehouse/inventory-account drill — resolves to warehouses via ERPNext map.
	inventory_account: str | list[str] | None = None


@dataclass
class DocumentScope:
	company: str = ""
	fiscal_year: str | None = None
	from_date: Any = None
	to_date: Any = None
	finance_book: str | None = None
	voucher: VoucherFilter = field(default_factory=VoucherFilter)
	accounting: AccountingFilter = field(default_factory=AccountingFilter)
	accounting_dimensions: dict[str, Any] = field(default_factory=dict)
	currency: CurrencyFilter = field(default_factory=CurrencyFilter)
	status: StatusFilter = field(default_factory=StatusFilter)
	inventory: InventoryFilter = field(default_factory=InventoryFilter)
	hide_zero_rows: bool = True


@dataclass
class AccountScope:
	mode: str = "tree"
	selected_account: str | None = None
	virtual_row_key: str | None = None
	is_virtual_group: bool = False
	level_sequence: int | None = None
	tree_root_account: str | None = None


@dataclass
class PartyScope:
	party_type: str | None = None
	selected_party: str | None = None


@dataclass
class DimensionScope:
	dimension_type: str | None = None
	selected_dimension_value: str | None = None


@dataclass
class UnifiedPartyScope:
	selected_unified_party: str | None = None
	include_unmapped: bool = False


@dataclass
class VoucherScope:
	voucher_type: str | None = None
	voucher_no: str | None = None


@dataclass
class ItemGroupScope:
	selected_item_group: str | None = None


@dataclass
class ItemScope:
	selected_item: str | None = None


@dataclass
class PaginationState:
	page: int = 1
	page_size: int = 50
	sort_field: str = "display_code"
	sort_order: str = "asc"


@dataclass
class AnalysisContext:
	view_axis: str = "account_level"
	detail_mode: str = "summary"
	level_sequence: int | None = None
	account_scope: AccountScope = field(default_factory=AccountScope)
	party_scope: PartyScope = field(default_factory=PartyScope)
	unified_party_scope: UnifiedPartyScope = field(default_factory=UnifiedPartyScope)
	dimension_scope: DimensionScope = field(default_factory=DimensionScope)
	voucher_scope: VoucherScope = field(default_factory=VoucherScope)
	item_group_scope: ItemGroupScope = field(default_factory=ItemGroupScope)
	item_scope: ItemScope = field(default_factory=ItemScope)
	pagination: PaginationState = field(default_factory=PaginationState)


@dataclass
class AccountExplorerQuerySpec:
	document_scope: DocumentScope
	analysis: AnalysisContext
	presentation_currency: str = "company"
	included_account_names: list[str] | None = None
	resolved_member_tuples: list[tuple[str, str]] | None = None

	@property
	def company(self) -> str:
		return self.document_scope.company

	@property
	def fiscal_year(self) -> str | None:
		return self.document_scope.fiscal_year

	@property
	def from_date(self):
		return self.document_scope.from_date

	@property
	def to_date(self):
		return self.document_scope.to_date

	@property
	def finance_book(self) -> str | None:
		return self.document_scope.finance_book

	@property
	def hide_zero_rows(self) -> bool:
		return self.document_scope.hide_zero_rows

	@property
	def include_cancelled_entries(self) -> bool:
		return self.document_scope.status.include_cancelled_entries

	@property
	def include_opening_entries(self) -> bool:
		return self.document_scope.status.include_opening_entries

	@property
	def include_default_book_entries(self) -> bool:
		return self.document_scope.status.include_default_finance_book_entries

	@property
	def include_period_closing_vouchers(self) -> bool:
		return self.document_scope.status.include_period_closing_vouchers

	@property
	def view_axis(self) -> str:
		return self.analysis.view_axis

	@property
	def detail_mode(self) -> str:
		return self.analysis.detail_mode

	@property
	def level_sequence(self) -> int | None:
		return self.analysis.level_sequence

	@property
	def account_scope(self) -> AccountScope:
		return self.analysis.account_scope

	@property
	def party_scope(self) -> PartyScope:
		return self.analysis.party_scope

	@property
	def unified_party_scope(self) -> UnifiedPartyScope:
		return self.analysis.unified_party_scope

	@property
	def dimension_scope(self) -> DimensionScope:
		return self.analysis.dimension_scope

	@property
	def voucher_scope(self) -> VoucherScope:
		return self.analysis.voucher_scope

	@property
	def item_group_scope(self) -> ItemGroupScope:
		return self.analysis.item_group_scope

	@property
	def item_scope(self) -> ItemScope:
		return self.analysis.item_scope

	@property
	def pagination(self) -> PaginationState:
		return self.analysis.pagination

	def requires_bounded_dates(self) -> bool:
		return bool(self.company and self.from_date and self.to_date)

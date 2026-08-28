app_name = "erpnext_extensions"
app_title = "ERPNext Extensions"
app_publisher = "Farbod Siyahpoosh"
app_description = "Extensions for ERPNext v15 — decimal precision, localization, Jalali, etc."
app_email = "sfarbod@gmail.com"
app_license = "mit"

# Desk boot: expose Payment Request workflow flag for client ``has_workflow`` alignment (see ``desk_boot.py``).
boot_session = ["erpnext_extensions.desk_boot.extend_bootinfo"]

# Apps
# ------------------

# required_apps = []

# Each item in the list will be shown as an app in the apps page
# add_to_apps_screen = [
# 	{
# 		"name": "erpnext_extensions",
# 		"logo": "/assets/erpnext_extensions/logo.png",
# 		"title": "ERPNext Extensions",
# 		"route": "/erpnext_extensions",
# 		"has_permission": "erpnext_extensions.api.permission.has_app_permission"
# 	}
# ]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
app_include_css = "/assets/erpnext_extensions/css/petty_management_desk.css"
# app_include_js = "/assets/erpnext_extensions/js/erpnext_extensions.js"

# include js, css files in header of web template
# web_include_css = "/assets/erpnext_extensions/css/erpnext_extensions.css"
# web_include_js = "/assets/erpnext_extensions/js/erpnext_extensions.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "erpnext_extensions/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
doctype_js = {
	# NOTE: Cheque Opening Import already autoloads its doctype JS from
	# `cheque_management/doctype/cheque_opening_import/cheque_opening_import.js`.
	# Keep this hook for extra assets only (avoid double-loading the same file).
	"Cheque Opening Import": [
		"public/js/cheque_opening_import_inline_template.js",
		"public/js/cheque_opening_import_delete_pdc.js",
	],
	"Post Dated Cheque": [
		"public/js/cheque_opening_import_delete_pdc.js",
	],
	"Payment Request": [
		"public/js/pdc_create_from_source.js",
		"public/js/pdc_settlement_summary.js",
		"public/js/payment_request.js",
	],
	"Sales Invoice": [
		"public/js/pdc_create_from_source.js",
		"public/js/pdc_settlement_summary.js",
		"public/js/pdc_advance_on_invoice.js",
	],
	"Purchase Invoice": [
		"public/js/pdc_create_from_source.js",
		"public/js/pdc_settlement_summary.js",
		"public/js/pdc_advance_on_invoice.js",
	],
	"Purchase Order": [
		"public/js/pdc_create_from_order.js",
	],
	"Sales Order": [
		"public/js/pdc_create_from_order.js",
	],
	"Facility": [
		"facility_management/public/js/facility_dimension_link_queries.js",
		"facility_management/public/js/facility_settings_defaults.js",
		"facility_management/public/js/facility_je_preview_dialog.js",
	],
	"Facility Repayment": [
		"facility_management/public/js/facility_settings_defaults.js",
		"facility_management/public/js/facility_je_preview_dialog.js",
	],
	"Facility Settings": [
		"facility_management/public/js/facility_dimension_link_queries.js",
	],
	"Stock Entry": [
		"consignment_stock/public/js/stock_entry_consignment.js",
		"consignment_stock/public/js/stock_entry_material_loan.js",
	],
	# v16 hides the stock Corrective Job Card button whenever the card declares
	# a finished_good, which is every card under semi-finished goods tracking.
	"Job Card": [
		"stock_extensions/public/js/job_card_corrective_operation.js",
	],
	"Stock Entry Type": [
		"consignment_stock/public/js/stock_entry_type_consignment.js",
		"consignment_stock/public/js/stock_entry_type_material_loan.js",
	],
	# Autoloads doctype JS; extras keep custom buttons clear_actions_menu-safe.
	"PM Request": "public/js/pm_desk_workflow_actions.js",
	"PM Clearance": "public/js/pm_desk_workflow_actions.js",
	# v4.6.7: append PM Request to ignore_doctypes_on_cancel_all (Desk Cancel All).
	"Payment Entry": "public/js/payment_entry_pm_request_cancel.js",
}
doctype_list_js = {
	"PM Clearance": "erpnext_extensions/petty_management/doctype/pm_clearance/pm_clearance_list.js",
	"Post Dated Cheque": "erpnext_extensions/cheque_management/doctype/post_dated_cheque/post_dated_cheque_list.js",
}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Svg Icons
# ------------------
# include app icons in desk
# app_include_icons = "erpnext_extensions/public/icons.svg"

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# role_home_page = {
# 	"Role": "home_page"
# }

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# Jinja
# ----------

# add methods and filters to jinja environment
# jinja = {
# 	"methods": "erpnext_extensions.utils.jinja_methods",
# 	"filters": "erpnext_extensions.utils.jinja_filters"
# }

# Installation
# ------------

# before_install = "erpnext_extensions.install.before_install"
# after_install = "erpnext_extensions.install.after_install"

# Re-apply Cheque Management links on Payments `Workspace Sidebar` after migrate completes
# (Frappe may remove patch-created Workspaces during orphan cleanup in the same migrate).
after_migrate = [
	"erpnext_extensions.cheque_management.payments_sidebar.after_migrate",
	"erpnext_extensions.petty_management.desk_visibility.after_migrate",
	"erpnext_extensions.cheque_management.pdc_accounting_dimensions.after_migrate",
	"erpnext_extensions.facility_management.facility_accounting_dimensions.after_migrate",
	"erpnext_extensions.iran_accounting.integration.bootstrap.apply",
	"erpnext_extensions.extentionhrms.install.after_migrate",
	"erpnext_extensions.consignment_stock.install.after_migrate",
	"erpnext_extensions.asset_usage_depreciation.install.after_migrate",
]

# ERPNext injects Accounting Dimension custom fields onto these DocTypes (see Accounting Dimension on_update).
accounting_dimension_doctypes = [
	"Post Dated Cheque",
	"Asset Request",
	"Asset Request Item",
]

# Uninstallation
# ------------

# before_uninstall = "erpnext_extensions.uninstall.before_uninstall"
# after_uninstall = "erpnext_extensions.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "erpnext_extensions.utils.before_app_install"
# after_app_install = "erpnext_extensions.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "erpnext_extensions.utils.before_app_uninstall"
# after_app_uninstall = "erpnext_extensions.utils.after_app_uninstall"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "erpnext_extensions.notifications.get_notification_config"

# Permissions
# -----------
# Permissions evaluated in scripted ways

permission_query_conditions = {
	"PM Request": "erpnext_extensions.petty_management.permissions.pm_request_permission_query_conditions",
	"PM Clearance": "erpnext_extensions.petty_management.permissions.pm_clearance_permission_query_conditions",
	"Asset Request": "erpnext_extensions.asset_usage_depreciation.permissions.asset_request_permission_query_conditions",
}

has_permission = {
	"PM Request": "erpnext_extensions.petty_management.permissions.has_pm_request_permission",
	"PM Clearance": "erpnext_extensions.petty_management.permissions.has_pm_clearance_permission",
	"Asset Request": "erpnext_extensions.asset_usage_depreciation.permissions.has_asset_request_permission",
}

# DocType Class
# ---------------
# Override standard doctype classes

# override_doctype_class = {
# 	"ToDo": "custom_app.overrides.CustomToDo"
# }

# Group the HRMS payroll accrual entry by (Account, Cost Center, Department) so
# it satisfies the mandatory-for-P&L Department accounting dimension. See
# ``extentionhrms/payroll_entry_override.py``.
override_doctype_class = {
	"Payroll Entry": (
		"erpnext_extensions.extentionhrms.payroll_entry_override.PayrollEntryWithAccountingDimensions"
	),
	# Hourly leave (مرخصی ساعتی): converts a single-day time range into a
	# fractional ``total_leave_days`` so it deducts from the same entitlement
	# balance. See ``extentionhrms/leave_application_override.py``.
	"Leave Application": (
		"erpnext_extensions.extentionhrms.leave_application_override.LeaveApplicationWithHourlyLeave"
	),
}

# Document Events
# ---------------
# Hook on document methods and events

# Document Events
# ---------------
# Hook on document methods and events

doc_events = {
	# Track-Semi-Finished-Goods guards (stock_extensions/job_card_semi_fg.py), still needed on 16.33:
	#  * after_insert — same semi_fg_bom on every card so per-lot operating cost is not double-counted
	#  * validate     — refuse a Pending Qty on a completed cycle (locks the operation's remainder)
	"Job Card": {
		"after_insert": "erpnext_extensions.stock_extensions.job_card_semi_fg.after_insert",
		"validate": "erpnext_extensions.stock_extensions.job_card_semi_fg.validate",
	},
	"GL Entry": {
		"after_insert": "erpnext_extensions.iran_accounting.account_explorer.cache_revision.bump_accounting_revision",
		"on_update": "erpnext_extensions.iran_accounting.account_explorer.cache_revision.bump_accounting_revision",
		"on_trash": "erpnext_extensions.iran_accounting.account_explorer.cache_revision.bump_accounting_revision",
	},
	"Period Closing Voucher": {
		"on_submit": "erpnext_extensions.iran_accounting.account_explorer.cache_revision.bump_accounting_revision",
		"on_cancel": "erpnext_extensions.iran_accounting.account_explorer.cache_revision.bump_accounting_revision",
	},
	"Account Closing Balance": {
		"on_update": "erpnext_extensions.iran_accounting.account_explorer.cache_revision.bump_accounting_revision",
		"on_trash": "erpnext_extensions.iran_accounting.account_explorer.cache_revision.bump_accounting_revision",
	},
	"Accounting Dimension": {
		"on_update": "erpnext_extensions.iran_accounting.account_explorer.cache_revision.bump_accounting_revision",
		"on_trash": "erpnext_extensions.iran_accounting.account_explorer.cache_revision.bump_accounting_revision",
	},
	"Asset Value Adjustment": {
		"on_submit": "erpnext_extensions.asset_usage_depreciation.integration_hooks.on_asset_value_adjustment_submit",
		"on_cancel": "erpnext_extensions.asset_usage_depreciation.integration_hooks.on_asset_value_adjustment_cancel",
	},
	"Asset Repair": {
		"on_submit": "erpnext_extensions.asset_usage_depreciation.integration_hooks.on_asset_repair_submit",
		"on_cancel": "erpnext_extensions.asset_usage_depreciation.integration_hooks.on_asset_repair_cancel",
	},
	"Asset": {
		"on_submit": "erpnext_extensions.asset_usage_depreciation.integration_hooks.on_asset_submit",
	},
	"Asset Movement": {
		"on_cancel": "erpnext_extensions.asset_usage_depreciation.integration_hooks.on_asset_movement_cancel",
	},
	"Material Request": {
		"on_cancel": "erpnext_extensions.asset_usage_depreciation.integration_hooks.on_material_request_cancel",
	},
	"PM Clearance": {
		"onload": "erpnext_extensions.petty_management.clearance_onload.sync_pm_clearance_on_load",
	},
	"Journal Entry": {
		"on_submit": [
			"erpnext_extensions.petty_management.journal_entry_hooks.on_journal_entry_submit",
			"erpnext_extensions.consignment_stock.journal_entry_hooks.on_submit",
			"erpnext_extensions.consignment_stock.material_loan.journal_entry_hooks.on_submit",
			# Repair Depreciation Entry → ADS row link when core date==str compare fails
			"erpnext_extensions.asset_usage_depreciation.services.je_link.ensure_depreciation_schedule_je_link",
		],
		"before_cancel": [
			"erpnext_extensions.petty_management.journal_entry_hooks.on_journal_entry_before_cancel",
			"erpnext_extensions.consignment_stock.journal_entry_hooks.before_cancel",
			"erpnext_extensions.consignment_stock.material_loan.journal_entry_hooks.before_cancel",
		],
		"on_cancel": [
			"erpnext_extensions.consignment_stock.journal_entry_hooks.on_cancel",
			"erpnext_extensions.consignment_stock.material_loan.journal_entry_hooks.on_cancel",
		],
		"on_trash": [
			"erpnext_extensions.consignment_stock.journal_entry_hooks.on_trash",
			"erpnext_extensions.consignment_stock.material_loan.journal_entry_hooks.on_trash",
		],
	},
	"Payment Entry": {
		"validate": "erpnext_extensions.cheque_management.payment_entry_pdc_validation.validate_payment_entry_against_pdc_settlement",
		"after_insert": "erpnext_extensions.petty_management.payment_entry_hooks.on_payment_entry_after_insert",
		"before_cancel": [
			"erpnext_extensions.petty_management.payment_entry_hooks.on_payment_entry_before_cancel",
		],
		"on_submit": [
			"erpnext_extensions.cheque_management.pdc_payment_request_status.on_payment_entry_changed",
			"erpnext_extensions.petty_management.payment_entry_hooks.on_payment_entry_submit",
		],
		"on_cancel": [
			"erpnext_extensions.cheque_management.pdc_payment_request_status.on_payment_entry_changed",
			"erpnext_extensions.petty_management.payment_entry_hooks.on_payment_entry_cancel",
		],
		"on_trash": "erpnext_extensions.petty_management.payment_entry_hooks.on_payment_entry_trash",
		"after_delete": "erpnext_extensions.petty_management.payment_entry_hooks.on_payment_entry_after_delete",
	},
	"Post Dated Cheque": {
		"on_submit": [
			"erpnext_extensions.cheque_management.pdc_payment_request_status.on_post_dated_cheque_changed",
		],
		"on_cancel": [
			"erpnext_extensions.cheque_management.pdc_payment_request_status.on_post_dated_cheque_changed",
		],
		"on_update_after_submit": [
			"erpnext_extensions.cheque_management.pdc_payment_request_status.on_post_dated_cheque_changed",
		],
	},
	"Payment Request": {
		"validate": "erpnext_extensions.cheque_management.pdc_payment_request_eligibility.validate_payment_request_invoice_ceiling_on_save",
	},
	"Purchase Invoice": {
		"validate": [
			"erpnext_extensions.cheque_management.pdc_invoice_advance_application.on_invoice_validate",
			"erpnext_extensions.iran_accounting.accounts_invoice.round_irr_invoice_totals",
		],
		"before_submit": [
			"erpnext_extensions.cheque_management.pdc_invoice_advance_application.before_invoice_submit",
			"erpnext_extensions.iran_accounting.accounts_invoice.round_irr_invoice_totals",
		],
		"on_submit": "erpnext_extensions.cheque_management.pdc_invoice_advance_application.on_invoice_submit",
		"on_cancel": "erpnext_extensions.cheque_management.pdc_invoice_advance_application.on_invoice_cancel",
	},
	"Sales Invoice": {
		"validate": [
			"erpnext_extensions.cheque_management.pdc_invoice_advance_application.on_invoice_validate",
			"erpnext_extensions.iran_accounting.accounts_invoice.round_irr_invoice_totals",
		],
		"before_submit": [
			"erpnext_extensions.cheque_management.pdc_invoice_advance_application.before_invoice_submit",
			"erpnext_extensions.iran_accounting.accounts_invoice.round_irr_invoice_totals",
		],
		"on_submit": [
			"erpnext_extensions.cheque_management.pdc_invoice_advance_application.on_invoice_submit",
			"erpnext_extensions.asset_usage_depreciation.integration_hooks.on_sales_invoice_submit",
		],
		"on_cancel": [
			"erpnext_extensions.cheque_management.pdc_invoice_advance_application.on_invoice_cancel",
			"erpnext_extensions.asset_usage_depreciation.integration_hooks.on_sales_invoice_cancel",
		],
	},
	"GL Entry": {
		"validate": "erpnext_extensions.iran_accounting.gl_entry.validate_gl_entry",
		"before_insert": "erpnext_extensions.iran_accounting.gl_entry.before_insert_gl_entry",
	},
	"Stock Ledger Entry": {
		"validate": "erpnext_extensions.iran_accounting.stock_ledger.validate_stock_ledger_entry",
		"before_insert": "erpnext_extensions.iran_accounting.stock_ledger.before_insert_stock_ledger_entry",
		"after_insert": "erpnext_extensions.iran_accounting.stock_ledger.after_insert_stock_ledger_entry",
	},
	"Stock Entry": {
		"before_validate": [
			"erpnext_extensions.iran_accounting.stock_entry.before_validate_stock_entry",
			"erpnext_extensions.consignment_stock.stock_entry_hooks.before_validate",
			"erpnext_extensions.consignment_stock.material_loan.stock_entry_hooks.before_validate",
		],
		"validate": [
			"erpnext_extensions.iran_accounting.stock_entry.validate_stock_entry",
			"erpnext_extensions.consignment_stock.stock_entry_hooks.validate",
			"erpnext_extensions.consignment_stock.material_loan.stock_entry_hooks.validate",
		],
		"before_submit": [
			"erpnext_extensions.iran_accounting.stock_entry.before_submit_stock_entry",
			"erpnext_extensions.consignment_stock.stock_entry_hooks.before_submit",
			"erpnext_extensions.consignment_stock.material_loan.stock_entry_hooks.before_submit",
		],
		"on_submit": [
			"erpnext_extensions.iran_accounting.stock_entry.on_submit_stock_entry",
			"erpnext_extensions.consignment_stock.stock_entry_hooks.on_submit",
			"erpnext_extensions.consignment_stock.material_loan.stock_entry_hooks.on_submit",
		],
		"before_cancel": [
			"erpnext_extensions.consignment_stock.stock_entry_hooks.before_cancel",
			"erpnext_extensions.consignment_stock.material_loan.stock_entry_hooks.before_cancel",
		],
		"on_cancel": [
			"erpnext_extensions.consignment_stock.stock_entry_hooks.on_cancel",
			"erpnext_extensions.consignment_stock.material_loan.stock_entry_hooks.on_cancel",
		],
		"on_update_after_submit": [
			"erpnext_extensions.consignment_stock.material_loan.stock_entry_hooks.on_update_after_submit",
		],
	},
	"Stock Entry Type": {
		"validate": [
			"erpnext_extensions.consignment_stock.stock_entry_type.validate",
			"erpnext_extensions.consignment_stock.material_loan.stock_entry_type.validate",
		],
	},
	"Repost Item Valuation": {
		"validate": "erpnext_extensions.consignment_stock.material_loan.repost_guards.validate_repost_item_valuation",
		"on_update_after_submit": "erpnext_extensions.consignment_stock.material_loan.repost_guards.on_repost_completed",
	},
	"Landed Cost Voucher": {
		"on_submit": "erpnext_extensions.iran_accounting.stock_entry.on_submit_landed_cost_voucher",
	},
	"Stock Reconciliation": {
		"validate": "erpnext_extensions.iran_accounting.stock_reconciliation.validate_stock_reconciliation",
		"before_submit": "erpnext_extensions.iran_accounting.stock_reconciliation.before_submit_stock_reconciliation",
		"on_submit": "erpnext_extensions.iran_accounting.stock_reconciliation.on_submit_stock_reconciliation",
	},
	"Purchase Order": {
		"validate": "erpnext_extensions.iran_accounting.buying_selling.validate_purchase_order",
	},
	"Company": {
		"validate": "erpnext_extensions.iran_accounting.company_round_off_defaults.validate_company_round_off_dimension_defaults",
	},
	"Purchase Receipt": {
		"validate": "erpnext_extensions.iran_accounting.buying_selling.validate_purchase_receipt",
		"on_submit": "erpnext_extensions.asset_usage_depreciation.integration_hooks.on_purchase_receipt_submit",
	},
	"Delivery Note": {
		"validate": "erpnext_extensions.iran_accounting.buying_selling.validate_delivery_note",
	},
}

# Scheduled Tasks
# ---------------

# scheduler_events = {
# 	"all": [
# 		"erpnext_extensions.tasks.all"
# 	],
# 	"daily": [
# 		"erpnext_extensions.tasks.daily"
# 	],
# 	"hourly": [
# 		"erpnext_extensions.tasks.hourly"
# 	],
# 	"weekly": [
# 		"erpnext_extensions.tasks.weekly"
# 	],
# 	"monthly": [
# 		"erpnext_extensions.tasks.monthly"
# 	],
# }

# Testing
# -------

# before_tests = "erpnext_extensions.install.before_tests"

# Overriding Methods
# ------------------------------
#
override_whitelisted_methods = {
	"frappe.model.workflow.apply_workflow": "erpnext_extensions.petty_management.workflow_hooks.apply_workflow",
	# PDC: workflow toolbar calls can_cancel_document(doctype) with no per-doctype hook in Frappe.
	# Implementation returns False only for Post Dated Cheque and delegates others to native Frappe
	# (see cheque_management/pdc_direct_cancel_policy.py). Server enforcement: PostDatedCheque.before_cancel.
	"frappe.model.workflow.can_cancel_document": (
		"erpnext_extensions.cheque_management.pdc_direct_cancel_policy.can_cancel_document"
	),
	"frappe.desk.search.get_link_title": "erpnext_extensions.petty_management.overrides.search.get_link_title",
	# Re-apply usage factors after core scrap/restore rebuilds ADS.
	"erpnext.assets.doctype.asset.depreciation.scrap_asset": (
		"erpnext_extensions.asset_usage_depreciation.integration_hooks.scrap_asset"
	),
	"erpnext.assets.doctype.asset.depreciation.restore_asset": (
		"erpnext_extensions.asset_usage_depreciation.integration_hooks.restore_asset"
	),
}
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
override_doctype_dashboards = {
	"Asset": "erpnext_extensions.asset_usage_depreciation.asset_dashboard.get_data",
	"Material Request": "erpnext_extensions.asset_usage_depreciation.material_request_dashboard.get_data",
}

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

ignore_links_on_delete = ["PM Request"]

# Request Events
# ----------------
# Ensure IRR monkey patches are applied on boot/request paths.
# apply_monkey_patches is idempotent (guarded), so calling it on requests is safe.
before_request = [
	"erpnext_extensions.iran_accounting.integration.bootstrap.apply",
]
# after_request = ["erpnext_extensions.utils.after_request"]

# ERPNext regional extension: end of BuyingController.update_valuation_rate.
# Bound for Iran region; monkey_patches also installs the same function so IRR
# companies work regardless of Desk country setting.
regional_overrides = {
	"Iran": {
		"erpnext.controllers.buying_controller.update_regional_item_valuation_rate": [
			"erpnext_extensions.iran_accounting.buying_selling.update_regional_item_valuation_rate"
		],
	},
}

# Job Events
# ----------
before_job = [
	"erpnext_extensions.iran_accounting.integration.bootstrap.apply",
]
# after_job = ["erpnext_extensions.utils.after_job"]

# User Data Protection
# --------------------

# user_data_fields = [
# 	{
# 		"doctype": "{doctype_1}",
# 		"filter_by": "{filter_by}",
# 		"redact_fields": ["{field_1}", "{field_2}"],
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_2}",
# 		"filter_by": "{filter_by}",
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_3}",
# 		"strict": False,
# 	},
# 	{
# 		"doctype": "{doctype_4}"
# 	}
# ]

# Authentication and authorization
# --------------------------------

# auth_hooks = [
# 	"erpnext_extensions.auth.validate"
# ]

# Automatically update python controller files with type annotations for this app.
# export_python_type_annotations = True

# default_log_clearing_doctypes = {
# 	"Logging DocType Name": 30  # days to retain logs
# }

# Fixtures
# --------
# Fixtures are used to export/import customizations (Custom Fields, Scripts, Workflows, etc.)
# between different sites. When you run `bench --site [site] export-fixtures`, these doctypes
# will be exported to the fixtures directory and can be imported to other sites.
#
# Usage:
# 1. Export fixtures: bench --site [site] export-fixtures
# 2. Import fixtures: bench --site [new-site] migrate (fixtures are imported automatically)
#
# The fixtures will be saved in: erpnext_extensions/fixtures/

fixtures = [
	{"dt": "Custom Field"},
	{"dt": "Client Script"},
	{"dt": "Server Script"},
	{"dt": "Property Setter"},
	{"dt": "Workflow State"},
	{"dt": "Workflow Action Master"},
	{"dt": "Workflow"},
	{"dt": "Role"},
]

standard_queries = {
	"PM Holder": "erpnext_extensions.petty_management.doctype.pm_holder.pm_holder.pm_holder_query",
	"PM Opening Advance": "erpnext_extensions.petty_management.doctype.pm_opening_advance.pm_opening_advance.pm_opening_advance_link_query",
	"Facility": "erpnext_extensions.facility_management.facility_queries.facility_link_query",
	"Facility Type": "erpnext_extensions.facility_management.facility_queries.facility_type_link_query",
}

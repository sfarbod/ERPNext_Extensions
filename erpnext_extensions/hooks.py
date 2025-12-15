app_name = "erpnext_extensions"
app_title = "ERPNext Extensions"
app_publisher = "Farbod Siyahpoosh"
app_description = "Extensions for ERPNext v15 — decimal precision, localization, Jalali, etc."
app_email = "sfarbod@gmail.com"
app_license = "mit"

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
# app_include_css = "/assets/erpnext_extensions/css/erpnext_extensions.css"
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
# doctype_js = {"doctype" : "public/js/doctype.js"}
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}
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

# permission_query_conditions = {
# 	"Event": "frappe.desk.doctype.event.event.get_permission_query_conditions",
# }
#
# has_permission = {
# 	"Event": "frappe.desk.doctype.event.event.has_permission",
# }

# DocType Class
# ---------------
# Override standard doctype classes

# override_doctype_class = {
# 	"ToDo": "custom_app.overrides.CustomToDo"
# }

# Document Events
# ---------------
# Hook on document methods and events

# Document Events
# ---------------
# Temporarily disabled - Cheque Management module
# doc_events = {
# 	"Cheque": {
# 		"on_update": "erpnext_extensions.cheque_management.doctype.cheque.cheque.on_cheque_update",
# 		"before_delete": "erpnext_extensions.cheque_management.doctype.cheque.cheque.before_cheque_delete",
# 		"on_update_after_submit": "erpnext_extensions.cheque_management.doctype.cheque.cheque.on_cheque_update_after_submit"
# 	}
# }

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
# override_whitelisted_methods = {
# 	"frappe.desk.doctype.event.event.get_events": "erpnext_extensions.event.get_events"
# }
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "erpnext_extensions.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Request Events
# ----------------
# before_request = ["erpnext_extensions.utils.before_request"]
# after_request = ["erpnext_extensions.utils.after_request"]

# Job Events
# ----------
# before_job = ["erpnext_extensions.utils.before_job"]
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
	{"dt": "Workflow"},
	{"dt": "Workflow State"},
	{"dt": "Workflow Action Master"},
	{"dt": "Role"},
]


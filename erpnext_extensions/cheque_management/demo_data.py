"""
Demo Data and Test Scripts for Cheque Management
Run this script to create sample data for testing
"""

import frappe
from frappe.utils import getdate, add_days


def create_sample_cheque_settings(company):
	"""
	Create sample Cheque Settings for a company
	Note: This assumes accounts exist in the company
	"""
	# Check if settings already exist
	if frappe.db.exists("Cheque Settings", {"company": company}):
		frappe.msgprint(f"Cheque Settings already exists for {company}")
		return frappe.get_doc("Cheque Settings", {"company": company})
	
	# Get company default accounts
	company_doc = frappe.get_doc("Company", company)
	
	# Try to find or create required accounts
	settings = frappe.new_doc("Cheque Settings")
	settings.company = company
	
	# Get default receivable account (usually from Customer)
	receivable_account = frappe.db.get_value(
		"Account",
		{"account_type": "Receivable", "company": company, "is_group": 0},
		"name",
		order_by="creation desc"
	)
	
	# Get default bank account
	bank_account = frappe.db.get_value(
		"Account",
		{"account_type": "Bank", "company": company, "is_group": 0},
		"name",
		order_by="creation desc"
	)
	
	# Try to find existing accounts or use defaults
	# Note: User should create these accounts in Chart of Accounts first
	settings.default_receivable_cheque_account = receivable_account
	settings.default_under_collection_account = receivable_account  # Use same for demo
	settings.default_returned_cheque_account = receivable_account  # Use same for demo
	settings.default_payable_cheque_account = receivable_account  # Use same for demo (should be Payable)
	settings.default_bank_account = bank_account
	settings.enable_sayad_fields = 1
	
	try:
		settings.save()
		frappe.msgprint(f"Cheque Settings created for {company}")
		frappe.msgprint("Note: Please update account settings with proper accounts from Chart of Accounts")
		return settings
	except Exception as e:
		frappe.msgprint(f"Error creating Cheque Settings: {str(e)}")
		frappe.msgprint("Please create the following accounts in Chart of Accounts:")
		frappe.msgprint("- Receivable Cheque Account (Asset)")
		frappe.msgprint("- Under Collection Account (Asset)")
		frappe.msgprint("- Returned Cheque Account (Asset)")
		frappe.msgprint("- Payable Cheque Account (Liability)")
		frappe.msgprint("- Bank Account (Bank)")
		return None


@frappe.whitelist()
def create_demo_receivable_cheque(company, customer=None, amount=100000):
	"""
	Create a demo Receivable Cheque and test full lifecycle
	"""
	if not frappe.db.exists("Company", company):
		frappe.throw(f"Company {company} does not exist")
	
	# Get or create customer
	if not customer:
		customers = frappe.get_all("Customer", {"company": company}, limit=1)
		if customers:
			customer = customers[0].name
		else:
			frappe.throw("No customer found. Please create a customer first.")
	
	# Create Cheque Settings if not exists
	create_sample_cheque_settings(company)
	
	# Create Receivable Cheque
	cheque = frappe.new_doc("Cheque")
	cheque.cheque_type = "Receivable"
	cheque.company = company
	cheque.cheque_no = f"DEMO-REC-{frappe.utils.now_datetime().strftime('%Y%m%d%H%M%S')}"
	cheque.cheque_date = getdate()
	cheque.cheque_amount = amount
	cheque.party_type = "Customer"
	cheque.party = customer
	cheque.save()
	
	frappe.msgprint(f"Demo Receivable Cheque created: {cheque.name}")
	return cheque


@frappe.whitelist()
def test_receivable_cheque_lifecycle(cheque_name):
	"""
	Test full lifecycle of a Receivable Cheque
	"""
	cheque = frappe.get_doc("Cheque", cheque_name)
	
	if cheque.cheque_type != "Receivable":
		frappe.throw("This is not a Receivable cheque")
	
	results = []
	
	try:
		# Step 1: Mark Waiting For Sayad
		cheque.mark_waiting_for_sayad()
		results.append(f"✓ Marked as Waiting For Sayad")
		
		# Step 2: Mark Registered In Sayad
		cheque.reload()
		cheque.mark_registered_in_sayad()
		results.append(f"✓ Marked as Registered In Sayad")
		
		# Step 3: Move To Box
		cheque.reload()
		cheque.move_to_box()
		results.append(f"✓ Moved to Box")
		
		# Step 4: Assign To Bank (creates JE)
		cheque.reload()
		je1 = cheque.assign_to_bank()
		results.append(f"✓ Assigned to Bank - JE: {je1.name}")
		
		# Step 5: Mark As Collected (creates JE) - requires Cheque Manager
		if frappe.has_permission("Cheque", "submit", cheque.name):
			cheque.reload()
			je2 = cheque.mark_as_collected()
			results.append(f"✓ Marked as Collected - JE: {je2.name}")
		else:
			results.append("⚠ Mark As Collected skipped (requires Cheque Manager)")
		
		frappe.msgprint({
			"message": "Receivable Cheque Lifecycle Test Completed:<br>" + "<br>".join(results),
			"indicator": "green"
		})
		
		return {"success": True, "results": results}
		
	except Exception as e:
		frappe.msgprint(f"Error in lifecycle test: {str(e)}")
		return {"success": False, "error": str(e)}


@frappe.whitelist()
def create_demo_payable_cheque(company, supplier=None, amount=50000):
	"""
	Create a demo Payable Cheque and test full lifecycle
	"""
	if not frappe.db.exists("Company", company):
		frappe.throw(f"Company {company} does not exist")
	
	# Get or create supplier
	if not supplier:
		suppliers = frappe.get_all("Supplier", {"company": company}, limit=1)
		if suppliers:
			supplier = suppliers[0].name
		else:
			frappe.throw("No supplier found. Please create a supplier first.")
	
	# Create Cheque Settings if not exists
	create_sample_cheque_settings(company)
	
	# Get bank account
	bank_account = frappe.db.get_value(
		"Account",
		{"account_type": "Bank", "company": company, "is_group": 0},
		"name",
		order_by="creation desc"
	)
	
	# Create Payable Cheque
	cheque = frappe.new_doc("Cheque")
	cheque.cheque_type = "Payable"
	cheque.company = company
	cheque.cheque_no = f"DEMO-PAY-{frappe.utils.now_datetime().strftime('%Y%m%d%H%M%S')}"
	cheque.cheque_date = add_days(getdate(), 30)  # Future date
	cheque.cheque_amount = amount
	cheque.party_type = "Supplier"
	cheque.party = supplier
	cheque.bank_account = bank_account
	cheque.save()
	
	frappe.msgprint(f"Demo Payable Cheque created: {cheque.name}")
	return cheque


@frappe.whitelist()
def test_payable_cheque_lifecycle(cheque_name):
	"""
	Test full lifecycle of a Payable Cheque
	"""
	cheque = frappe.get_doc("Cheque", cheque_name)
	
	if cheque.cheque_type != "Payable":
		frappe.throw("This is not a Payable cheque")
	
	results = []
	
	try:
		# Step 1: Select Bank
		if not cheque.bank_account:
			frappe.throw("Bank Account is required")
		cheque.select_bank()
		results.append(f"✓ Bank selected")
		
		# Step 2: Issue Cheque (creates JE)
		cheque.reload()
		je1 = cheque.issue_cheque()
		results.append(f"✓ Cheque issued - JE: {je1.name}")
		
		# Step 3: Mark As Printed
		cheque.reload()
		cheque.mark_as_printed()
		results.append(f"✓ Marked as printed")
		
		# Step 4: First Signature Done
		cheque.reload()
		cheque.first_signature_done()
		results.append(f"✓ First signature done")
		
		# Step 5: Second Signature Done
		cheque.reload()
		cheque.second_signature_done()
		results.append(f"✓ Second signature done")
		
		# Step 6: Notify Supplier
		cheque.reload()
		cheque.notify_supplier()
		results.append(f"✓ Supplier notified")
		
		# Step 7: Deliver To Supplier
		cheque.reload()
		cheque.deliver_to_supplier()
		results.append(f"✓ Delivered to supplier")
		
		# Step 8: Mark Registered In Sayad
		cheque.reload()
		cheque.mark_registered_in_sayad_payable()
		results.append(f"✓ Marked as registered in Sayad")
		
		# Step 9: Mark Sayad Success
		cheque.reload()
		cheque.mark_sayad_success()
		results.append(f"✓ Sayad success")
		
		frappe.msgprint({
			"message": "Payable Cheque Lifecycle Test Completed:<br>" + "<br>".join(results),
			"indicator": "green"
		})
		
		return {"success": True, "results": results}
		
	except Exception as e:
		frappe.msgprint(f"Error in lifecycle test: {str(e)}")
		return {"success": False, "error": str(e)}


@frappe.whitelist()
def setup_demo_data(company):
	"""
	Setup complete demo data for testing
	Returns: Cheque Settings document object if successful, None otherwise
	"""
	try:
		# Create Cheque Settings
		settings = create_sample_cheque_settings(company)
		
		if not settings:
			return None
		
		frappe.msgprint({
			"message": f"Demo data setup completed for {company}<br>You can now create test cheques using the create_demo_cheque methods.",
			"indicator": "green"
		})
		
		return settings
		
	except Exception as e:
		frappe.msgprint(f"Error setting up demo data: {str(e)}")
		return None


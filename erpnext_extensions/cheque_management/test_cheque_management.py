"""
Test Script for Cheque Management Module
Run this script from Frappe Console or via bench console

Usage:
    bench --site frontend console
    Then in console:
    exec(open('apps/erpnext_extensions/erpnext_extensions/cheque_management/test_cheque_management.py').read())
"""

import frappe
from erpnext_extensions.cheque_management.demo_data import (
    setup_demo_data,
    create_demo_receivable_cheque,
    create_demo_payable_cheque,
    test_receivable_cheque_lifecycle,
    test_payable_cheque_lifecycle
)

def run_all_tests():
    """Run all tests for Cheque Management"""
    
    print("=" * 60)
    print("Cheque Management Module - Test Suite")
    print("=" * 60)
    
    # Get company
    companies = frappe.get_all("Company", limit=1)
    if not companies:
        print("❌ ERROR: No company found. Please create a company first.")
        return
    
    company = companies[0].name
    print(f"\n✓ Using Company: {company}")
    
    # Test 1: Setup Cheque Settings
    print("\n" + "-" * 60)
    print("Test 1: Setting up Cheque Settings")
    print("-" * 60)
    try:
        settings = setup_demo_data(company)
        if settings:
            print(f"✓ Cheque Settings created/verified: {settings.name}")
        else:
            print("⚠ Warning: Cheque Settings setup had issues. Please check manually.")
    except Exception as e:
        print(f"❌ ERROR in Cheque Settings setup: {str(e)}")
        return
    
    # Test 2: Create Receivable Cheque
    print("\n" + "-" * 60)
    print("Test 2: Creating Receivable Cheque")
    print("-" * 60)
    try:
        customers = frappe.get_all("Customer", {"company": company}, limit=1)
        if not customers:
            print("⚠ Warning: No customer found. Skipping Receivable test.")
            receivable = None
        else:
            customer = customers[0].name
            receivable = create_demo_receivable_cheque(company, customer, 100000)
            print(f"✓ Receivable Cheque created: {receivable.name}")
            print(f"  - Cheque No: {receivable.cheque_no}")
            print(f"  - Amount: {receivable.cheque_amount}")
            print(f"  - Status: {receivable.status}")
    except Exception as e:
        print(f"❌ ERROR creating Receivable Cheque: {str(e)}")
        receivable = None
    
    # Test 3: Test Receivable Lifecycle
    if receivable:
        print("\n" + "-" * 60)
        print("Test 3: Testing Receivable Cheque Lifecycle")
        print("-" * 60)
        try:
            result = test_receivable_cheque_lifecycle(receivable.name)
            if result.get("success"):
                print("✓ Receivable Lifecycle Test PASSED")
                for step in result.get("results", []):
                    print(f"  {step}")
            else:
                print(f"❌ Receivable Lifecycle Test FAILED: {result.get('error')}")
        except Exception as e:
            print(f"❌ ERROR in Receivable Lifecycle test: {str(e)}")
    
    # Test 4: Create Payable Cheque
    print("\n" + "-" * 60)
    print("Test 4: Creating Payable Cheque")
    print("-" * 60)
    try:
        suppliers = frappe.get_all("Supplier", {"company": company}, limit=1)
        if not suppliers:
            print("⚠ Warning: No supplier found. Skipping Payable test.")
            payable = None
        else:
            supplier = suppliers[0].name
            payable = create_demo_payable_cheque(company, supplier, 50000)
            print(f"✓ Payable Cheque created: {payable.name}")
            print(f"  - Cheque No: {payable.cheque_no}")
            print(f"  - Amount: {payable.cheque_amount}")
            print(f"  - Status: {payable.status}")
    except Exception as e:
        print(f"❌ ERROR creating Payable Cheque: {str(e)}")
        payable = None
    
    # Test 5: Test Payable Lifecycle
    if payable:
        print("\n" + "-" * 60)
        print("Test 5: Testing Payable Cheque Lifecycle")
        print("-" * 60)
        try:
            result = test_payable_cheque_lifecycle(payable.name)
            if result.get("success"):
                print("✓ Payable Lifecycle Test PASSED")
                for step in result.get("results", []):
                    print(f"  {step}")
            else:
                print(f"❌ Payable Lifecycle Test FAILED: {result.get('error')}")
        except Exception as e:
            print(f"❌ ERROR in Payable Lifecycle test: {str(e)}")
    
    # Test 6: Verify Journal Entries
    print("\n" + "-" * 60)
    print("Test 6: Verifying Journal Entries")
    print("-" * 60)
    try:
        if receivable:
            receivable.reload()
            if receivable.journal_references:
                print(f"✓ Receivable Cheque has {len(receivable.journal_references)} Journal Entry references:")
                for ref in receivable.journal_references:
                    print(f"  - {ref.journal_entry} ({ref.purpose})")
            else:
                print("⚠ Warning: No Journal Entries found for Receivable Cheque")
        
        if payable:
            payable.reload()
            if payable.journal_references:
                print(f"✓ Payable Cheque has {len(payable.journal_references)} Journal Entry references:")
                for ref in payable.journal_references:
                    print(f"  - {ref.journal_entry} ({ref.purpose})")
            else:
                print("⚠ Warning: No Journal Entries found for Payable Cheque")
    except Exception as e:
        print(f"❌ ERROR verifying Journal Entries: {str(e)}")
    
    # Summary
    print("\n" + "=" * 60)
    print("Test Summary")
    print("=" * 60)
    print("✓ All tests completed!")
    print("\nNext steps:")
    print("1. Check Cheque Settings in UI: Cheque Management > Cheque Settings")
    print("2. View created cheques: Cheque Management > Cheque")
    print("3. View Journal Entries: Accounting > Journal Entry")
    print("4. Test UI actions manually through the web interface")
    print("=" * 60)

if __name__ == "__main__":
    run_all_tests()


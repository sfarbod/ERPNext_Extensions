# Testing Guide for Cheque Management

## Prerequisites

1. ERPNext 15 installed and running
2. At least one Company configured
3. At least one Customer and one Supplier
4. Chart of Accounts with required accounts (see below)

## Required Accounts Setup

Before testing, create these accounts in Chart of Accounts:

### For Receivable Cheques:
- **Receivable Cheque Account** (Account Type: Asset)
- **Under Collection Account** (Account Type: Asset)  
- **Returned Cheque Account** (Account Type: Asset)

### For Payable Cheques:
- **Payable Cheque Account** (Account Type: Liability)

### Common:
- **Bank Account** (Account Type: Bank)

## Step-by-Step Testing

### 1. Setup Cheque Settings

```python
# In Frappe Console
import frappe
from erpnext_extensions.cheque_management.demo_data import setup_demo_data

# Replace with your company name
setup_demo_data("Your Company Name")
```

Or manually:
1. Go to **Cheque Management > Cheque Settings**
2. Select Company
3. Fill in all account fields
4. Save

### 2. Test Receivable Cheque Lifecycle

```python
from erpnext_extensions.cheque_management.demo_data import (
    create_demo_receivable_cheque,
    test_receivable_cheque_lifecycle
)

# Create demo cheque
cheque = create_demo_receivable_cheque(
    company="Your Company Name",
    customer="CUST-00001",  # Replace with actual customer
    amount=100000
)

# Test full lifecycle
test_receivable_cheque_lifecycle(cheque.name)
```

**Expected Flow:**
1. Received From Customer → Waiting For Sayad
2. Waiting For Sayad → Registered In Sayad
3. Registered In Sayad → Move To Box
4. Move To Box → Assign To Bank (creates JE)
5. Under Collection → Mark As Collected (creates JE, requires Cheque Manager)

### 3. Test Payable Cheque Lifecycle

```python
from erpnext_extensions.cheque_management.demo_data import (
    create_demo_payable_cheque,
    test_payable_cheque_lifecycle
)

# Create demo cheque
cheque = create_demo_payable_cheque(
    company="Your Company Name",
    supplier="SUP-00001",  # Replace with actual supplier
    amount=50000
)

# Test full lifecycle
test_payable_cheque_lifecycle(cheque.name)
```

**Expected Flow:**
1. Payment Request Created → Select Bank
2. Select Bank → Issue Cheque (creates JE)
3. Issued → Mark As Printed
4. Mark As Printed → First Signature Done
5. First Signature Done → Second Signature Done
6. Second Signature Done → Notify Supplier
7. Notify Supplier → Deliver To Supplier
8. Deliver To Supplier → Mark Registered In Sayad
9. Mark Registered In Sayad → Mark Sayad Success

### 4. Verify Journal Entries

After running lifecycle tests, verify:

1. **Journal Entries Created**: Check that JEs are created and submitted
2. **Journal References**: Check that JEs are linked in Cheque > Journal References table
3. **Account Balances**: Verify account balances are correct

### 5. Test Permissions

**Test as Cheque User:**
- Should be able to create cheques
- Should NOT see "Mark As Collected" button
- Should NOT see "Mark As Void" button
- Should NOT be able to manually edit status

**Test as Cheque Manager:**
- Should see all buttons
- Should be able to perform all actions
- Should be able to mark as collected and void

### 6. Test Error Handling

1. **Missing Settings**: Try to create JE without Cheque Settings → Should show error
2. **Invalid Status Transition**: Try invalid status change → Should show error
3. **Missing Accounts**: Try action without required accounts → Should show error
4. **Permission Denied**: Try sensitive action as Cheque User → Should show error

## Manual UI Testing

### Receivable Cheque Flow:

1. Create new Cheque (Receivable type)
2. Fill in: Cheque No, Date, Amount, Customer
3. Status should be "Received From Customer" (colored)
4. Click "Mark Waiting For Sayad" → Status changes
5. Continue through all steps
6. Verify Journal Entries are created and linked

### Payable Cheque Flow:

1. Create new Cheque (Payable type)
2. Fill in: Cheque No, Date, Amount, Supplier, Bank Account
3. Status should be "Payment Request Created" (colored)
4. Click "Select Bank" → Status changes
5. Continue through all steps
6. Verify Journal Entry is created and linked

## Verification Checklist

- [ ] Cheque Settings can be created and saved
- [ ] Receivable cheques can be created
- [ ] Payable cheques can be created
- [ ] Status changes work correctly
- [ ] Journal Entries are created automatically
- [ ] Journal Entries are linked in Journal References
- [ ] Colored status indicator works
- [ ] Confirmation dialogs appear for financial actions
- [ ] JE links appear in success messages
- [ ] Permissions work correctly (Cheque User vs Manager)
- [ ] Invalid transitions are blocked
- [ ] No ERPNext core features are broken

## Troubleshooting

### Issue: "Cheque Settings not found"
**Solution**: Create Cheque Settings manually or run `setup_demo_data()`

### Issue: "Account not found"
**Solution**: Create required accounts in Chart of Accounts first

### Issue: "Permission denied"
**Solution**: Ensure user has Cheque Manager role for sensitive actions

### Issue: "Status cannot be changed"
**Solution**: Use action buttons instead of manual editing

## Notes

- All Journal Entries are automatically submitted
- Status field is read-only (use action buttons)
- Financial actions require confirmation
- Sensitive actions require Cheque Manager role


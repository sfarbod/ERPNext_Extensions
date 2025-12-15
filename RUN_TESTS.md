# راهنمای کامل تست ماژول مدیریت چک

## 📋 فهرست مطالب
1. [پیش‌نیازها](#پیش‌نیازها)
2. [آماده‌سازی اولیه](#آماده‌سازی-اولیه)
3. [سناریوهای تست چک دریافتنی](#سناریوهای-تست-چک-دریافتنی)
4. [سناریوهای تست چک پرداختنی](#سناریوهای-تست-چک-پرداختنی)
5. [تست از طریق UI](#تست-از-طریق-ui)
6. [تست از طریق Console](#تست-از-طریق-console)

---

## پیش‌نیازها

### 1. نصب و راه‌اندازی

```bash
# اطمینان حاصل کنید که bench start اجرا شده است
bench start

# در ترمینال دیگر:
cd /workspace/development/frappe-bench
bench --site mysite.localhost clear-cache
bench build --app erpnext_extensions
bench --site mysite.localhost migrate
```

### 2. ایجاد حساب‌های مورد نیاز

قبل از تست، باید حساب‌های زیر را در **Chart of Accounts** ایجاد کنید:

#### برای چک‌های دریافتنی:
1. **Receivable Cheque Account**
   - Account Type: `Asset`
   - Parent Account: `Assets > Current Assets`
   - مثال: `Assets > Current Assets > Receivable Cheque`

2. **Under Collection Account**
   - Account Type: `Asset`
   - Parent Account: `Assets > Current Assets`
   - مثال: `Assets > Current Assets > Under Collection`

3. **Returned Cheque Account**
   - Account Type: `Asset`
   - Parent Account: `Assets > Current Assets`
   - مثال: `Assets > Current Assets > Returned Cheque`

#### برای چک‌های پرداختنی:
4. **Payable Cheque Account**
   - Account Type: `Liability`
   - Parent Account: `Liabilities > Current Liabilities`
   - مثال: `Liabilities > Current Liabilities > Payable Cheque`

#### حساب بانکی:
5. **Bank Account**
   - Account Type: `Bank`
   - Parent Account: `Assets > Bank Accounts`
   - مثال: `Assets > Bank Accounts > Main Bank`

### 3. ایجاد Company, Customer, Supplier

- حداقل یک **Company** باید وجود داشته باشد
- حداقل یک **Customer** برای تست چک دریافتنی
- حداقل یک **Supplier** برای تست چک پرداختنی

---

## آماده‌سازی اولیه

### مرحله 1: ایجاد Cheque Settings

#### از طریق UI:
1. به `http://mysite.localhost:8000` بروید
2. وارد سیستم شوید
3. به **Cheque Management > Cheque Settings** بروید
4. **Company** را انتخاب کنید
5. حساب‌های پیش‌فرض را تنظیم کنید:
   - Default Receivable Cheque Account
   - Default Under Collection Account
   - Default Returned Cheque Account
   - Default Payable Cheque Account
   - Default Bank Account
6. در صورت نیاز، **"Enable Sayad Fields"** را فعال کنید
7. **Save** کنید

#### از طریق Console:
```bash
bench --site mysite.localhost console
```

```python
import frappe
from erpnext_extensions.cheque_management.demo_data import setup_demo_data

# جایگزین کنید با نام Company خود
company_name = "Your Company Name"  # مثال: "Test Company"
setup_demo_data(company_name)
```

---

## سناریوهای تست چک دریافتنی

### سناریو 1: Flow عادی (موفق) ⭐

**هدف**: تست workflow کامل چک دریافتنی از دریافت تا وصول

#### گام 1: ایجاد چک دریافتنی
1. به **Cheque Management > Cheque** بروید
2. روی **New** کلیک کنید
3. فرم را پر کنید:
   - **Cheque Type**: `Receivable`
   - **Company**: انتخاب کنید
   - **Cheque No**: `TEST-REC-001`
   - **Cheque Date**: تاریخ امروز
   - **Cheque Amount**: `1,000,000`
   - **Party Type**: `Customer`
   - **Party**: یک Customer انتخاب کنید
4. **Save** کنید
5. ✅ وضعیت باید `Received From Customer` باشد (رنگ آبی)

#### گام 2: Mark Waiting For Sayad
1. روی دکمه **"Mark Waiting For Sayad"** کلیک کنید
2. ✅ وضعیت باید به `Waiting For Sayad` تغییر کند (رنگ زرد)

#### گام 3: Mark Registered In Sayad
1. روی دکمه **"Mark Registered In Sayad"** کلیک کنید
2. ✅ وضعیت باید به `Registered In Sayad` تغییر کند (رنگ سبز)

#### گام 4: Move To Box
1. روی دکمه **"Move To Box"** کلیک کنید
2. ✅ وضعیت باید به `Move To Box` تغییر کند (رنگ خاکستری)

#### گام 5: Assign To Bank (ایجاد JE)
1. روی دکمه **"Assign To Bank"** کلیک کنید
2. یک Dialog باز می‌شود: **"This will create a Journal Entry for Under Collection. Continue?"**
3. روی **OK** کلیک کنید
4. ✅ وضعیت باید به `Under Collection` تغییر کند (رنگ آبی)
5. ✅ یک پیام موفقیت با لینک Journal Entry نمایش داده می‌شود
6. ✅ در جدول **Journal References** یک رکورد اضافه شده است

**بررسی Journal Entry:**
- به Journal Entry ایجاد شده بروید
- Debit: `Default Receivable Cheque Account` = 1,000,000
- Credit: `Default Under Collection Account` = 1,000,000
- Status: Submitted

#### گام 6: Mark As Collected (فقط Cheque Manager)
1. مطمئن شوید که کاربر دارای Role **"Cheque Manager"** است
2. روی دکمه **"Mark As Collected"** کلیک کنید
3. Dialog: **"This will create a Journal Entry for Collection. Continue?"**
4. روی **OK** کلیک کنید
5. ✅ وضعیت باید به `Collected` تغییر کند (رنگ سبز)
6. ✅ یک Journal Entry جدید ایجاد شده است

**بررسی Journal Entry:**
- Debit: `Default Bank Account` = 1,000,000
- Credit: `Default Under Collection Account` = 1,000,000

**✅ نتیجه**: چک با موفقیت وصول شد!

---

### سناریو 2: برگشت چک (Returned) ⭐

**هدف**: تست سناریوی برگشت چک از بانک

#### پیش‌نیاز:
- چک در وضعیت `Under Collection` باشد

#### گام 1: Mark As Returned From Bank
1. چک را در وضعیت `Under Collection` قرار دهید (یا از سناریو 1، گام 5 استفاده کنید)
2. روی دکمه **"Mark As Returned From Bank"** کلیک کنید
3. Dialog: **"This will create a Journal Entry for Return. Continue?"**
4. روی **OK** کلیک کنید
5. ✅ وضعیت باید به `Returned From Bank` تغییر کند (رنگ قرمز)

**بررسی Journal Entry:**
- Debit: `Default Returned Cheque Account` = 1,000,000
- Credit: `Default Under Collection Account` = 1,000,000

#### گام 2: Return To Customer
1. روی دکمه **"Return To Customer"** کلیک کنید
2. ✅ وضعیت باید به `Return To Customer` تغییر کند (رنگ قرمز)

**✅ نتیجه**: چک برگشتی به مشتری تحویل داده شد!

---

### سناریو 3: برگشت چک (ثبت نشد) - جدید ⭐

**هدف**: تست سناریوی برگشت چک قبل از ثبت در صیاد

#### پیش‌نیاز:
- چک در وضعیت `Waiting For Sayad` باشد

#### گام 1: Return Not Registered To Customer
1. چک را در وضعیت `Waiting For Sayad` قرار دهید
2. روی دکمه **"Return Not Registered To Customer"** کلیک کنید
3. Dialog: **"Are you sure you want to return this cheque to customer (not registered)?"**
4. روی **OK** کلیک کنید
5. ✅ وضعیت باید به `Returned Not Registered` تغییر کند (رنگ قرمز)

**✅ نتیجه**: چک ثبت نشده به مشتری برگشت داده شد!

---

### سناریو 4: عودت چک ثبت شده - جدید ⭐

**هدف**: تست سناریوی عودت چک بعد از ثبت در صیاد

#### پیش‌نیاز:
- چک در وضعیت `Registered In Sayad` باشد

#### گام 1: Return Registered To Customer
1. چک را در وضعیت `Registered In Sayad` قرار دهید
2. روی دکمه **"Return Registered To Customer"** کلیک کنید
3. Dialog: **"Are you sure you want to return this registered cheque to customer?"**
4. روی **OK** کلیک کنید
5. ✅ وضعیت باید به `Returned Registered To Customer` تغییر کند (رنگ صورتی)

**✅ نتیجه**: چک ثبت شده به مشتری برگشت داده شد!

---

### سناریو 5: پس گرفتن چک از بانک - جدید ⭐

**هدف**: تست سناریوی پس گرفتن چک از بانک بدون اقدام

#### پیش‌نیاز:
- چک در وضعیت `Under Collection` باشد

#### گام 1: Retrieve From Bank
1. چک را در وضعیت `Under Collection` قرار دهید
2. روی دکمه **"Retrieve From Bank"** کلیک کنید
3. Dialog: **"This will cancel the Under Collection Journal Entry and retrieve the cheque from bank. Continue?"**
4. روی **OK** کلیک کنید
5. ✅ وضعیت باید به `Retrieved From Bank` تغییر کند (رنگ نارنجی)
6. ✅ Journal Entry مربوط به Under Collection باید Cancel شده باشد

#### گام 2: Move Back To Box
1. روی دکمه **"Move Back To Box"** کلیک کنید
2. ✅ وضعیت باید به `Move To Box` تغییر کند
3. حالا می‌توانید دوباره به بانک واگذار کنید

**✅ نتیجه**: چک از بانک پس گرفته شد و به صندوق برگشت!

---

### سناریو 6: واگذاری مجدد به بانک - جدید ⭐

**هدف**: تست سناریوی واگذاری مجدد چک برگشتی به بانک

#### پیش‌نیاز:
- چک در وضعیت `Returned From Bank` یا `Retrieved From Bank` باشد

#### گام 1: Reassign To Bank
1. چک را در وضعیت `Returned From Bank` یا `Retrieved From Bank` قرار دهید
2. روی دکمه **"Reassign To Bank"** کلیک کنید
3. Dialog: **"This will create a Journal Entry for Under Collection. Continue?"**
4. روی **OK** کلیک کنید
5. ✅ وضعیت باید به `Under Collection` تغییر کند
6. ✅ یک Journal Entry جدید برای Under Collection ایجاد شده است

**✅ نتیجه**: چک دوباره به بانک واگذار شد!

---

## سناریوهای تست چک پرداختنی

### سناریو 7: Flow عادی چک پرداختنی (موفق) ⭐

**هدف**: تست workflow کامل چک پرداختنی از ایجاد تا ثبت موفق در صیاد

#### گام 1: ایجاد چک پرداختنی
1. به **Cheque Management > Cheque** بروید
2. روی **New** کلیک کنید
3. فرم را پر کنید:
   - **Cheque Type**: `Payable`
   - **Company**: انتخاب کنید
   - **Cheque No**: `TEST-PAY-001`
   - **Cheque Date**: تاریخ آینده (مثلاً 30 روز بعد)
   - **Cheque Amount**: `500,000`
   - **Party Type**: `Supplier`
   - **Party**: یک Supplier انتخاب کنید
   - **Bank Account**: یک حساب بانکی انتخاب کنید
4. **Save** کنید
5. ✅ وضعیت باید `Payment Request Created` باشد (رنگ آبی)

#### گام 2: Select Bank
1. اگر Bank Account انتخاب نشده، آن را انتخاب کنید
2. روی دکمه **"Select Bank"** کلیک کنید
3. ✅ وضعیت باید به `Select Bank` تغییر کند (رنگ زرد)

#### گام 3: Issue Cheque (ایجاد JE)
1. روی دکمه **"Issue Cheque"** کلیک کنید
2. Dialog: **"This will create a Journal Entry for Payable Issue. Continue?"**
3. روی **OK** کلیک کنید
4. ✅ وضعیت باید به `Issued` تغییر کند (رنگ آبی)
5. ✅ یک پیام موفقیت با لینک Journal Entry نمایش داده می‌شود

**بررسی Journal Entry:**
- Debit: `Default Payable Cheque Account` = 500,000
- Credit: `Default Bank Account` = 500,000

#### گام 4: Mark As Printed
1. روی دکمه **"Mark As Printed"** کلیک کنید
2. ✅ وضعیت باید به `Mark As Printed` تغییر کند (رنگ خاکستری)

#### گام 5: First Signature Done
1. روی دکمه **"First Signature Done"** کلیک کنید
2. ✅ وضعیت باید به `First Signature Done` تغییر کند (رنگ آبی)

#### گام 6: Second Signature Done
1. روی دکمه **"Second Signature Done"** کلیک کنید
2. ✅ وضعیت باید به `Second Signature Done` تغییر کند (رنگ سبز)

#### گام 7: Notify Supplier
1. روی دکمه **"Notify Supplier"** کلیک کنید
2. ✅ وضعیت باید به `Notify Supplier` تغییر کند (رنگ زرد)

#### گام 8: Deliver To Supplier
1. روی دکمه **"Deliver To Supplier"** کلیک کنید
2. ✅ وضعیت باید به `Deliver To Supplier` تغییر کند (رنگ سبز)

#### گام 9: Mark Registered In Sayad
1. روی دکمه **"Mark Registered In Sayad"** کلیک کنید
2. ✅ وضعیت باید به `Mark Registered In Sayad` تغییر کند (رنگ آبی)

#### گام 10: Mark Sayad Success
1. روی دکمه **"Mark Sayad Success"** کلیک کنید
2. ✅ وضعیت باید به `Mark Sayad Success` تغییر کند (رنگ سبز)

**✅ نتیجه**: چک با موفقیت صادر و در صیاد ثبت شد!

---

### سناریو 8: ابطال چک پرداختنی ⭐

**هدف**: تست سناریوی ابطال چک در مراحل مختلف

#### تست 8.1: ابطال قبل از چاپ
1. چک را در وضعیت `Payment Request Created` یا `Select Bank` قرار دهید
2. روی دکمه **"Mark As Void"** کلیک کنید (فقط Cheque Manager)
3. Dialog: **"Are you sure you want to mark this cheque as void?"**
4. روی **OK** کلیک کنید
5. ✅ وضعیت باید به `Mark As Void` تغییر کند (رنگ قرمز)

#### تست 8.2: ابطال بعد از چاپ
1. چک را در وضعیت `Mark As Printed` قرار دهید
2. روی دکمه **"Mark As Void"** کلیک کنید
3. ✅ وضعیت باید به `Mark As Void` تغییر کند

#### تست 8.3: ابطال بعد از امضا
1. چک را در وضعیت `First Signature Done` یا `Second Signature Done` قرار دهید
2. روی دکمه **"Mark As Void"** کلیک کنید
3. ✅ وضعیت باید به `Mark As Void` تغییر کند

**✅ نتیجه**: چک در هر مرحله قابل ابطال است (قبل از Cleared)!

---

## تست از طریق UI

### چک‌لیست تست UI:

#### چک دریافتنی:
- [ ] ایجاد چک دریافتنی
- [ ] دکمه "Mark Waiting For Sayad" نمایش داده می‌شود
- [ ] دکمه "Mark Registered In Sayad" نمایش داده می‌شود
- [ ] دکمه "Move To Box" نمایش داده می‌شود
- [ ] دکمه "Assign To Bank" نمایش داده می‌شود و JE ایجاد می‌کند
- [ ] دکمه "Mark As Collected" فقط برای Cheque Manager نمایش داده می‌شود
- [ ] دکمه "Mark As Returned From Bank" نمایش داده می‌شود و JE ایجاد می‌کند
- [ ] دکمه "Return To Customer" نمایش داده می‌شود
- [ ] دکمه "Return Not Registered To Customer" از Waiting For Sayad
- [ ] دکمه "Return Registered To Customer" از Registered In Sayad
- [ ] دکمه "Retrieve From Bank" از Under Collection
- [ ] دکمه "Move Back To Box" از Retrieved From Bank
- [ ] دکمه "Reassign To Bank" از Returned/Retrieved
- [ ] وضعیت‌ها به رنگ درست نمایش داده می‌شوند
- [ ] Status field read-only است

#### چک پرداختنی:
- [ ] ایجاد چک پرداختنی
- [ ] دکمه "Select Bank" نمایش داده می‌شود
- [ ] دکمه "Issue Cheque" نمایش داده می‌شود و JE ایجاد می‌کند
- [ ] دکمه "Mark As Printed" نمایش داده می‌شود
- [ ] دکمه "First Signature Done" نمایش داده می‌شود
- [ ] دکمه "Second Signature Done" نمایش داده می‌شود
- [ ] دکمه "Notify Supplier" نمایش داده می‌شود
- [ ] دکمه "Deliver To Supplier" نمایش داده می‌شود
- [ ] دکمه "Mark Registered In Sayad" نمایش داده می‌شود
- [ ] دکمه "Mark Sayad Success" نمایش داده می‌شود
- [ ] دکمه "Mark As Void" فقط برای Cheque Manager
- [ ] وضعیت‌ها به رنگ درست نمایش داده می‌شوند

---

## تست از طریق Console

### تست سریع با Demo Functions:

```bash
bench --site mysite.localhost console
```

```python
import frappe
from erpnext_extensions.cheque_management.demo_data import (
    setup_demo_data,
    create_demo_receivable_cheque,
    create_demo_payable_cheque,
    test_receivable_cheque_lifecycle,
    test_payable_cheque_lifecycle
)

# تنظیمات اولیه
company = "Your Company Name"  # جایگزین کنید
setup_demo_data(company)

# تست چک دریافتنی
receivable = create_demo_receivable_cheque(
    company=company,
    customer="CUST-00001",  # جایگزین کنید
    amount=1000000
)
print(f"Receivable Cheque created: {receivable.name}")

# تست lifecycle کامل
test_receivable_cheque_lifecycle(receivable.name)

# تست چک پرداختنی
payable = create_demo_payable_cheque(
    company=company,
    supplier="SUP-00001",  # جایگزین کنید
    amount=500000
)
print(f"Payable Cheque created: {payable.name}")

# تست lifecycle کامل
test_payable_cheque_lifecycle(payable.name)
```

### تست دستی Step by Step:

```python
import frappe
from erpnext_extensions.cheque_management.utils import ReceivableChequeStatus

# دریافت چک
cheque = frappe.get_doc("Cheque", "CHEQ-2025-00001")

# بررسی وضعیت فعلی
print(f"Current Status: {cheque.status}")

# تغییر وضعیت از طریق متدها
cheque.mark_waiting_for_sayad()
cheque.save()
print(f"New Status: {cheque.status}")  # باید "Waiting For Sayad" باشد

cheque.reload()
cheque.mark_registered_in_sayad()
cheque.save()
print(f"New Status: {cheque.status}")  # باید "Registered In Sayad" باشد

# ادامه workflow...
```

---

## سناریوهای تست پیشرفته

### تست 1: بررسی Journal Entry References

```python
import frappe

cheque = frappe.get_doc("Cheque", "CHEQ-2025-00001")

# بررسی Journal References
print("Journal References:")
for ref in cheque.journal_references:
    print(f"  - JE: {ref.journal_entry}, Purpose: {ref.purpose}, Amount: {ref.amount}")

# بررسی Journal Entries
for ref in cheque.journal_references:
    je = frappe.get_doc("Journal Entry", ref.journal_entry)
    print(f"\nJE {je.name}:")
    print(f"  Status: {je.docstatus} (0=Draft, 1=Submitted, 2=Cancelled)")
    for acc in je.accounts:
        print(f"  - {acc.account}: Debit={acc.debit}, Credit={acc.credit}")
```

### تست 2: بررسی Validation

```python
import frappe

# تست: تغییر Status به صورت دستی (باید خطا بدهد)
cheque = frappe.get_doc("Cheque", "CHEQ-2025-00001")
cheque.status = "Collected"  # این باید در validate() خطا بدهد
cheque.save()  # باید خطا بدهد: Status cannot be changed manually
```

### تست 3: بررسی Permissions

```python
import frappe

# تست: Cheque User نمی‌تواند Mark As Collected کند
cheque = frappe.get_doc("Cheque", "CHEQ-2025-00001")
cheque.status = "Under Collection"
cheque.save()

# با کاربر عادی (بدون Cheque Manager role)
frappe.set_user("user@example.com")  # کاربر بدون Cheque Manager
try:
    cheque.mark_as_collected()  # باید خطای Permission بدهد
except frappe.PermissionError:
    print("✓ Permission check works correctly!")
```

---

## چک‌لیست نهایی تست

### ✅ تست‌های اصلی:

#### Setup:
- [ ] Cheque Settings ایجاد شد
- [ ] حساب‌ها در Chart of Accounts ایجاد شدند

#### چک دریافتنی:
- [ ] چک دریافتنی ایجاد می‌شود
- [ ] همه دکمه‌های Action کار می‌کنند
- [ ] Journal Entry برای Assign To Bank ایجاد می‌شود
- [ ] Journal Entry برای Mark As Collected ایجاد می‌شود
- [ ] Journal Entry برای Mark As Returned ایجاد می‌شود
- [ ] Return Not Registered کار می‌کند
- [ ] Return Registered To Customer کار می‌کند
- [ ] Retrieve From Bank کار می‌کند و JE را cancel می‌کند
- [ ] Move Back To Box از Retrieved کار می‌کند
- [ ] Reassign To Bank کار می‌کند

#### چک پرداختنی:
- [ ] چک پرداختنی ایجاد می‌شود
- [ ] همه دکمه‌های Action کار می‌کنند
- [ ] Journal Entry برای Issue Cheque ایجاد می‌شود
- [ ] Mark As Void کار می‌کند

#### UI/UX:
- [ ] وضعیت‌ها به رنگ درست نمایش داده می‌شوند
- [ ] Status field read-only است
- [ ] دکمه‌ها به صورت شرطی نمایش داده می‌شوند
- [ ] پیام‌های موفقیت نمایش داده می‌شوند
- [ ] لینک‌های Journal Entry کار می‌کنند

#### Permissions:
- [ ] Cheque User نمی‌تواند Mark As Collected ببیند
- [ ] Cheque User نمی‌تواند Mark As Void ببیند
- [ ] Cheque Manager می‌تواند تمام Action‌ها را انجام دهد

---

## عیب‌یابی (Troubleshooting)

### مشکل: "Cheque Settings not found"
**راه حل:**
```python
from erpnext_extensions.cheque_management.demo_data import setup_demo_data
setup_demo_data("Your Company Name")
```

### مشکل: "Account not found"
**راه حل:** 
- به Chart of Accounts بروید
- حساب‌های مورد نیاز را ایجاد کنید
- در Cheque Settings حساب‌ها را تنظیم کنید

### مشکل: "Permission denied"
**راه حل:**
- مطمئن شوید کاربر دارای Role "Cheque Manager" است
- برای عملیات حساس (Mark As Collected, Mark As Void) نیاز به Cheque Manager است

### مشکل: "Status cannot be changed"
**راه حل:**
- Status را به صورت دستی تغییر ندهید
- از دکمه‌های Action استفاده کنید
- مطمئن شوید که تغییر Status از وضعیت فعلی مجاز است

### مشکل: Journal Entry ایجاد نمی‌شود
**راه حل:**
- بررسی کنید که Cheque Settings تنظیم شده باشد
- بررسی کنید که حساب‌ها در Cheque Settings درست انتخاب شده باشند
- لاگ‌های سرور را بررسی کنید

---

## دستورات سریع

```bash
# Clear cache
bench --site mysite.localhost clear-cache

# Build assets
bench build --app erpnext_extensions

# Migrate
bench --site mysite.localhost migrate

# Console
bench --site mysite.localhost console

# Start server (در ترمینال جداگانه)
bench start
```

---

## نکات مهم

1. **Status field همیشه read-only است** - فقط از Action Buttons استفاده کنید
2. **Journal Entries به صورت خودکار Submit می‌شوند**
3. **Journal References به صورت خودکار لینک می‌شوند**
4. **Cheque Manager** برای عملیات حساس مالی نیاز است
5. **همیشه بعد از تغییرات، migrate و clear-cache انجام دهید**

---

**نویسنده:** Auto  
**تاریخ:** 2025-01-27  
**نسخه:** 1.0


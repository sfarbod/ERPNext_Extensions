# Cheque Management Module

مدیریت چک‌های دریافتنی و پرداختنی برای ERPNext 15

## نصب و راه‌اندازی

### 1. نصب ماژول

```bash
bench --site [your-site] migrate
```

### 2. تنظیمات اولیه

#### ایجاد حساب‌های مورد نیاز

قبل از استفاده، باید حساب‌های زیر را در Chart of Accounts ایجاد کنید:

**برای چک‌های دریافتنی:**
- حساب Receivable Cheque (نوع: Asset)
- حساب Under Collection (نوع: Asset)
- حساب Returned Cheque (نوع: Asset)

**برای چک‌های پرداختنی:**
- حساب Payable Cheque (نوع: Liability)

**حساب بانکی:**
- حساب Bank (نوع: Bank)

#### ایجاد Cheque Settings

1. به **Cheque Management > Cheque Settings** بروید
2. Company را انتخاب کنید
3. حساب‌های پیش‌فرض را تنظیم کنید
4. در صورت نیاز، Sayad Fields را فعال کنید

### 3. ایجاد داده‌های نمونه (Demo Data)

برای تست سریع، می‌توانید از اسکریپت‌های demo استفاده کنید:

```python
# در Frappe Console یا Python Script
import frappe
from erpnext_extensions.cheque_management.demo_data import *

# Setup demo data
setup_demo_data("Your Company Name")

# Create demo Receivable Cheque
receivable = create_demo_receivable_cheque("Your Company Name", customer="CUST-00001", amount=100000)

# Test Receivable lifecycle
test_receivable_cheque_lifecycle(receivable.name)

# Create demo Payable Cheque
payable = create_demo_payable_cheque("Your Company Name", supplier="SUP-00001", amount=50000)

# Test Payable lifecycle
test_payable_cheque_lifecycle(payable.name)
```

## استفاده

### چک‌های دریافتنی (Receivable)

1. **ایجاد چک**: یک چک جدید با نوع "Receivable" ایجاد کنید
2. **Mark Waiting For Sayad**: چک را برای ثبت در سیستم صیاد آماده کنید
3. **Mark Registered In Sayad**: چک در سیستم صیاد ثبت شد
4. **Move To Box**: چک به صندوق منتقل شد
5. **Assign To Bank**: چک به بانک ارسال شد (ایجاد JE)
6. **Mark As Collected**: چک وصول شد (ایجاد JE) - فقط Cheque Manager
7. **Mark As Returned From Bank**: چک از بانک برگشت (ایجاد JE)
8. **Return To Customer**: چک به مشتری برگشت داده شد
9. **Reassign To Bank**: چک دوباره به بانک ارسال شد (ایجاد JE)

### چک‌های پرداختنی (Payable)

1. **ایجاد چک**: یک چک جدید با نوع "Payable" ایجاد کنید
2. **Select Bank**: حساب بانکی را انتخاب کنید
3. **Issue Cheque**: چک صادر شد (ایجاد JE)
4. **Mark As Printed**: چک چاپ شد
5. **First Signature Done**: امضای اول انجام شد
6. **Second Signature Done**: امضای دوم انجام شد
7. **Notify Supplier**: تامین‌کننده اطلاع داده شد
8. **Deliver To Supplier**: چک به تامین‌کننده تحویل داده شد
9. **Mark Registered In Sayad**: در سیستم صیاد ثبت شد
10. **Mark Sayad Success**: ثبت در صیاد موفق بود
11. **Mark As Void**: چک باطل شد - فقط Cheque Manager

## نقش‌ها و دسترسی‌ها

### Cheque User
- خواندن چک‌ها
- ایجاد چک جدید
- تغییر وضعیت از طریق دکمه‌ها (بدون عملیات مالی حساس)

### Cheque Manager
- تمام دسترسی‌های Cheque User
- Mark As Collected
- Mark As Void
- Submit, Cancel, Delete

## تست

برای تست کامل lifecycle:

```python
# Test Receivable Cheque
from erpnext_extensions.cheque_management.demo_data import test_receivable_cheque_lifecycle
test_receivable_cheque_lifecycle("CHEQ-2025-00001")

# Test Payable Cheque
from erpnext_extensions.cheque_management.demo_data import test_payable_cheque_lifecycle
test_payable_cheque_lifecycle("CHEQ-2025-00002")
```

## Workflow

این ماژول از **Frappe Workflow** برای مدیریت وضعیت‌های چک استفاده می‌کند.

### فعال‌سازی Workflow

برای فعال‌سازی Workflow، به راهنمای کامل مراجعه کنید:
- 📖 [راهنمای کامل فعال‌سازی Workflow](./WORKFLOW_SETUP_GUIDE.md)

### ویژگی‌های Workflow

- ✅ **ایجاد خودکار Journal Entry**: هنگام تغییر workflow state به وضعیت‌های مالی، Journal Entry به صورت خودکار ایجاد و Submit می‌شود
- ✅ **Submit خودکار**: هنگام ایجاد Journal Entry، سند Cheque نیز به صورت خودکار Submit می‌شود
- ✅ **جلوگیری از حذف**: سندهای Submit شده یا دارای Journal Entry قابل حذف نیستند
- ✅ **همگام‌سازی Status**: فیلد `status` به صورت خودکار با `workflow_state` همگام می‌شود

### مستندات Workflow

- 📄 [راهنمای پیاده‌سازی Workflow](./WORKFLOW_IMPLEMENTATION.md)
- 📄 [مقایسه Workflow و Custom Buttons](./WORKFLOW_VS_CUSTOM_BUTTONS.md)
- 📄 [راهنمای گام به گام فعال‌سازی](./WORKFLOW_SETUP_GUIDE.md)

## نکات مهم

1. **حساب‌ها**: قبل از استفاده، حتماً حساب‌های مورد نیاز را در Chart of Accounts ایجاد کنید
2. **Cheque Settings**: برای هر Company باید Cheque Settings ایجاد شود
3. **Journal Entries**: عملیات مالی به صورت خودکار Journal Entry ایجاد می‌کنند
4. **Permissions**: عملیات حساس فقط برای Cheque Manager قابل انجام است
5. **Workflow**: برای استفاده از Workflow، باید workflowها را در Frappe UI ایجاد کنید (راهنمای کامل در `WORKFLOW_SETUP_GUIDE.md`)

## پشتیبانی

برای مشکلات یا سوالات، لطفاً issue ایجاد کنید.


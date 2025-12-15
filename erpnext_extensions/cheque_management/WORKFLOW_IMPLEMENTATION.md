# پیاده‌سازی Workflow برای مدیریت چک

## خلاصه تغییرات

این سند خلاصه‌ای از تغییرات انجام شده برای پیاده‌سازی Workflow در ماژول مدیریت چک است.

## تغییرات انجام شده

### 1. افزودن فیلد Workflow State
- فیلد `workflow_state` به DocType `Cheque` اضافه شد
- این فیلد برای نگهداری وضعیت فعلی workflow استفاده می‌شود

### 2. ایجاد Workflow States
تمام وضعیت‌های مورد نیاز برای چک‌های دریافتنی و پرداختنی به `workflow_state.json` اضافه شدند:

**چک‌های دریافتنی:**
- Received From Customer
- Waiting For Sayad
- Registered In Sayad
- Move To Box
- Under Collection
- Collected
- Returned From Bank
- Returned
- Return To Customer
- Retrieved From Bank

**چک‌های پرداختنی:**
- Payment Request Created
- Select Bank
- Issued
- Mark As Printed
- First Signature Done
- Second Signature Done
- Notify Supplier
- Deliver To Supplier
- Mark Registered In Sayad
- Mark Sayad Success
- Cleared
- Mark As Void

### 3. ایجاد Workflow Actions
تمام عملیات‌های مورد نیاز به `workflow_action_master.json` اضافه شدند:
- Mark Waiting For Sayad
- Mark Registered In Sayad
- Move To Box
- Assign To Bank
- Mark As Collected
- Mark As Returned From Bank
- Return To Customer
- Reassign To Bank
- Retrieve From Bank
- Select Bank
- Issue Cheque
- Mark As Printed
- First Signature Done
- Second Signature Done
- Notify Supplier
- Deliver To Supplier
- Mark Sayad Success
- Mark As Void

### 4. پیاده‌سازی Hooks

#### `on_cheque_update`
- هنگام تغییر `workflow_state`، فیلد `status` به‌صورت خودکار به‌روزرسانی می‌شود
- Journal Entry به‌صورت خودکار برای وضعیت‌های خاص ایجاد می‌شود:
  - **Under Collection**: ایجاد JE برای Under Collection
  - **Collected**: ایجاد JE برای Collection
  - **Returned From Bank**: ایجاد JE برای Return
  - **Received From Customer**: ایجاد JE برای Receive
  - **Issued**: ایجاد JE برای Payable Issue
  - **Cleared**: ایجاد JE برای Payable Clear

#### `before_cheque_delete`
- جلوگیری از حذف سندهای Submit شده
- جلوگیری از حذف چک‌هایی که Journal Entry دارند
- جلوگیری از حذف چک‌های در وضعیت‌های نهایی (Collected, Return To Customer, Cleared, Void)

#### `on_cheque_update_after_submit`
- مدیریت تغییرات workflow برای سندهای Submit شده

### 5. ایجاد خودکار Journal Entry
هنگام تغییر workflow state به وضعیت‌های زیر، Journal Entry به‌صورت خودکار ایجاد و Submit می‌شود:

**چک‌های دریافتنی:**
- `Under Collection` → ایجاد JE برای انتقال به Under Collection
- `Collected` → ایجاد JE برای وصول چک
- `Returned From Bank` → ایجاد JE برای برگشت چک
- `Received From Customer` → ایجاد JE برای دریافت چک

**چک‌های پرداختنی:**
- `Issued` → ایجاد JE برای صدور چک
- `Cleared` → ایجاد JE برای تسویه چک

## مراحل بعدی

### 1. ایجاد Workflow در Frappe
باید دو Workflow در Frappe ایجاد شود:

#### Workflow برای چک‌های دریافتنی (Receivable Cheque Workflow)
- Document Type: `Cheque`
- Workflow State Field: `workflow_state`
- Condition: `doc.cheque_type == "Receivable"`

**States:**
1. Received From Customer (doc_status: 0)
2. Waiting For Sayad (doc_status: 0)
3. Registered In Sayad (doc_status: 0)
4. Move To Box (doc_status: 0)
5. Under Collection (doc_status: 1) - ایجاد JE
6. Collected (doc_status: 1) - ایجاد JE
7. Returned From Bank (doc_status: 1) - ایجاد JE
8. Returned (doc_status: 1)
9. Return To Customer (doc_status: 1)
10. Retrieved From Bank (doc_status: 0)

**Transitions:**
- Received From Customer → Waiting For Sayad (Action: Mark Waiting For Sayad)
- Waiting For Sayad → Registered In Sayad (Action: Mark Registered In Sayad)
- Registered In Sayad → Move To Box (Action: Move To Box)
- Received From Customer / Move To Box → Under Collection (Action: Assign To Bank)
- Under Collection → Collected (Action: Mark As Collected) - فقط Cheque Manager
- Under Collection → Returned From Bank (Action: Mark As Returned From Bank)
- Returned From Bank → Returned (Action: Return To Customer)
- Under Collection → Retrieved From Bank (Action: Retrieve From Bank)
- Retrieved From Bank → Move To Box (Action: Move Back To Box)
- Returned From Bank / Returned / Retrieved From Bank → Under Collection (Action: Reassign To Bank)

#### Workflow برای چک‌های پرداختنی (Payable Cheque Workflow)
- Document Type: `Cheque`
- Workflow State Field: `workflow_state`
- Condition: `doc.cheque_type == "Payable"`

**States:**
1. Payment Request Created (doc_status: 0)
2. Select Bank (doc_status: 0)
3. Issued (doc_status: 1) - ایجاد JE
4. Mark As Printed (doc_status: 1)
5. First Signature Done (doc_status: 1)
6. Second Signature Done (doc_status: 1)
7. Notify Supplier (doc_status: 1)
8. Deliver To Supplier (doc_status: 1)
9. Mark Registered In Sayad (doc_status: 1)
10. Mark Sayad Success (doc_status: 1)
11. Cleared (doc_status: 1) - ایجاد JE
12. Mark As Void (doc_status: 1) - فقط Cheque Manager

**Transitions:**
- Payment Request Created → Select Bank (Action: Select Bank)
- Select Bank → Issued (Action: Issue Cheque)
- Issued → Mark As Printed (Action: Mark As Printed)
- Mark As Printed → First Signature Done (Action: First Signature Done)
- First Signature Done → Second Signature Done (Action: Second Signature Done)
- Second Signature Done → Notify Supplier (Action: Notify Supplier)
- Notify Supplier → Deliver To Supplier (Action: Deliver To Supplier)
- Deliver To Supplier → Mark Registered In Sayad (Action: Mark Registered In Sayad)
- Mark Registered In Sayad → Mark Sayad Success (Action: Mark Sayad Success)
- Mark Sayad Success → Cleared (Action: Mark Sayad Success)
- هر وضعیت (قبل از Cleared) → Mark As Void (Action: Mark As Void) - فقط Cheque Manager

### 2. تنظیم Permissions
- اطمینان حاصل کنید که نقش‌های `Cheque User` و `Cheque Manager` به درستی تنظیم شده‌اند
- `Cheque Manager` باید دسترسی Submit و Cancel داشته باشد

### 3. تست
1. ایجاد چک دریافتنی جدید و تست workflow
2. ایجاد چک پرداختنی جدید و تست workflow
3. بررسی ایجاد خودکار Journal Entry
4. تست جلوگیری از حذف سندهای نهایی شده

## نکات مهم

1. **Journal Entry خودکار**: هنگام تغییر workflow state به وضعیت‌های مالی، Journal Entry به‌صورت خودکار ایجاد و Submit می‌شود.

2. **Submit خودکار**: هنگام ایجاد Journal Entry، سند Cheque نیز به‌صورت خودکار Submit می‌شود (اگر قبلاً Submit نشده باشد).

3. **جلوگیری از حذف**: سندهای Submit شده یا دارای Journal Entry قابل حذف نیستند.

4. **همگام‌سازی Status**: فیلد `status` به‌صورت خودکار با `workflow_state` همگام می‌شود.

## مرجع

- [Frappe Workflow Documentation](https://frappeframework.com/docs/user/en/workflows)
- Repository مرجع: https://github.com/hfaridgit/Cheque-Management/tree/master/cheque_management

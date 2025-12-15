# راهنمای کامل راه‌اندازی Workflow برای مدیریت چک

## ✅ وضعیت فعلی

همه چیز برای استفاده از Workflow آماده است:
- ✅ فیلد `workflow_state` اضافه شده
- ✅ Workflow States ایجاد شده
- ✅ Workflow Actions ایجاد شده
- ✅ Hooks برای ایجاد خودکار Journal Entry پیاده‌سازی شده
- ✅ جلوگیری از حذف سندهای نهایی شده پیاده‌سازی شده

## 📋 مراحل راه‌اندازی

### مرحله 1: Import Workflow States و Actions

Workflow States و Actions قبلاً در fixtures اضافه شده‌اند. برای import:

```bash
cd /workspace/development/frappe-bench
bench --site [your-site] migrate
```

یا اگر می‌خواهید فقط fixtures را import کنید:

```bash
bench --site [your-site] import-fixtures
```

### مرحله 2: ایجاد Workflow در Frappe UI

#### 2.1. Workflow برای چک‌های دریافتنی

1. به **Setup > Workflow > Workflow** بروید
2. روی **New** کلیک کنید
3. تنظیمات زیر را وارد کنید:

**اطلاعات اصلی:**
- **Workflow Name**: `Receivable Cheque Workflow`
- **Document Type**: `Cheque`
- **Workflow State Field**: `workflow_state`
- **Is Active**: ✅ فعال
- **Send Email Alert**: (اختیاری)

**Condition (شرط):**
```
doc.cheque_type == "Receivable"
```

این شرط باعث می‌شود workflow فقط برای چک‌های دریافتنی فعال شود.

#### 2.2. اضافه کردن States

برای هر state، روی **Add Row** کلیک کنید و اطلاعات زیر را وارد کنید:

**State 1: Received From Customer**
- State: `Received From Customer`
- Doc Status: `0` (Draft)
- Allow Edit: `All` یا `Cheque User, Cheque Manager`
- Style: `Info`

**State 2: Waiting For Sayad**
- State: `Waiting For Sayad`
- Doc Status: `0`
- Allow Edit: `All`
- Style: `Warning`

**State 3: Registered In Sayad**
- State: `Registered In Sayad`
- Doc Status: `0`
- Allow Edit: `All`
- Style: `Success`

**State 4: Move To Box**
- State: `Move To Box`
- Doc Status: `0`
- Allow Edit: `All`
- Style: (خالی)

**State 5: Under Collection** ⚠️ **ایجاد JE خودکار**
- State: `Under Collection`
- Doc Status: `1` (Submitted)
- Allow Edit: `Cheque Manager`
- Style: `Info`

**State 6: Collected** ⚠️ **ایجاد JE خودکار**
- State: `Collected`
- Doc Status: `1`
- Allow Edit: `Cheque Manager`
- Style: `Success`

**State 7: Returned From Bank** ⚠️ **ایجاد JE خودکار**
- State: `Returned From Bank`
- Doc Status: `1`
- Allow Edit: `All`
- Style: `Danger`

**State 8: Returned**
- State: `Returned`
- Doc Status: `1`
- Allow Edit: `All`
- Style: `Danger`

**State 9: Return To Customer**
- State: `Return To Customer`
- Doc Status: `1`
- Allow Edit: `All`
- Style: `Danger`

**State 10: Retrieved From Bank**
- State: `Retrieved From Bank`
- Doc Status: `0`
- Allow Edit: `All`
- Style: `Warning`

#### 2.3. اضافه کردن Transitions

برای هر transition، روی **Add Row** در بخش Transitions کلیک کنید:

**Transition 1:**
- State: `Received From Customer`
- Action: `Mark Waiting For Sayad`
- Next State: `Waiting For Sayad`
- Allowed: `All` یا `Cheque User, Cheque Manager`

**Transition 2:**
- State: `Waiting For Sayad`
- Action: `Mark Registered In Sayad`
- Next State: `Registered In Sayad`
- Allowed: `All`

**Transition 3:**
- State: `Registered In Sayad`
- Action: `Move To Box`
- Next State: `Move To Box`
- Allowed: `All`

**Transition 4:** ⚠️ **ایجاد JE خودکار**
- State: `Received From Customer` یا `Move To Box`
- Action: `Assign To Bank`
- Next State: `Under Collection`
- Allowed: `All`
- **نکته**: این transition باید از دو state به `Under Collection` برود

**Transition 5:** ⚠️ **ایجاد JE خودکار - فقط Cheque Manager**
- State: `Under Collection`
- Action: `Mark As Collected`
- Next State: `Collected`
- Allowed: `Cheque Manager`

**Transition 6:** ⚠️ **ایجاد JE خودکار**
- State: `Under Collection`
- Action: `Mark As Returned From Bank`
- Next State: `Returned From Bank`
- Allowed: `All`

**Transition 7:**
- State: `Returned From Bank`
- Action: `Return To Customer`
- Next State: `Return To Customer`
- Allowed: `All`

**Transition 8:**
- State: `Under Collection`
- Action: `Retrieve From Bank`
- Next State: `Retrieved From Bank`
- Allowed: `All`

**Transition 9:**
- State: `Retrieved From Bank`
- Action: `Move To Box` (یا یک action جدید)
- Next State: `Move To Box`
- Allowed: `All`

**Transition 10:** ⚠️ **ایجاد JE خودکار**
- State: `Returned From Bank` یا `Returned` یا `Retrieved From Bank`
- Action: `Reassign To Bank`
- Next State: `Under Collection`
- Allowed: `All`

#### 2.4. ذخیره و فعال‌سازی

1. روی **Save** کلیک کنید
2. مطمئن شوید **Is Active** فعال است
3. Workflow آماده استفاده است!

---

### مرحله 3: ایجاد Workflow برای چک‌های پرداختنی

مراحل مشابه است، اما با تنظیمات زیر:

**اطلاعات اصلی:**
- **Workflow Name**: `Payable Cheque Workflow`
- **Document Type**: `Cheque`
- **Workflow State Field**: `workflow_state`
- **Is Active**: ✅ فعال

**Condition:**
```
doc.cheque_type == "Payable"
```

**States:**

1. **Payment Request Created** (doc_status: 0)
2. **Select Bank** (doc_status: 0)
3. **Issued** (doc_status: 1) ⚠️ **ایجاد JE خودکار**
4. **Mark As Printed** (doc_status: 1)
5. **First Signature Done** (doc_status: 1)
6. **Second Signature Done** (doc_status: 1)
7. **Notify Supplier** (doc_status: 1)
8. **Deliver To Supplier** (doc_status: 1)
9. **Mark Registered In Sayad** (doc_status: 1)
10. **Mark Sayad Success** (doc_status: 1)
11. **Cleared** (doc_status: 1) ⚠️ **ایجاد JE خودکار**
12. **Mark As Void** (doc_status: 1) - فقط Cheque Manager

**Transitions:**

1. Payment Request Created → Select Bank (Action: Select Bank)
2. Select Bank → Issued (Action: Issue Cheque) ⚠️ **ایجاد JE**
3. Issued → Mark As Printed (Action: Mark As Printed)
4. Mark As Printed → First Signature Done (Action: First Signature Done)
5. First Signature Done → Second Signature Done (Action: Second Signature Done)
6. Second Signature Done → Notify Supplier (Action: Notify Supplier)
7. Notify Supplier → Deliver To Supplier (Action: Deliver To Supplier)
8. Deliver To Supplier → Mark Registered In Sayad (Action: Mark Registered In Sayad)
9. Mark Registered In Sayad → Mark Sayad Success (Action: Mark Sayad Success)
10. Mark Sayad Success → Cleared (Action: Mark Sayad Success) ⚠️ **ایجاد JE**
11. هر state (قبل از Cleared) → Mark As Void (Action: Mark As Void) - فقط Cheque Manager

---

## 🔧 تست Workflow

### تست 1: ایجاد چک دریافتنی

1. یک چک دریافتنی جدید ایجاد کنید
2. `workflow_state` باید به صورت خودکار `Received From Customer` باشد
3. دکمه‌های workflow را در فرم ببینید
4. روی "Mark Waiting For Sayad" کلیک کنید
5. `workflow_state` باید به `Waiting For Sayad` تغییر کند
6. `status` باید به صورت خودکار همگام شود

### تست 2: ایجاد Journal Entry خودکار

1. چک را به وضعیت `Move To Box` ببرید
2. روی "Assign To Bank" کلیک کنید
3. `workflow_state` باید به `Under Collection` تغییر کند
4. **Journal Entry باید به صورت خودکار ایجاد و Submit شود**
5. سند Cheque باید به صورت خودکار Submit شود

### تست 3: جلوگیری از حذف

1. یک چک در وضعیت `Collected` ایجاد کنید
2. سعی کنید آن را حذف کنید
3. باید خطا بدهد: "Cannot delete a Cheque in final state"

---

## ⚙️ تنظیمات پیشرفته

### 1. غیرفعال کردن Custom Buttons (اختیاری)

اگر می‌خواهید فقط از Workflow استفاده کنید:

1. به **Setup > Customization > Client Script** بروید
2. Script های زیر را پیدا کنید:
   - "Receivable Cheque Action Buttons"
   - "Payable Cheque Action Buttons"
3. آنها را **Disable** کنید یا **Delete** کنید

### 2. اضافه کردن Approval Workflow

می‌توانید برای برخی transitions نیاز به approval اضافه کنید:

1. در Transition، **Allowed** را به یک Role خاص تنظیم کنید
2. می‌توانید **Send Email Alert** را فعال کنید
3. می‌توانید **Next Action Email Template** تنظیم کنید

### 3. اضافه کردن Conditions

می‌توانید برای transitions شرط اضافه کنید:

مثال: فقط برای مبلغ‌های بالای 1,000,000:
```
doc.cheque_amount > 1000000
```

---

## 📝 نکات مهم

1. **Journal Entry خودکار**: هنگام تغییر workflow state به وضعیت‌های مالی (Under Collection, Collected, Issued, Cleared)، Journal Entry به صورت خودکار ایجاد می‌شود.

2. **Submit خودکار**: هنگام ایجاد Journal Entry، سند Cheque نیز به صورت خودکار Submit می‌شود.

3. **همگام‌سازی Status**: فیلد `status` به صورت خودکار با `workflow_state` همگام می‌شود.

4. **جلوگیری از حذف**: سندهای Submit شده یا دارای Journal Entry قابل حذف نیستند.

5. **دو Workflow جداگانه**: باید دو workflow جداگانه برای Receivable و Payable ایجاد کنید.

---

## 🐛 عیب‌یابی

### مشکل: Workflow Actions نمایش داده نمی‌شوند

**راه حل:**
1. مطمئن شوید workflow **Is Active** است
2. مطمئن شوید **Condition** درست است
3. مطمئن شوید `workflow_state` field در DocType وجود دارد
4. صفحه را Refresh کنید

### مشکل: Journal Entry ایجاد نمی‌شود

**راه حل:**
1. مطمئن شوید hooks در `hooks.py` فعال هستند
2. مطمئن شوید Cheque Settings تنظیم شده است
3. لاگ‌های Frappe را بررسی کنید

### مشکل: سند Submit نمی‌شود

**راه حل:**
1. مطمئن شوید کاربر دسترسی Submit دارد
2. مطمئن شوید doc_status در state درست تنظیم شده است

---

## ✅ چک‌لیست نهایی

- [ ] Workflow States import شده‌اند
- [ ] Workflow Actions import شده‌اند
- [ ] Receivable Cheque Workflow ایجاد شده
- [ ] Payable Cheque Workflow ایجاد شده
- [ ] هر دو workflow فعال هستند
- [ ] Condition برای هر workflow تنظیم شده
- [ ] تمام States اضافه شده‌اند
- [ ] تمام Transitions اضافه شده‌اند
- [ ] Permissions برای هر transition تنظیم شده
- [ ] تست ایجاد چک انجام شده
- [ ] تست ایجاد Journal Entry انجام شده
- [ ] تست جلوگیری از حذف انجام شده

---

## 📚 منابع

- [Frappe Workflow Documentation](https://frappeframework.com/docs/user/en/workflows)
- فایل `WORKFLOW_IMPLEMENTATION.md` برای جزئیات فنی
- فایل `WORKFLOW_VS_CUSTOM_BUTTONS.md` برای مقایسه روش‌ها

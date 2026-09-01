/**
 * Asset Request v4.4.0 Playwright E2E — acquisition only.
 * DB is source of truth; UI checks are secondary (project E2E standard).
 */
import { chromium } from "/tmp/e2e-npm/node_modules/playwright/index.mjs";
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";
import {
  benchExecute,
  getDocumentState,
  waitDocumentState,
  SITE,
} from "../../e2e/e2e_playwright_db.mjs";

const SITE_HEADERS = { "X-Frappe-Site-Name": SITE };

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SCREEN = path.join(__dirname, "screenshots", "asset_request");
const BASE =
  process.env.FRAPPE_E2E_BASE_URL || "http://development.localhost:8000";

const APPLY_WF =
  "erpnext_extensions.asset_usage_depreciation.e2e.asset_request_prep.apply_asset_request_workflow";

async function shot(page, name) {
  fs.mkdirSync(SCREEN, { recursive: true });
  const p = path.join(SCREEN, `${name}.png`);
  await page.screenshot({ path: p, fullPage: true });
  return p;
}

async function login(page, email, password) {
  await page.goto(`${BASE}/login`, {
    waitUntil: "domcontentloaded",
    timeout: 120000,
  });
  const emailSel = '#login_email, input[name="usr"], input[type="email"]';
  const passSel = '#login_password, input[name="pwd"], input[type="password"]';
  await page.locator(emailSel).first().fill(email);
  await page.locator(passSel).first().fill(password);
  await page.click('button[type="submit"]');
  try {
    await page.waitForURL(/\/(app|desk)/, { timeout: 60000 });
  } catch (e) {
    await shot(page, `login_fail_${email.replace(/[^a-z0-9]/gi, "_")}`).catch(() => {});
    const msg = await page.evaluate(() => document.body.innerText.slice(0, 500));
    throw new Error(`login failed for ${email}: ${msg.replace(/\s+/g, " ").slice(0, 240)}`);
  }
}

async function openForm(page, name) {
  const url = name
    ? `${BASE}/app/asset-request/${encodeURIComponent(name)}`
    : `${BASE}/app/asset-request/new`;
  await page.goto(url, { waitUntil: "domcontentloaded", timeout: 60000 });
  try {
    await page.waitForFunction(
      (dt) => window.cur_frm?.doc?.doctype === dt && !window.cur_frm.is_loading,
      "Asset Request",
      { timeout: 25000 }
    );
    await page.waitForTimeout(400);
    return true;
  } catch {
    return false;
  }
}

function applyWorkflow(name, action) {
  try {
    return { ok: true, ...benchExecute(APPLY_WF, { name, action }) };
  } catch (e) {
    const msg = `${e.stdout || e.message || e}`.slice(0, 1500);
    return { ok: false, error: msg };
  }
}

async function collectActionLabels(page) {
  return page.evaluate(() => {
    const menu = document.querySelector(
      ".actions-btn-group .dropdown-toggle, .btn-group .dropdown-toggle"
    );
    if (menu) {
      try {
        menu.click();
      } catch {
        /* ignore */
      }
    }
    return Array.from(document.querySelectorAll("button, a, .dropdown-item")).map(
      (el) => (el.textContent || "").trim()
    );
  });
}


async function loginAs(page, email, password) {
  await login(page, email, password);
}

async function openAsUser(browser, email, password) {
  const ctx = await browser.newContext({
    locale: "en-US",
    viewport: { width: 1600, height: 950 },
    extraHTTPHeaders: SITE_HEADERS,
  });
  const page = await ctx.newPage();
  page.setDefaultTimeout(180000);
  await login(page, email, password);
  const user = await page.evaluate(() => window.frappe?.session?.user || null);
  return { ctx, page, user };
}

function inspectRequest(name) {
  return benchExecute(
    "erpnext_extensions.asset_usage_depreciation.e2e.asset_request_prep.inspect_asset_request",
    { name }
  );
}

async function clickWorkflowApprove(page) {
  return clickWorkflowAction(page, /AR Approve|^Approve$/i, "AR Approve");
}

async function clickWorkflowSubmitForApproval(page) {
  return clickWorkflowAction(
    page,
    /AR Submit for Approval|Submit for Approval/i,
    "AR Submit for Approval"
  );
}

async function clickWorkflowAction(page, pattern, actionName) {
  const clicked = await page.evaluate((reSource) => {
    const re = new RegExp(reSource, "i");
    const nodes = Array.from(
      document.querySelectorAll("button, .dropdown-item, a.grey-link, .actions-btn-group .btn")
    );
    const el = nodes.find((e) => re.test((e.textContent || "").trim()));
    if (el) {
      el.click();
      return true;
    }
    return false;
  }, pattern.source);
  await page.waitForTimeout(400);
  const confirm = page.locator(".modal-footer .btn-primary:visible");
  if (await confirm.count()) {
    await confirm.first().click();
  }
  if (!clicked) {
    await page.evaluate(async (action) => {
      if (!window.cur_frm) return;
      await frappe.xcall("frappe.model.workflow.apply_workflow", {
        doc: cur_frm.doc,
        action,
      });
    }, actionName);
  }
  await page.waitForTimeout(1200);
  return clicked;
}


async function clickCustomButton(page, pattern) {
  await page.evaluate(() => {
    const menu = document.querySelector(
      ".actions-btn-group .dropdown-toggle, .btn-group .dropdown-toggle"
    );
    if (menu) {
      try {
        menu.click();
      } catch (e) {
        /* ignore */
      }
    }
  });
  const clicked = await page.evaluate((re) => {
    const rx = new RegExp(re, "i");
    const nodes = Array.from(
      document.querySelectorAll("button, .dropdown-item, a.btn, .custom-actions .btn")
    );
    const el = nodes.find((e) => rx.test((e.textContent || "").trim()));
    if (el) {
      el.click();
      return (el.textContent || "").trim();
    }
    return null;
  }, pattern);
  return clicked;
}

async function hasFulfillmentActions(page) {
  const labels = await collectActionLabels(page);
  return {
    labels,
    check: labels.some((t) => /Check Availability/i.test(t)),
    issue: labels.some((t) => /Issue from Pool/i.test(t)),
    purchase: labels.some((t) => /Request Purchase/i.test(t)),
  };
}

async function saveNewAssetRequest(page, args) {
  await page.evaluate(async (payload) => {
    const frm = window.cur_frm;
    if (!frm) throw new Error("no frm");
    await frm.set_value("company", payload.company);
    await frm.set_value("employee", payload.employee);
    await frm.set_value("purpose", payload.purpose);
    await frm.set_value("transaction_date", frappe.datetime.get_today());
    await frm.set_value("required_date", frappe.datetime.get_today());
    if (!(frm.doc.items || []).length) {
      frm.add_child("items");
    }
    frm.refresh_field("items");
    const row = frm.doc.items[0];
    await frappe.model.set_value(row.doctype, row.name, "requested_item_code", payload.item);
    await frappe.model.set_value(row.doctype, row.name, "qty", 1);
    frm.refresh_field("items");
  }, args);
  const saveBtn = page.locator(".page-actions .primary-action, button.primary-action").first();
  if (await saveBtn.count()) {
    await saveBtn.click();
  } else {
    await page.evaluate(() => {
      window.cur_frm.save();
    });
  }
  const started = Date.now();
  while (Date.now() - started < 40000) {
    const state = await page.evaluate(() => {
      const frm = window.cur_frm;
      const msg = Array.from(document.querySelectorAll(".msgprint, .modal-body, .alert"))
        .map((el) => (el.textContent || "").trim())
        .filter(Boolean)
        .slice(0, 5);
      return {
        name: frm?.doc?.name || null,
        is_new: Boolean(frm?.is_new?.() || frm?.doc?.__islocal),
        messages: msg,
      };
    });
    if (state.name && !state.is_new && !String(state.name).startsWith("new-")) {
      return { ok: true, name: state.name, error: null };
    }
    if (state.messages.some((m) => /permission|mandatory|error|خطا/i.test(m))) {
      return { ok: false, name: null, error: state.messages.join(" | ") };
    }
    await page.waitForTimeout(500);
  }
  const leftover = await page.evaluate(() =>
    (document.body.innerText || "").replace(/\s+/g, " ").slice(0, 500)
  );
  return { ok: false, name: null, error: leftover };
}

async function confirmVisibleModal(page) {
  const btn = page.locator(".modal-footer .btn-primary:visible").last();
  if (await btn.count()) {
    await btn.click();
    return true;
  }
  return false;
}

async function run() {
  const prep = benchExecute(
    "erpnext_extensions.asset_usage_depreciation.e2e.asset_request_prep.prepare_asset_request_e2e"
  );
  const screenshots = {};
  const results = {};
  const consoleErrors = [];

  const browser = await chromium.launch({
    headless: true,
    args: ["--no-sandbox", "--disable-dev-shm-usage"],
  });
  const context = await browser.newContext({
    locale: "en-US",
    viewport: { width: 1600, height: 950 },
    extraHTTPHeaders: SITE_HEADERS,
  });
  const page = await context.newPage();
  page.setDefaultTimeout(180000);
  page.on("console", (msg) => {
    if (msg.type() === "error") consoleErrors.push(msg.text());
  });
  page.on("pageerror", (err) => consoleErrors.push(`pageerror: ${err}`));

  try {
    // Prep already set e2e user passwords. Skip Administrator desk login
    // (this site does not use admin/admin).

    // Scenario 1 — real Employee login: New → save → Submit for Approval
    const empSession = await openAsUser(browser, prep.emp_email, prep.password);
    const empPage = empSession.page;
    empPage.on("pageerror", (err) => consoleErrors.push(`emp pageerror: ${err}`));
    results.employee_session_user = await empPage.evaluate(
      () => window.frappe?.session?.user || null
    );
    results.employee_logged_in = results.employee_session_user === prep.emp_email;
    await openForm(empPage, null);
    results.form_loaded = await empPage.evaluate(
      () => window.cur_frm?.doc?.doctype === "Asset Request"
    );
    results.item_query_fixed_asset = await empPage.evaluate(() => {
      const grid = window.cur_frm?.fields_dict?.items?.grid;
      const field = grid?.get_field?.("requested_item_code");
      const q = field?.get_query ? field.get_query() : null;
      const filters = q?.filters || q || {};
      return Number(filters.is_fixed_asset) === 1;
    });
    const created = await saveNewAssetRequest(empPage, {
      company: prep.company,
      employee: prep.employee,
      item: prep.employee_item || prep.samsung,
      purpose: "E2E employee create",
    });
    screenshots.employee_create_attempt = await shot(empPage, "01_employee_create");
    const createdName = created.name;
    results.created_name = createdName;
    results.employee_save_error = created.error || null;
    results.employee_save_ok = Boolean(created.ok && createdName);
    if (createdName) {
      const createdWait = await waitDocumentState("Asset Request", createdName, {
        docstatus: 0,
      });
      results.employee_save_ok = Boolean(created.ok && createdWait.ok);
    }
    await openForm(empPage, createdName);
    results.requested_item_visible = await empPage.evaluate(
      (item) =>
        (window.cur_frm?.doc?.items || []).some(
          (r) => r.requested_item_code === item
        ),
      prep.employee_item || prep.samsung
    );
    const createLabels = await collectActionLabels(empPage);
    results.submit_action_visible = createLabels.some((t) =>
      /AR Submit for Approval|Submit for Approval/i.test(t)
    );
    const empDraftFulfillment = await hasFulfillmentActions(empPage);
    results.employee_no_fulfillment_on_draft =
      !empDraftFulfillment.check &&
      !empDraftFulfillment.issue &&
      !empDraftFulfillment.purchase;
    screenshots.employee_create = await shot(empPage, "01_employee_create");

    await clickWorkflowSubmitForApproval(empPage);
    const submittedWait = createdName
      ? await waitDocumentState("Asset Request", createdName, {
          workflow_state: "Pending Manager Approval",
        })
      : { ok: false };
    const submittedInspect = createdName ? inspectRequest(createdName) : {};
    results.employee_submit_error = submittedInspect.exists
      ? null
      : "submit did not persist";
    results.employee_submit_workflow =
      Boolean(submittedWait.ok) &&
      submittedInspect.workflow_state === "Pending Manager Approval" &&
      Number(submittedInspect.docstatus) === 0;
    await openForm(empPage, createdName);
    results.employee_submit_ui_state = await empPage.evaluate(
      () => window.cur_frm?.doc?.workflow_state || window.cur_frm?.doc?.status
    );
    results.employee_permission_error = await empPage.evaluate(() => {
      const body = document.body.innerText || "";
      return /PermissionError|not permitted|Insufficient Permission/i.test(body)
        ? body.slice(0, 240)
        : null;
    });
    results.employee_manager_approver_stamped =
      Boolean(createdName) &&
      inspectRequest(createdName).manager_approver === prep.mgr_email;

    await openForm(empPage, prep.approved_request);
    const empApprovedFulfillment = await hasFulfillmentActions(empPage);
    results.employee_no_fulfillment_on_approved =
      !empApprovedFulfillment.check &&
      !empApprovedFulfillment.issue &&
      !empApprovedFulfillment.purchase;
    await empSession.ctx.close();

    results.mgr_has_no_ar_manager_role = prep.mgr_has_ar_manager_role === false;

    // Negative — unrelated Employee must not see manager actions
    const otherSession = await openAsUser(browser, prep.other_email, prep.password);
    const otherPage = otherSession.page;
    const otherOpened = await openForm(otherPage, prep.pending_request);
    results.unrelated_employee_opened = otherOpened;
    const otherLabels = otherOpened ? await collectActionLabels(otherPage) : [];
    results.unrelated_employee_no_manager_actions =
      !otherLabels.some((t) => /AR Approve|^Approve$/i.test(t)) &&
      !otherLabels.some((t) => /AR Reject|^Reject$/i.test(t)) &&
      !otherLabels.some((t) => /AR Send Back|Send Back/i.test(t));
    await otherSession.ctx.close();

    // Negative — unstamped Asset Request Manager must not see manager actions
    const arMgrSession = await openAsUser(browser, prep.ar_mgr_email, prep.password);
    const arMgrPage = arMgrSession.page;
    const arMgrOpened = await openForm(arMgrPage, prep.pending_request);
    results.unstamped_ar_manager_opened = arMgrOpened;
    const arMgrLabels = arMgrOpened ? await collectActionLabels(arMgrPage) : [];
    results.unstamped_ar_manager_no_manager_actions =
      !arMgrLabels.some((t) => /AR Approve|^Approve$/i.test(t)) &&
      !arMgrLabels.some((t) => /AR Reject|^Reject$/i.test(t)) &&
      !arMgrLabels.some((t) => /AR Send Back|Send Back/i.test(t));
    await arMgrSession.ctx.close();

    // Scenario 2 — Manager approval as the real line manager (Employee role only)
    const mgrSession = await openAsUser(browser, prep.mgr_email, prep.password);
    const mgrPage = mgrSession.page;
    await openForm(mgrPage, prep.pending_request);
    results.manager_session_user = await mgrPage.evaluate(
      () => window.frappe?.session?.user || null
    );
    results.manager_sees_pending = await mgrPage.evaluate(
      () =>
        (window.cur_frm?.doc?.workflow_state || window.cur_frm?.doc?.status || "")
          .toString()
          .includes("Pending")
    );
    const pendingLabels = await collectActionLabels(mgrPage);
    results.approve_action_visible = pendingLabels.some((t) =>
      /AR Approve|^Approve$/i.test(t)
    );
    results.reject_action_visible = pendingLabels.some((t) =>
      /AR Reject|^Reject$/i.test(t)
    );
    results.send_back_action_visible = pendingLabels.some((t) =>
      /AR Send Back|Send Back/i.test(t)
    );
    mgrPage.on("pageerror", (err) => consoleErrors.push(`mgr pageerror: ${err}`));
    await clickWorkflowApprove(mgrPage);
    await waitDocumentState("Asset Request", prep.pending_request, { docstatus: 1 });
    results.manager_still_logged_in = await mgrPage.evaluate(
      () => window.frappe?.session?.user || null
    );
    results.manager_not_guest = results.manager_still_logged_in && results.manager_still_logged_in !== "Guest";
    const afterApprove = getDocumentState("Asset Request", prep.pending_request, [
      "docstatus",
      "workflow_state",
      "fulfillment_status",
      "material_request",
    ]);
    results.manager_approve_ok =
      Number(afterApprove.docstatus) === 1 && afterApprove.workflow_state === "Approved";
    results.manager_approve_no_mr = !afterApprove.material_request;
    results.manager_fulfillment_waiting =
      afterApprove.fulfillment_status === "Waiting for fulfillment";
    results.manager_no_whitelist_error = !consoleErrors.some((e) =>
      /get_open_count|not whitelisted/i.test(e)
    );
    await openForm(mgrPage, prep.pending_request);
    screenshots.manager_approve = await shot(mgrPage, "02_manager_approve");
    await mgrSession.ctx.close();

    // Scenario 3 — Asset Manager: Check Availability + Issue from Pool
    const amSession = await openAsUser(browser, prep.am_email, prep.password);
    const amPage = amSession.page;
    await openForm(amPage, prep.approved_request);
    results.am_session_user = await amPage.evaluate(
      () => window.frappe?.session?.user || null
    );
    results.fulfillment_buttons = await amPage.evaluate(() => {
      const labels = Array.from(document.querySelectorAll("button, a, .btn, .dropdown-item")).map(
        (el) => (el.textContent || "").trim()
      );
      return {
        check: labels.some((t) => /Check Availability/i.test(t)),
        issue: labels.some((t) => /Issue from Pool/i.test(t)),
        purchase: labels.some((t) => /Request Purchase/i.test(t)),
      };
    });
    results.am_check_clicked = await clickCustomButton(amPage, "Check Availability");
    await amPage.waitForTimeout(800);
    results.am_issue_clicked = await clickCustomButton(amPage, "Issue from Pool");
    await amPage.waitForTimeout(500);
    results.am_picker_visible = await amPage.locator(".ar-pool-picker, .modal-dialog:visible").count().then((n) => n > 0);
    screenshots.asset_manager_picker = await shot(amPage, "03_asset_manager_picker");
    await confirmVisibleModal(amPage);
    await amPage.waitForTimeout(400);
    results.am_substitute_confirmed = await confirmVisibleModal(amPage);
    await amPage.waitForTimeout(1200);
    const issued = getDocumentState("Asset Request", prep.approved_request, [
      "docstatus",
      "fulfillment_status",
    ]);
    const issuedInspect = inspectRequest(prep.approved_request);
    results.am_issue_ok =
      Number(issued.docstatus) === 1 &&
      (issuedInspect.asset_movements || []).length > 0;
    results.am_issue_movements = issuedInspect.asset_movements || [];
    results.am_still_logged_in = await amPage.evaluate(
      () => window.frappe?.session?.user || null
    );
    results.am_not_guest = results.am_still_logged_in && results.am_still_logged_in !== "Guest";
    screenshots.asset_manager_fulfillment = await shot(amPage, "03_asset_manager_fulfillment");

    // Scenario 4 — Asset Manager: Request Purchase
    await openForm(amPage, prep.purchase_request);
    results.am_purchase_clicked = await clickCustomButton(amPage, "Request Purchase");
    await amPage.waitForTimeout(400);
    await confirmVisibleModal(amPage);
    await amPage.waitForTimeout(800);
    await waitDocumentState("Asset Request", prep.purchase_request, { docstatus: 1 });
    const purchaseDb = getDocumentState("Asset Request", prep.purchase_request, [
      "name",
      "docstatus",
      "material_request",
      "fulfillment_status",
    ]);
    results.purchase_submitted =
      purchaseDb.exists && Number(purchaseDb.docstatus) === 1;
    results.purchase_mr = purchaseDb.material_request;
    results.purchase_mr_linked = false;
    if (results.purchase_mr) {
      const mr = getDocumentState("Material Request", results.purchase_mr, [
        "name",
        "custom_asset_request",
        "material_request_type",
      ]);
      results.purchase_mr_linked =
        mr.exists && mr.custom_asset_request === prep.purchase_request;
      results.purchase_mr_purpose = mr.material_request_type;
    }
    results.am_purchase_still_logged_in = await amPage.evaluate(
      () => window.frappe?.session?.user || null
    );
    await openForm(amPage, prep.purchase_request);
    results.purchase_link_on_form = await amPage.evaluate(
      (mr) =>
        window.cur_frm?.doc?.material_request === mr ||
        Boolean(window.cur_frm?.doc?.material_request),
      results.purchase_mr
    );
    screenshots.purchase_path = await shot(amPage, "04_purchase_path");

    // Scenario 5 — List + reports as Asset Manager
    await amPage.goto(`${BASE}/app/asset-request`, {
      waitUntil: "domcontentloaded",
      timeout: 180000,
    });
    await amPage.waitForTimeout(1500);
    results.list_loaded = await amPage.evaluate(
      () =>
        window.cur_list?.doctype === "Asset Request" ||
        document.querySelector(".frappe-list, .list-view, .list-row") != null ||
        /Asset Request/i.test(document.body.innerText || "")
    );
    results.list_filters = await amPage.evaluate((company) => {
      try {
        if (window.cur_list?.filter_area?.add) {
          window.cur_list.filter_area.add([
            ["Asset Request", "company", "=", company],
          ]);
        }
      } catch {
        /* ignore */
      }
      return Boolean(
        document.querySelector(
          ".filter-selector, .standard-filter-section, .filter-box, .list-filters"
        ) || window.cur_list
      );
    }, prep.company);
    screenshots.list = await shot(amPage, "05_list");

    await amPage.goto(
      `${BASE}/app/query-report/Requested%20Asset%20vs%20Fulfilled%20Asset`,
      { waitUntil: "domcontentloaded", timeout: 180000 }
    );
    await amPage.waitForTimeout(2500);
    results.requested_vs_fulfilled_report = await amPage.evaluate(
      () =>
        /Requested Asset vs Fulfilled Asset/i.test(document.body.innerText || "") ||
        Boolean(document.querySelector(".datatable, .report-wrapper, .query-report"))
    );
    screenshots.report_requested_vs_fulfilled = await shot(
      amPage,
      "05_report_requested_vs_fulfilled"
    );

    await amPage.goto(`${BASE}/app/query-report/Pending%20Asset%20Requests`, {
      waitUntil: "domcontentloaded",
      timeout: 180000,
    });
    await amPage.waitForTimeout(2500);
    results.pending_report = await amPage.evaluate(
      () =>
        /Pending Asset Requests/i.test(document.body.innerText || "") ||
        Boolean(document.querySelector(".datatable, .report-wrapper, .query-report"))
    );
    screenshots.report_pending = await shot(amPage, "05_report_pending");
    await amSession.ctx.close();
  } finally {
    const leftoverErrors = consoleErrors.filter((e) => {
      if (/get_open_count|not whitelisted|logged out|Session expired|Please login/i.test(e)) {
        return true;
      }
      return !/favicon|Failed to load resource: the server responded with a status of (404|400|500)|socket\.io|Unauthorized.*fetch failed|get_open_form is not a function|refresh_comments_count/i.test(
        e
      );
    });
    const benign = leftoverErrors;

    const pass = Boolean(
      results.form_loaded &&
        results.item_query_fixed_asset &&
        results.employee_logged_in &&
        results.employee_save_ok &&
        results.requested_item_visible &&
        results.employee_submit_workflow &&
        results.employee_manager_approver_stamped &&
        !results.employee_permission_error &&
        results.employee_no_fulfillment_on_draft &&
        results.employee_no_fulfillment_on_approved &&
        results.mgr_has_no_ar_manager_role &&
        results.unrelated_employee_no_manager_actions &&
        results.unstamped_ar_manager_no_manager_actions &&
        results.manager_sees_pending &&
        results.approve_action_visible &&
        results.reject_action_visible &&
        results.send_back_action_visible &&
        results.manager_approve_ok &&
        results.manager_not_guest &&
        results.manager_approve_no_mr &&
        results.am_not_guest &&
        Boolean(results.am_check_clicked) &&
        Boolean(results.am_issue_clicked) &&
        results.am_picker_visible &&
        results.am_issue_ok &&
        results.purchase_submitted &&
        results.purchase_mr_linked &&
        results.list_loaded &&
        results.requested_vs_fulfilled_report &&
        results.pending_report &&
        benign.length === 0
    );

    console.log(
      JSON.stringify(
        {
          pass,
          all_ok: pass,
          prep: {
            pending_request: prep.pending_request,
            approved_request: prep.approved_request,
            purchase_request: prep.purchase_request,
            purchase_mr: prep.purchase_mr,
          },
          results,
          screenshots,
          benign_console_errors: benign.slice(0, 20),
        },
        null,
        2
      )
    );
    await browser.close();
    if (!pass) {
      process.exitCode = 1;
    }
  }
}

run().catch((e) => {
  console.error(e);
  process.exit(1);
});

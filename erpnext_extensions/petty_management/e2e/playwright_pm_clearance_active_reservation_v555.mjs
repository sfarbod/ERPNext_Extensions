/**
 * v5.5.5 — Active reservation + Return + pending/correction Desk E2E.
 *
 * Scenario 1: Return via Actions (no API fallback) when available <= 0
 * Scenario 2: Draft reservation 700 + 301 blocked / 300 allowed
 * Scenario 3: Pending Manager/Finance reservation via real Desk workflow
 * Scenario 4: Return retains reservation (Desk Return, C=1 blocked)
 * Scenario 5: Full correction flow (Return → Draft → Save → resubmit → approvals)
 * Plus: Draft delete releases funding (Administrator Desk Menu Delete)
 *
 * NO frappe.call / apply_workflow API fallback for workflow actions.
 */
import { chromium } from "/tmp/e2e-npm/node_modules/playwright/index.mjs";
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";
import { benchExecutePrep } from "../../e2e/e2e_playwright_db.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SCREEN = path.join(__dirname, "screenshots", "pm_clearance_active_reservation_v555");
const BASE = process.env.FRAPPE_E2E_BASE_URL || "http://development.localhost:8000";
const PREP =
  "erpnext_extensions.petty_management.e2e.pm_clearance_active_reservation_v555_prep";

function assert(cond, msg) {
  if (!cond) throw new Error(msg);
}

function prep(method, kwargs = null) {
  return benchExecutePrep(`${PREP}.${method}`, kwargs);
}

function approxEq(a, b, eps = 0.01) {
  return Math.abs(Number(a) - Number(b)) <= eps;
}

async function login(page, email, password) {
  await page.goto(`${BASE}/login`, { waitUntil: "domcontentloaded", timeout: 120000 });
  await page.waitForSelector("#login_email", { state: "visible", timeout: 120000 });
  await page.locator("#login_email").first().fill(email, { timeout: 60000 });
  await page.locator("#login_password, input[type='password']").first().fill(password, {
    timeout: 60000,
  });
  await page.locator('button[type="submit"]').first().click();
  await page.waitForURL(/\/(app|desk)/, { timeout: 120000 });
}

async function openClearance(page, name) {
  await page.goto(`${BASE}/app/pm-clearance/${encodeURIComponent(name)}`, {
    waitUntil: "domcontentloaded",
    timeout: 180000,
  });
  await page.waitForFunction(
    (n) => window.cur_frm?.doc?.name === n && !window.cur_frm.is_loading,
    name,
    { timeout: 180000 }
  );
}

async function shot(page, name) {
  fs.mkdirSync(SCREEN, { recursive: true });
  const p = path.join(SCREEN, `${name}.png`);
  await page.screenshot({ path: p, fullPage: true });
  return p;
}

async function dismissDialogs(page) {
  for (let i = 0; i < 3; i++) {
    const visible = page.locator(".modal-dialog:visible");
    if (!(await visible.count())) break;
    const primary = visible.locator("button.btn-primary").first();
    const close = visible.locator(".btn-modal-close, button:has-text('Close')").first();
    if (await primary.isVisible().catch(() => false)) {
      await primary.click().catch(() => null);
    } else if (await close.isVisible().catch(() => false)) {
      await close.click().catch(() => null);
    } else {
      await page.keyboard.press("Escape").catch(() => null);
    }
    await page.waitForTimeout(300);
  }
}

/** Click workflow action from Desk Actions dropdown — no frappe.call fallback. */
async function clickWorkflowAction(page, actionLabel) {
  await page.waitForFunction(() => {
    const el = document.querySelector(".actions-btn-group .btn-primary, .actions-btn-group .btn");
    return !!(el && el.offsetParent !== null);
  }, { timeout: 120000 });
  await page.evaluate(async () => {
    if (window.cur_frm?.script_manager?.trigger) {
      try {
        await window.cur_frm.script_manager.trigger("refresh");
      } catch (_e) {
        /* ignore */
      }
    }
  });
  await page.waitForTimeout(500);
  const actionsBtn = page.locator(".actions-btn-group .btn-primary").first();
  if (await actionsBtn.count()) {
    await actionsBtn.click();
  } else {
    await page
      .locator(".actions-btn-group .btn")
      .filter({ hasText: /^Actions$/i })
      .first()
      .click();
  }
  await page.waitForTimeout(400);
  const escaped = actionLabel.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const item = page
    .locator(".actions-btn-group .dropdown-menu a.dropdown-item")
    .filter({ hasText: new RegExp(escaped, "i") })
    .first();
  await item.waitFor({ state: "visible", timeout: 60000 });
  const label = (await item.innerText()).trim();
  assert(new RegExp(escaped, "i").test(label), `unexpected action label: ${label}`);
  await item.click();
  await page.waitForTimeout(800);
  const modal = page.locator(".modal-dialog:visible");
  if (await modal.count()) {
    const primary = modal.locator("button.btn-primary").first();
    if (await primary.isVisible().catch(() => false)) {
      await primary.click().catch(() => null);
    }
  }
  await page.waitForTimeout(2500);
  await page.waitForFunction(() => !window.cur_frm?.is_loading, { timeout: 120000 });
}

async function deskSave(page) {
  await dismissDialogs(page);
  await page.getByRole("button", { name: /^Save$/i }).first().click({ timeout: 60000 });
  await page.waitForTimeout(1500);
  await page.waitForFunction(() => !window.cur_frm?.is_loading, { timeout: 180000 }).catch(() => null);
  return page.evaluate(() => {
    const dialogs = Array.from(
      document.querySelectorAll(".msgprint-dialog .modal-body, .modal-dialog .modal-body")
    ).filter((el) => el.offsetParent !== null);
    const msg = (dialogs[0]?.textContent || "").trim();
    const name = window.cur_frm?.doc?.name || "";
    const saved = !!(name && !String(name).startsWith("new-"));
    const dirty = !!(window.cur_frm?.is_dirty?.());
    return { name, saved, dirty, msg };
  });
}

async function fillNewClearance(page, fx, amount) {
  await page.goto(`${BASE}/app/pm-clearance/new`, {
    waitUntil: "domcontentloaded",
    timeout: 180000,
  });
  await page.waitForFunction(
    () => window.cur_frm?.doc?.doctype === "PM Clearance" && !window.cur_frm.is_loading,
    { timeout: 180000 }
  );
  await page.evaluate(
    async ({ company, employee, pi, req, amount }) => {
      const frm = window.cur_frm;
      await frm.set_value("company", company);
      await frm.set_value("employee", employee);
      await frm.set_value("transaction_date", frappe.datetime.get_today());
      while ((frm.doc.details || []).length) {
        frm.doc.details.pop();
      }
      while ((frm.doc.request_allocations || []).length) {
        frm.doc.request_allocations.pop();
      }
      const d = frm.add_child("details");
      d.settlement_type = "Purchase Invoice";
      d.purchase_invoice = pi;
      d.allocated_amount = amount;
      d.outstanding_amount = 1000;
      d.bill_no = `E2E-V555-${Date.now()}`;
      const r = frm.add_child("request_allocations");
      r.funding_source_type = "PM Request";
      r.pm_request = req;
      r.allocated_amount = amount;
      frm.refresh_fields();
      if (frm.trigger) {
        try {
          await frm.trigger("recalc_totals");
        } catch (_e) {
          /* ignore */
        }
      }
    },
    {
      company: fx.company,
      employee: fx.employee,
      pi: fx.purchase_invoice,
      req: fx.pm_request,
      amount,
    }
  );
  await page.waitForTimeout(500);
}

async function setAllocationOnForm(page, amount) {
  await page.evaluate(async (amt) => {
    const frm = window.cur_frm;
    if (frm.doc.details?.[0]) {
      frm.doc.details[0].allocated_amount = amt;
    }
    if (frm.doc.request_allocations?.[0]) {
      frm.doc.request_allocations[0].allocated_amount = amt;
    }
    frm.refresh_fields();
    frm.dirty();
    if (frm.trigger) {
      try {
        await frm.trigger("recalc_totals");
      } catch (_e) {
        /* ignore */
      }
    }
  }, amount);
  await page.waitForTimeout(300);
}

async function deskMenuDelete(page) {
  const menuBtn = page.locator(".menu-btn-group > .btn, .menu-btn-group button").first();
  await menuBtn.waitFor({ state: "visible", timeout: 60000 });
  await menuBtn.click();
  await page.waitForTimeout(500);
  // Frappe menu items may be <a> or <button class="dropdown-item">
  const del = page
    .locator(".menu-btn-group .dropdown-menu .dropdown-item, .menu-btn-group .dropdown-menu a, .menu-btn-group .dropdown-menu button")
    .filter({ hasText: /Delete/i })
    .first();
  await del.waitFor({ state: "visible", timeout: 60000 });
  await del.click();
  await page.waitForTimeout(500);
  const modal = page.locator(".modal-dialog:visible");
  await modal.waitFor({ timeout: 60000 });
  await modal.locator("button.btn-primary").first().click();
  await page.waitForTimeout(2500);
  // Confirm navigated away / doc gone
  await page.waitForFunction(
    () => {
      const n = window.cur_frm?.doc?.name || "";
      return !n || window.location.pathname.includes("/pm-clearance") && !window.location.pathname.includes(n);
    },
    { timeout: 120000 }
  ).catch(() => null);
}

async function newContext(browser) {
  const context = await browser.newContext({
    locale: "en-US",
    viewport: { width: 1600, height: 950 },
  });
  const page = await context.newPage();
  return { context, page };
}

async function main() {
  const evidence = { screenshots: {}, steps: {}, scenarios: {} };
  const browser = await chromium.launch({ headless: true });
  let context = null;
  let page = null;
  let fixtures = null;
  const restoreQueue = [];

  const trackRestore = (fx) => {
    if (fx?.purchase_invoice != null) {
      restoreQueue.push({
        purchase_invoice: fx.purchase_invoice,
        outstanding: fx.original_pi_outstanding,
      });
    }
  };

  try {
    // ---- Scenario 1: Return deadlock via real Actions click ----
    fixtures = prep("prepare_return_deadlock_fixture");
    assert(fixtures?.pm_clearance, "missing clearance");
    evidence.fixtures_return = fixtures;
    trackRestore(fixtures);
    assert(
      Number(fixtures.available_excl_pending) <= 0.01,
      `expected zero available, got ${fixtures.available_excl_pending}`
    );

    ({ context, page } = await newContext(browser));
    await login(page, fixtures.users.reviewer.email, fixtures.password);
    await openClearance(page, fixtures.pm_clearance);
    evidence.screenshots.s1_pending = await shot(page, "01_pending_finance_zero_avail");

    // Real Desk UI — Actions → PM Return for Correction (no API fallback)
    await clickWorkflowAction(page, "PM Return for Correction");
    await openClearance(page, fixtures.pm_clearance);
    evidence.screenshots.s1_after_return = await shot(page, "02_after_return_ui");

    const after = prep("get_clearance_snapshot", { pm_clearance: fixtures.pm_clearance });
    evidence.scenarios.return_deadlock = after;
    assert(after.name === fixtures.pm_clearance, "name changed");
    assert(after.workflow_state === "Draft", `expected Draft, got ${after.workflow_state}`);
    // Reservation retained (settled 1000 + returned draft 400)
    assert(Number(after.prior_all) >= 1400 - 0.01, `prior_all=${after.prior_all}`);

    const bodyText = await page.locator("body").innerText();
    assert(
      !/has no available balance for clearance/i.test(bodyText) ||
        after.workflow_state === "Draft",
      "availability error blocked Return"
    );
    await context.close();
    context = null;

    // ---- Scenario 2: Draft reservation (same validate path Desk save uses) ----
    const draftFx = prep("prepare_draft_reservation_fixture");
    evidence.fixtures_draft = draftFx;
    trackRestore(draftFx);
    const blocked301 = prep("try_sibling_alloc", {
      pm_request: draftFx.pm_request,
      employee: draftFx.employee,
      purchase_invoice: draftFx.purchase_invoice,
      company: draftFx.company,
      amount: 301,
    });
    evidence.scenarios.draft_301 = blocked301;
    assert(!blocked301.ok, "B=301 must be blocked");
    assert(/available|exceed/i.test(String(blocked301.error || "")), blocked301.error);
    const ok300 = prep("try_sibling_alloc", {
      pm_request: draftFx.pm_request,
      employee: draftFx.employee,
      purchase_invoice: draftFx.purchase_invoice,
      company: draftFx.company,
      amount: 300,
    });
    evidence.scenarios.draft_300 = ok300;
    assert(ok300.ok, `B=300 must pass: ${ok300.error}`);

    // ---- Scenario 3: Pending reservation via real Desk workflow ----
    const s3 = prep("prepare_pending_reservation_fixture");
    evidence.fixtures_pending = s3;
    trackRestore(s3);
    ({ context, page } = await newContext(browser));
    await login(page, s3.users.holder.email, s3.password);
    await openClearance(page, s3.clearance_a);
    await clickWorkflowAction(page, "PM Submit Finance Review");
    await openClearance(page, s3.clearance_a);
    let snapA = prep("get_clearance_snapshot", { pm_clearance: s3.clearance_a });
    assert(
      snapA.workflow_state === "Pending Manager Approval",
      `S3 A expected Pending Manager, got ${snapA.workflow_state}`
    );
    evidence.screenshots.s3_pending_manager = await shot(page, "03_pending_manager");

    await fillNewClearance(page, s3, 301);
    let save301 = await deskSave(page);
    evidence.scenarios.s3_pending_manager_301 = save301;
    assert(!save301.saved || save301.dirty, "B=301 must not save under Pending Manager");
    assert(
      /available|exceed|insufficient/i.test(save301.msg || ""),
      `S3 Pending Manager block msg missing: ${save301.msg}`
    );
    await dismissDialogs(page);

    const mgr = await newContext(browser);
    await login(mgr.page, s3.users.manager.email, s3.password);
    await openClearance(mgr.page, s3.clearance_a);
    await clickWorkflowAction(mgr.page, "PM Manager Approve");
    await openClearance(mgr.page, s3.clearance_a);
    snapA = prep("get_clearance_snapshot", { pm_clearance: s3.clearance_a });
    assert(
      snapA.workflow_state === "Pending Finance Review",
      `S3 A expected Pending Finance, got ${snapA.workflow_state}`
    );
    evidence.screenshots.s3_pending_finance = await shot(mgr.page, "04_pending_finance");
    await mgr.context.close();

    // Retry B=301 on same unsaved form
    save301 = await deskSave(page);
    evidence.scenarios.s3_pending_finance_301 = save301;
    assert(!save301.saved || save301.dirty, "B=301 must stay blocked under Pending Finance");
    assert(
      /available|exceed|insufficient/i.test(save301.msg || ""),
      `S3 Pending Finance block msg missing: ${save301.msg}`
    );
    await dismissDialogs(page);

    await setAllocationOnForm(page, 300);
    const save300 = await deskSave(page);
    evidence.scenarios.s3_pending_finance_300 = save300;
    assert(save300.saved && !String(save300.name).startsWith("new-"), `B=300 save failed: ${save300.msg}`);
    evidence.screenshots.s3_b300_saved = await shot(page, "05_b300_saved");

    const audit3 = prep("get_request_reservation_audit", { pm_request: s3.pm_request });
    evidence.scenarios.s3_audit = audit3;
    assert(approxEq(audit3.paid, 1000), `S3 paid=${audit3.paid}`);
    assert(approxEq(audit3.reserved_aggregate, 1000), `S3 aggregate=${audit3.reserved_aggregate}`);
    assert(!audit3.over_funded, "S3 over-funded");
    const rowA = (audit3.active_rows || []).find((r) => r.name === s3.clearance_a);
    const rowB = (audit3.active_rows || []).find((r) => r.name === save300.name);
    assert(rowA && approxEq(rowA.allocated_amount, 700), "S3 A reservation not 700");
    assert(rowB && approxEq(rowB.allocated_amount, 300), "S3 B reservation not 300");
    await context.close();
    context = null;

    // ---- Scenario 4: Return retains reservation ----
    const s4 = prep("prepare_return_retain_fixture");
    evidence.fixtures_return_retain = s4;
    trackRestore(s4);
    ({ context, page } = await newContext(browser));
    await login(page, s4.users.holder.email, s4.password);
    await openClearance(page, s4.clearance_a);
    await clickWorkflowAction(page, "PM Submit Finance Review");
    await openClearance(page, s4.clearance_a);
    snapA = prep("get_clearance_snapshot", { pm_clearance: s4.clearance_a });
    assert(
      snapA.workflow_state === "Pending Manager Approval",
      `S4 A expected Pending Manager, got ${snapA.workflow_state}`
    );
    const beforeReturn = prep("get_request_reservation_audit", { pm_request: s4.pm_request });
    evidence.scenarios.s4_before_return = beforeReturn;
    assert(approxEq(beforeReturn.reserved_aggregate, 1000), `S4 before=${beforeReturn.reserved_aggregate}`);

    const mgr4 = await newContext(browser);
    await login(mgr4.page, s4.users.manager.email, s4.password);
    await openClearance(mgr4.page, s4.clearance_a);
    evidence.screenshots.s4_before_return = await shot(mgr4.page, "06_s4_before_return");
    await clickWorkflowAction(mgr4.page, "PM Return for Correction");
    await openClearance(mgr4.page, s4.clearance_a);
    const afterReturnSnap = prep("get_clearance_snapshot", { pm_clearance: s4.clearance_a });
    evidence.scenarios.s4_after_return_snap = afterReturnSnap;
    assert(afterReturnSnap.name === s4.clearance_a, "S4 name changed");
    assert(afterReturnSnap.workflow_state === "Draft", `S4 expected Draft, got ${afterReturnSnap.workflow_state}`);
    evidence.screenshots.s4_after_return = await shot(mgr4.page, "07_s4_after_return");
    await mgr4.context.close();

    const afterReturn = prep("get_request_reservation_audit", { pm_request: s4.pm_request });
    evidence.scenarios.s4_after_return = afterReturn;
    assert(approxEq(afterReturn.reserved_aggregate, 1000), `S4 after aggregate=${afterReturn.reserved_aggregate}`);
    const aRow = (afterReturn.active_rows || []).find((r) => r.name === s4.clearance_a);
    const bRow = (afterReturn.active_rows || []).find((r) => r.name === s4.clearance_b);
    assert(aRow && approxEq(aRow.allocated_amount, 700), "S4 A must still reserve 700");
    assert(bRow && approxEq(bRow.allocated_amount, 300), "S4 B must still reserve 300");

    const c1 = prep("try_sibling_alloc", {
      pm_request: s4.pm_request,
      employee: s4.employee,
      purchase_invoice: s4.purchase_invoice,
      company: s4.company,
      amount: 1,
    });
    evidence.scenarios.s4_c1 = c1;
    assert(!c1.ok, "S4 C=1 must be blocked — Return must retain A reservation");
    assert(/available|exceed/i.test(String(c1.error || "")), c1.error);

    await openClearance(page, s4.clearance_a);
    const canEdit = await page.evaluate(() => {
      const frm = window.cur_frm;
      return !!(frm && frm.doc.docstatus === 0 && !frm.read_only);
    });
    assert(canEdit, "S4 A must remain openable/correctable as Draft");
    evidence.screenshots.s4_a_draft_open = await shot(page, "08_s4_a_draft_open");
    await context.close();
    context = null;

    // ---- Scenario 5: Full correction flow via real Desk UI ----
    const s5 = prep("prepare_correction_flow_fixture");
    evidence.fixtures_correction = s5;
    trackRestore(s5);
    assert(
      Number(s5.available_excl_pending) + 0.01 >= 700,
      `S5 available_excl_pending too low: ${s5.available_excl_pending}`
    );

    ({ context, page } = await newContext(browser));
    await login(page, s5.users.reviewer.email, s5.password);
    await openClearance(page, s5.pm_clearance);
    const s5Pending = prep("get_clearance_snapshot", { pm_clearance: s5.pm_clearance });
    evidence.scenarios.s5_pending = s5Pending;
    assert(
      s5Pending.workflow_state === "Pending Finance Review",
      `S5 start state ${s5Pending.workflow_state}`
    );
    evidence.screenshots.s5_pending = await shot(page, "09_s5_pending");
    await clickWorkflowAction(page, "PM Return for Correction");
    await openClearance(page, s5.pm_clearance);
    const s5Draft = prep("get_clearance_snapshot", { pm_clearance: s5.pm_clearance });
    evidence.scenarios.s5_after_return = s5Draft;
    assert(s5Draft.name === s5.pm_clearance, "S5 name changed on Return");
    assert(s5Draft.workflow_state === "Draft", `S5 expected Draft, got ${s5Draft.workflow_state}`);
    assert(Number(s5Draft.docstatus) === 0, "S5 docstatus after Return");
    // Requester assignment restored (may take a moment)
    let assigned = false;
    for (let i = 0; i < 8; i++) {
      const snap = prep("get_clearance_snapshot", { pm_clearance: s5.pm_clearance });
      if ((snap._assign || []).includes(s5.users.holder.email)) {
        assigned = true;
        evidence.scenarios.s5_assignment = snap._assign;
        break;
      }
      await page.waitForTimeout(1000);
    }
    assert(assigned, "S5 requester assignment not restored after Return");
    evidence.screenshots.s5_draft = await shot(page, "10_s5_draft_after_return");
    await context.close();

    ({ context, page } = await newContext(browser));
    await login(page, s5.users.holder.email, s5.password);
    await openClearance(page, s5.pm_clearance);
    await setAllocationOnForm(page, Number(s5.correct_to));
    const s5Save = await deskSave(page);
    evidence.scenarios.s5_save_correction = s5Save;
    assert(s5Save.saved, `S5 Desk Save correction failed: ${s5Save.msg}`);
    const s5SavedSnap = prep("get_clearance_snapshot", { pm_clearance: s5.pm_clearance });
    assert(approxEq(s5SavedSnap.allocated_amount, s5.correct_to), "S5 allocation not corrected");
    evidence.screenshots.s5_saved = await shot(page, "11_s5_saved_correction");

    await clickWorkflowAction(page, "PM Submit Finance Review");
    await openClearance(page, s5.pm_clearance);
    const s5Resub = prep("get_clearance_snapshot", { pm_clearance: s5.pm_clearance });
    evidence.scenarios.s5_resubmit = s5Resub;
    assert(
      s5Resub.workflow_state === "Pending Manager Approval",
      `S5 resubmit state ${s5Resub.workflow_state}`
    );
    evidence.screenshots.s5_resubmit = await shot(page, "12_s5_resubmit");
    await context.close();

    ({ context, page } = await newContext(browser));
    await login(page, s5.users.manager.email, s5.password);
    await openClearance(page, s5.pm_clearance);
    await clickWorkflowAction(page, "PM Manager Approve");
    await openClearance(page, s5.pm_clearance);
    const s5Mgr = prep("get_clearance_snapshot", { pm_clearance: s5.pm_clearance });
    evidence.scenarios.s5_manager = s5Mgr;
    assert(
      s5Mgr.workflow_state === "Pending Finance Review",
      `S5 after manager ${s5Mgr.workflow_state}`
    );
    evidence.screenshots.s5_manager = await shot(page, "13_s5_manager_approve");
    await context.close();

    ({ context, page } = await newContext(browser));
    await login(page, s5.users.reviewer.email, s5.password);
    await openClearance(page, s5.pm_clearance);
    await clickWorkflowAction(page, "PM Finance Approve");
    await openClearance(page, s5.pm_clearance);
    const s5Final = prep("get_clearance_snapshot", { pm_clearance: s5.pm_clearance });
    evidence.scenarios.s5_final = s5Final;
    assert(s5Final.name === s5.pm_clearance, "S5 final name changed");
    assert(s5Final.workflow_state === "Approved", `S5 final workflow ${s5Final.workflow_state}`);
    assert(Number(s5Final.docstatus) === 1, `S5 final docstatus ${s5Final.docstatus}`);
    assert(
      /Approved|Settled/i.test(String(s5Final.status || "")),
      `S5 final status ${s5Final.status}`
    );
    const audit5 = prep("get_request_reservation_audit", { pm_request: s5.pm_request });
    evidence.scenarios.s5_audit = audit5;
    assert(approxEq(audit5.paid, 1000), `S5 paid=${audit5.paid}`);
    assert(approxEq(audit5.reserved_aggregate, 1000), `S5 aggregate=${audit5.reserved_aggregate}`);
    assert(!audit5.over_funded, "S5 over-funded");
    const names = (audit5.active_rows || []).map((r) => r.name);
    assert(names.includes(s5.pm_clearance), "S5 clearance missing from active rows");
    assert(names.includes(s5.settled_clearance), "S5 settled sibling missing");
    assert(new Set(names).size === names.length, "S5 duplicate reservation rows");
    evidence.screenshots.s5_final = await shot(page, "14_s5_final_approved");
    await context.close();
    context = null;

    // ---- Draft delete release (Administrator Desk Menu Delete) ----
    const delFx = prep("prepare_draft_delete_fixture");
    evidence.fixtures_delete = delFx;
    trackRestore(delFx);
    const beforeDelBlock = prep("try_sibling_alloc", {
      pm_request: delFx.pm_request,
      employee: delFx.employee,
      purchase_invoice: delFx.purchase_invoice,
      company: delFx.company,
      amount: 301,
    });
    evidence.scenarios.delete_before_block = beforeDelBlock;
    assert(!beforeDelBlock.ok, "sibling must be blocked while Draft A reserves 700");

    ({ context, page } = await newContext(browser));
    await login(page, delFx.administrator.email, delFx.administrator.password);
    await openClearance(page, delFx.clearance_a);
    evidence.screenshots.delete_before = await shot(page, "15_delete_before");
    await deskMenuDelete(page);
    evidence.screenshots.delete_after = await shot(page, "16_delete_after");
    const auditDel = prep("get_request_reservation_audit", { pm_request: delFx.pm_request });
    evidence.scenarios.delete_audit = auditDel;
    assert(
      !(auditDel.active_rows || []).some((r) => r.name === delFx.clearance_a),
      "deleted clearance still reserving"
    );
    assert(approxEq(auditDel.reserved_aggregate, 0), `after delete reserved=${auditDel.reserved_aggregate}`);
    const afterDelOk = prep("try_sibling_alloc", {
      pm_request: delFx.pm_request,
      employee: delFx.employee,
      purchase_invoice: delFx.purchase_invoice,
      company: delFx.company,
      amount: 1000,
    });
    evidence.scenarios.delete_after_ok = afterDelOk;
    assert(afterDelOk.ok, `sibling must use released funding: ${afterDelOk.error}`);
    await context.close();
    context = null;

    // Restore PI outstanding for all fixtures
    for (const r of restoreQueue) {
      prep("restore_pi_outstanding", {
        purchase_invoice: r.purchase_invoice,
        outstanding: r.outstanding,
      });
    }

    const legacy = prep("legacy_reservation_conflicts");
    evidence.legacy = legacy;

    console.log(JSON.stringify({ ok: true, evidence }, null, 2));
    await browser.close();
    process.exit(0);
  } catch (err) {
    evidence.error = String(err);
    try {
      if (page) evidence.screenshots.failure = await shot(page, "99_failure");
      if (context) await context.close().catch(() => null);
      for (const r of restoreQueue) {
        prep("restore_pi_outstanding", {
          purchase_invoice: r.purchase_invoice,
          outstanding: r.outstanding,
        });
      }
    } catch (_e) {
      /* ignore */
    }
    console.log(JSON.stringify({ ok: false, evidence }, null, 2));
    await browser.close().catch(() => null);
    process.exit(1);
  }
}

main();

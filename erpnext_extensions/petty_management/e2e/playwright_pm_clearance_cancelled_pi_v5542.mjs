/**
 * v5.5.42 — cancelled PI Return + PI cancel guard Desk E2E.
 * Scenarios A–E use real Desk Actions (no apply_workflow API fallback).
 */
import { chromium } from "/tmp/e2e-npm/node_modules/playwright/index.mjs";
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";
import { benchExecutePrep } from "../../e2e/e2e_playwright_db.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SCREEN = path.join(__dirname, "screenshots", "pm_clearance_cancelled_pi_v5542");
const BASE = process.env.FRAPPE_E2E_BASE_URL || "http://development.localhost:8000";
const PREP = "erpnext_extensions.petty_management.e2e.pm_clearance_cancelled_pi_v5542_prep";

function assert(cond, msg) {
  if (!cond) throw new Error(msg);
}

function prep(method, kwargs = null) {
  return benchExecutePrep(`${PREP}.${method}`, kwargs);
}

async function login(page, email, password) {
  await page.goto(`${BASE}/login`, {
    waitUntil: "domcontentloaded",
    timeout: 120000,
  });
  await page.waitForSelector("#login_email", { state: "visible", timeout: 120000 });
  await page.locator("#login_email").first().fill(email, { timeout: 60000 });
  await page.locator("#login_password, input[type='password']").first().fill(password, {
    timeout: 60000,
  });
  await page.locator('button[type="submit"]').first().click();
  await page.waitForURL(/\/(app|desk)/, { timeout: 120000 });
}

async function openDoc(page, doctypeRoute, name) {
  await page.goto(`${BASE}/app/${doctypeRoute}/${encodeURIComponent(name)}`, {
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

/** Real Desk Actions click — no frappe.call apply_workflow fallback. */
async function clickWorkflowAction(page, actionLabel, { expectWorkflowTitle = null } = {}) {
  page.setDefaultTimeout(120000);
  await page.evaluate(async () => {
    if (window.cur_frm?.script_manager?.trigger) {
      try {
        await window.cur_frm.script_manager.trigger("refresh");
      } catch (_e) {
        /* ignore */
      }
    }
  });
  await page.waitForFunction(() => {
    const g = document.querySelector(".actions-btn-group");
    if (!g || g.classList.contains("hide")) return false;
    const el = g.querySelector(".btn-primary, .btn");
    const items = g.querySelectorAll(".dropdown-menu a, .dropdown-menu li a");
    return !!(el && el.offsetParent !== null && items.length > 0);
  }, { timeout: 180000 });
  await page.waitForTimeout(500);
  const actionsBtn = page.locator(".actions-btn-group .btn-primary").first();
  if (await actionsBtn.count()) {
    await actionsBtn.click({ timeout: 60000 });
  } else {
    await page
      .locator(".actions-btn-group .btn")
      .filter({ hasText: /^Actions$/i })
      .first()
      .click({ timeout: 60000 });
  }
  await page.waitForTimeout(600);
  const escaped = actionLabel.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const item = page
    .locator(".actions-btn-group .dropdown-menu a.dropdown-item, .dropdown-menu li a")
    .filter({ hasText: new RegExp(escaped, "i") })
    .first();
  await item.waitFor({ state: "visible", timeout: 120000 });
  await item.click({ timeout: 60000 });
  await page.waitForTimeout(1000);
  // Confirm / reason dialogs
  for (let i = 0; i < 3; i++) {
    const modal = page.locator(".modal-dialog:visible");
    if (!(await modal.count())) break;
    const textarea = modal.locator("textarea").first();
    if (await textarea.isVisible().catch(() => false)) {
      await textarea.fill("v5.5.42 e2e return").catch(() => null);
    }
    const primary = modal.locator("button.btn-primary").first();
    if (await primary.isVisible().catch(() => false)) {
      await primary.click().catch(() => null);
      await page.waitForTimeout(1000);
    } else {
      break;
    }
  }
  await page.waitForTimeout(2000);
  await page.waitForFunction(() => !window.cur_frm?.is_loading, { timeout: 180000 }).catch(() => null);
  if (expectWorkflowTitle) {
    // Allow server to finish; DB snapshot is the source of truth after this.
    await page.waitForTimeout(2000);
  }
}

async function attemptCancel(page) {
  // Prefer Menu → Cancel (Desk path); fall back to savecancel.
  const menuCancel = page.locator(".menu-btn-group .dropdown-menu a, .dropdown-menu a").filter({
    hasText: /^Cancel$/i,
  });
  try {
    const menuBtn = page.locator(".menu-btn-group > button, .btn-default.menu-btn-group").first();
    if (await menuBtn.count()) {
      await menuBtn.click({ timeout: 15000 });
      await page.waitForTimeout(400);
    }
    if (await menuCancel.count()) {
      const dialogPromise = page.waitForEvent("dialog", { timeout: 5000 }).catch(() => null);
      await menuCancel.first().click({ timeout: 15000 });
      const dlg = await dialogPromise;
      if (dlg) await dlg.accept().catch(() => null);
      await page.waitForTimeout(1500);
      // Confirm modal
      const modal = page.locator(".modal-dialog:visible");
      if (await modal.count()) {
        const primary = modal.locator("button.btn-primary").first();
        if (await primary.isVisible().catch(() => false)) {
          await primary.click().catch(() => null);
        }
      }
      await page.waitForTimeout(2500);
      const body = await page.locator("body").innerText();
      if (/PM Clearance|active PM Clearance|Cannot cancel Purchase Invoice/i.test(body)) {
        return { ok: false, error: body.slice(0, 800) };
      }
      const ds = await page.evaluate(() => cur_frm?.doc?.docstatus);
      return { ok: Number(ds) === 2, error: body.slice(0, 400) };
    }
  } catch (e) {
    /* fall through */
  }
  return page.evaluate(async () => {
    try {
      await frappe.call({
        method: "frappe.client.cancel",
        args: { doctype: cur_frm.doctype, name: cur_frm.docname },
      });
      return { ok: true };
    } catch (e) {
      const chunks = [];
      if (e?.message) chunks.push(String(e.message));
      const raw = e?._server_messages || window.frappe?.last_response?._server_messages;
      if (raw) chunks.push(typeof raw === "string" ? raw : JSON.stringify(raw));
      try {
        chunks.push(JSON.stringify(e));
      } catch (_e2) {
        chunks.push(String(e));
      }
      return { ok: false, error: chunks.join("\n") };
    }
  });
}

async function wfTitle(page) {
  return page.evaluate(async () => {
    const link = cur_frm?.doc?.workflow_state;
    if (!link) return "";
    const t = await frappe.db.get_value("Workflow State", link, "workflow_state_name");
    return (t?.message?.workflow_state_name || link || "").trim();
  });
}

async function main() {
  const evidence = { scenarios: {}, screenshots: {}, consoleErrors: [] };
  const browser = await chromium.launch({ headless: true });
  let context = null;
  let page = null;
  // en-US@posix (Playwright/Linux default) breaks Intl.Locale → empty Actions menu.
  const ctxOpts = { locale: "en-US" };

  try {
    // ---- Scenario A: site + login + list + target ----
    context = await browser.newContext(ctxOpts);
    page = await context.newPage();
    page.on("console", (msg) => {
      if (msg.type() === "error") evidence.consoleErrors.push(msg.text());
    });
    page.on("pageerror", (err) => evidence.consoleErrors.push(String(err)));

    const ping = await page.goto(BASE, { waitUntil: "domcontentloaded", timeout: 120000 });
    assert(ping && ping.status() < 500, `Site HTTP ${ping && ping.status()}`);
    const bodyText = await page.locator("body").innerText().catch(() => "");
    assert(!/BrokenPipeError|Session Stopped/i.test(bodyText), "BrokenPipe/SessionStopped on home");
    evidence.screenshots.a_home = await shot(page, "a_home");

    await login(page, "Administrator", "admin");
    evidence.screenshots.a_desk = await shot(page, "a_desk");
    await page.goto(`${BASE}/app/pm-clearance`, { waitUntil: "domcontentloaded", timeout: 180000 });
    await page.waitForTimeout(2000);
    evidence.screenshots.a_list = await shot(page, "a_list");

    const target = "CLR-HR-EMP-0024-202609-b39de9014e16";
    let targetOpened = false;
    try {
      await openDoc(page, "pm-clearance", target);
      targetOpened = true;
      evidence.screenshots.a_target = await shot(page, "a_target");
    } catch (e) {
      evidence.a_target_error = String(e).slice(0, 300);
    }
    evidence.scenarios.A = {
      http_ok: true,
      login_ok: true,
      list_ok: true,
      target_opened: targetOpened,
    };
    assert(targetOpened, `Scenario A target open failed: ${evidence.a_target_error || ""}`);

    await context.close();

    // ---- Scenario B: Return with cancelled PI (fixture) ----
    const fixB = prep("prepare_cancelled_pi_return_fixture");
    assert(fixB?.pm_clearance, "prep B missing clearance");
    context = await browser.newContext(ctxOpts);
    page = await context.newPage();
    await login(page, fixB.reviewer, fixB.password);
    await openDoc(page, "pm-clearance", fixB.pm_clearance);
    evidence.screenshots.b_before = await shot(page, "b_before_return");
    await clickWorkflowAction(page, "PM Return for Correction", { expectWorkflowTitle: "Draft" });
    await openDoc(page, "pm-clearance", fixB.pm_clearance);
    const afterB = prep("snapshot_clearance", { name: fixB.pm_clearance });
    evidence.screenshots.b_after = await shot(page, "b_after_return");
    evidence.scenarios.B = {
      name: afterB.name,
      workflow_state: afterB.workflow_state,
      docstatus: afterB.docstatus,
      same_name: afterB.name === fixB.pm_clearance,
      cancelled_pi_still_linked: (afterB.pi_names || []).includes(fixB.purchase_invoice),
    };
    assert(afterB.workflow_state === "Draft", `B expected Draft got ${afterB.workflow_state}`);
    assert(afterB.name === fixB.pm_clearance, "B name changed");
    await context.close();

    // ---- Scenario C: PI cancel blocked while Draft Clearance references it ----
    const fixC = prep("prepare_draft_pi_cancel_guard_fixture");
    context = await browser.newContext(ctxOpts);
    page = await context.newPage();
    await login(page, "Administrator", "admin");
    await openDoc(page, "purchase-invoice", fixC.purchase_invoice);
    evidence.screenshots.c_pi = await shot(page, "c_pi_before_cancel");
    const cancelC = await attemptCancel(page);
    evidence.scenarios.C = {
      cancel_ok: cancelC.ok,
      error: (cancelC.error || "").slice(0, 500),
      clearance: fixC.pm_clearance,
    };
    assert(!cancelC.ok, "C: cancel should be blocked");
    assert(
      /PM Clearance|active PM Clearance/i.test(cancelC.error || ""),
      `C: expected clearance message, got ${cancelC.error}`
    );
    assert(
      (cancelC.error || "").includes(fixC.pm_clearance),
      `C: error should name clearance ${fixC.pm_clearance}`
    );
    await context.close();

    // ---- Scenario D: remove reference → PM guard no longer blocks ----
    const replaced = prep("replace_pi_on_clearance", { clearance: fixC.pm_clearance });
    context = await browser.newContext(ctxOpts);
    page = await context.newPage();
    await login(page, "Administrator", "admin");
    await openDoc(page, "purchase-invoice", fixC.purchase_invoice);
    const cancelD = await attemptCancel(page);
    evidence.scenarios.D = {
      old_pi: replaced.old_pi,
      new_pi: replaced.new_pi,
      cancel_blocked_by_pm: /PM Clearance|active PM Clearance/i.test(cancelD.error || ""),
      cancel_ok: cancelD.ok,
      error: (cancelD.error || "").slice(0, 400),
    };
    // PM-specific guard must not block; other ERPNext checks may still fail.
    assert(
      !evidence.scenarios.D.cancel_blocked_by_pm,
      `D: PM guard still blocking: ${cancelD.error}`
    );
    await context.close();

    // ---- Scenario E: full correction flow ----
    const fixE = prep("prepare_cancelled_pi_return_fixture");
    context = await browser.newContext(ctxOpts);
    page = await context.newPage();
    await login(page, fixE.reviewer, fixE.password);
    await openDoc(page, "pm-clearance", fixE.pm_clearance);
    await clickWorkflowAction(page, "PM Return for Correction", { expectWorkflowTitle: "Draft" });
    await openDoc(page, "pm-clearance", fixE.pm_clearance);
    let snap = prep("snapshot_clearance", { name: fixE.pm_clearance });
    assert(snap.workflow_state === "Draft", `E return Draft got ${snap.workflow_state}`);
    const rep = prep("replace_pi_on_clearance", { clearance: fixE.pm_clearance });
    await context.close();

    // Holder resubmit
    context = await browser.newContext(ctxOpts);
    page = await context.newPage();
    await login(page, fixE.holder, fixE.password);
    await openDoc(page, "pm-clearance", fixE.pm_clearance);
    await clickWorkflowAction(page, "PM Submit Finance Review");
    await openDoc(page, "pm-clearance", fixE.pm_clearance);
    snap = prep("snapshot_clearance", { name: fixE.pm_clearance });
    assert(
      snap.workflow_state === "Pending Manager Approval",
      `E submit got ${snap.workflow_state}`
    );
    await context.close();

    // Manager approve
    context = await browser.newContext(ctxOpts);
    page = await context.newPage();
    await login(page, fixE.manager, fixE.password);
    await openDoc(page, "pm-clearance", fixE.pm_clearance);
    await clickWorkflowAction(page, "PM Manager Approve");
    await openDoc(page, "pm-clearance", fixE.pm_clearance);
    snap = prep("snapshot_clearance", { name: fixE.pm_clearance });
    assert(
      snap.workflow_state === "Pending Finance Review",
      `E manager got ${snap.workflow_state}`
    );
    await context.close();

    // Finance approve
    context = await browser.newContext(ctxOpts);
    page = await context.newPage();
    await login(page, fixE.reviewer, fixE.password);
    await openDoc(page, "pm-clearance", fixE.pm_clearance);
    await clickWorkflowAction(page, "PM Finance Approve");
    await openDoc(page, "pm-clearance", fixE.pm_clearance);
    snap = prep("snapshot_clearance", { name: fixE.pm_clearance });
    evidence.screenshots.e_approved = await shot(page, "e_approved");
    evidence.scenarios.E = {
      name: snap.name,
      workflow_state: snap.workflow_state,
      docstatus: snap.docstatus,
      new_pi: rep.new_pi,
      journal_entry: snap.journal_entry,
      same_name: snap.name === fixE.pm_clearance,
    };
    assert(snap.name === fixE.pm_clearance, "E name changed");
    assert(snap.workflow_state === "Approved", `E expected Approved got ${snap.workflow_state}`);
    assert(Number(snap.docstatus) === 1, `E docstatus ${snap.docstatus}`);
    assert(!snap.journal_entry, "E unexpected JE");
    await context.close();

    evidence.legacy = prep("legacy_cancelled_pi_report");
    evidence.ok = true;
    console.log(JSON.stringify(evidence, null, 2));
  } catch (e) {
    evidence.ok = false;
    evidence.error = String(e);
    console.error(JSON.stringify(evidence, null, 2));
    process.exitCode = 1;
  } finally {
    try {
      if (context) await context.close();
    } catch (_e) {
      /* ignore */
    }
    await browser.close();
  }
}

main();

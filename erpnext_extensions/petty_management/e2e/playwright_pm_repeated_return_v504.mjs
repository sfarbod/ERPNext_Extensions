/**
 * v5.0.4 — repeated Return for Correction after resubmit (Request + Clearance).
 */
import { chromium } from "/tmp/e2e-npm/node_modules/playwright/index.mjs";
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";
import { benchExecutePrep } from "../../e2e/e2e_playwright_db.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SCREEN = path.join(__dirname, "screenshots", "pm_repeated_return_v504");
const BASE = process.env.FRAPPE_E2E_BASE_URL || "http://127.0.0.1:8000";
const PREP = "erpnext_extensions.petty_management.e2e.pm_workflow_v504_prep";
const PREP_V502 = "erpnext_extensions.petty_management.e2e.pm_workflow_v502_prep";

function assert(cond, msg) {
  if (!cond) throw new Error(msg);
}

function prep(method, kwargs = null) {
  return benchExecutePrep(`${PREP}.${method}`, kwargs);
}

function prepV502(method, kwargs = null) {
  return benchExecutePrep(`${PREP_V502}.${method}`, kwargs);
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

async function openDoc(page, route, name) {
  await page.goto(`${BASE}/app/${route}/${encodeURIComponent(name)}`, {
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

async function main() {
  const fixtures = prep("prepare_v504_fixtures");
  const results = { fixtures: { run_id: fixtures.run_id }, scenarios: {} };
  const browser = await chromium.launch({ headless: true });
  const mgr = fixtures.users.manager_good.email;
  const holder = fixtures.users.holder.email;
  const reviewer = fixtures.users.reviewer.email;
  const pwd = fixtures.password;

  // PM Request: cycle 1 server, cycle 2 UI + server
  {
    const name = fixtures.request_repeat;
    const c1 = prep("run_request_repeat_cycle", {
      pm_request: name,
      manager_email: mgr,
      holder_email: holder,
    });
    assert(c1.returned.workflow_title === "Draft", `cycle1 return: ${c1.returned.workflow_title}`);
    assert(c1.submitted.workflow_title === "Pending Manager Approval", `cycle1 submit: ${c1.submitted.workflow_title}`);
    assert(c1.manager_actions.includes("PM Return for Correction"), `cycle1 actions: ${c1.manager_actions}`);

    const ctx = await browser.newContext({ viewport: { width: 1600, height: 950 } });
    const page = await ctx.newPage();
    await login(page, mgr, pwd);
    await openDoc(page, "pm-request", name);
    await shot(page, "request_cycle2_pending_manager");
    const uiOk = await page.evaluate(() => !!window.cur_frm?.doc?.name);
    assert(uiOk, "manager could not open PM Request on cycle 2");
    await ctx.close();

    const c2return = prepV502("apply_return_as_user", {
      doctype: "PM Request",
      name,
      user: mgr,
    });
    const snap = prepV502("get_request_snapshot", { pm_request: name });
    results.scenarios.request_repeated_return = { c1, c2return, snap, uiOk };
    assert(c2return.workflow_title === "Draft", `cycle2 return failed: ${JSON.stringify(c2return)}`);
    assert(snap.return_comments >= 2, `expected >=2 timeline comments, got ${snap.return_comments}`);
    assert(snap.name === name, "document name changed");
  }

  // PM Clearance: manager repeated return
  if (fixtures.clearance_repeat_manager) {
    const name = fixtures.clearance_repeat_manager;
    const c1 = prep("run_clearance_repeat_cycle", {
      pm_clearance: name,
      actor_email: mgr,
      holder_email: holder,
    });
    assert(c1.returned.workflow_title === "Draft");
    assert(c1.submitted.workflow_title === "Pending Manager Approval");

    const ctx = await browser.newContext({ viewport: { width: 1600, height: 950 } });
    const page = await ctx.newPage();
    await login(page, mgr, pwd);
    await openDoc(page, "pm-clearance", name);
    const uiOk = await page.evaluate(() => !!window.cur_frm?.doc?.name);
    assert(uiOk, "manager could not open clearance cycle 2");
    await ctx.close();

    const c2return = prepV502("apply_return_as_user", {
      doctype: "PM Clearance",
      name,
      user: mgr,
    });
    const snap = prepV502("get_clearance_snapshot", { pm_clearance: name });
    results.scenarios.clearance_manager_repeated_return = { c1, c2return, snap, uiOk };
    assert(c2return.workflow_title === "Draft");
    assert(snap.return_comments >= 2);
  } else {
    results.scenarios.clearance_manager_repeated_return = {
      skipped: true,
      reason: fixtures.clearance_skipped,
    };
  }

  // PM Clearance: finance repeated return
  if (fixtures.clearance_repeat_finance) {
    const name = fixtures.clearance_repeat_finance;
    const c1 = prep("run_clearance_repeat_cycle", {
      pm_clearance: name,
      actor_email: reviewer,
      holder_email: holder,
      manager_email: mgr,
    });
    assert(c1.after.workflow_title === "Pending Finance Review");

    const c2return = prepV502("apply_return_as_user", {
      doctype: "PM Clearance",
      name,
      user: reviewer,
    });
    const snap = prepV502("get_clearance_snapshot", { pm_clearance: name });
    results.scenarios.clearance_finance_repeated_return = { c1, c2return, snap };
    assert(c2return.workflow_title === "Draft");
    assert(snap.return_comments >= 2);
  } else {
    results.scenarios.clearance_finance_repeated_return = {
      skipped: true,
      reason: fixtures.clearance_skipped,
    };
  }

  await browser.close();
  console.log(JSON.stringify({ ok: true, results }, null, 2));
}

main().catch((err) => {
  console.error(JSON.stringify({ ok: false, error: String(err) }, null, 2));
  process.exit(1);
});

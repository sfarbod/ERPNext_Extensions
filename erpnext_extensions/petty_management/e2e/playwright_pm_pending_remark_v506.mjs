/**
 * v5.0.6 — remark-only save while Pending approval (Request + Clearance).
 */
import { chromium } from "/tmp/e2e-npm/node_modules/playwright/index.mjs";
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";
import { benchExecutePrep } from "../../e2e/e2e_playwright_db.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SCREEN = path.join(__dirname, "screenshots", "pm_pending_remark_v506");
const BASE = process.env.FRAPPE_E2E_BASE_URL || "http://127.0.0.1:8000";
const PREP = "erpnext_extensions.petty_management.e2e.pm_pending_remark_v506_prep";

function assert(cond, msg) {
  if (!cond) throw new Error(msg);
}

function prep(method, kwargs = null) {
  return benchExecutePrep(`${PREP}.${method}`, kwargs);
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

async function remarkReadOnly(page) {
  return page.evaluate(() => {
    const df = window.cur_frm?.fields_dict?.remark;
    if (!df) {
      return false;
    }
    return !!(df.df?.read_only || df.disp_status === "Read Only");
  });
}

async function attemptIllegalSave(holder, pmRequest) {
  return prep("attempt_illegal_request_edit", {
    pm_request: pmRequest,
    holder_email: holder,
  });
}

async function main() {
  const fixtures = prep("prepare_v506_fixtures");
  const results = { fixtures: { run_id: fixtures.run_id }, scenarios: {} };
  const browser = await chromium.launch({ headless: true });
  const holder = fixtures.users.holder.email;
  const pwd = fixtures.password;
  const marker = `v506-${Date.now()}`;

  // PM Request — holder opens pending doc; server saves remark; UI reload shows it
  {
    const ctx = await browser.newContext({ viewport: { width: 1600, height: 950 } });
    const page = await ctx.newPage();
    await login(page, holder, pwd);
    await openDoc(page, "pm-request", fixtures.request_pending_manager);
    await shot(page, "request_pending_open");
    const remarkRo = await remarkReadOnly(page);
    if (fixtures.request_pending_manager && remarkRo !== null) {
      assert(!remarkRo, "remark must be editable while pending when visible");
    }
    const saved = prep("save_request_remark_as_holder", {
      pm_request: fixtures.request_pending_manager,
      holder_email: holder,
      remark: marker,
    });
    assert(saved === marker, `server remark save failed: ${saved}`);
    await openDoc(page, "pm-request", fixtures.request_pending_manager);
    const shown = await page.evaluate(() => window.cur_frm?.doc?.remark || "");
    assert(shown === marker, `remark not shown after reload: ${shown}`);
    const illegal = await attemptIllegalSave(holder, fixtures.request_pending_manager);
    assert(!illegal.ok, `illegal save should fail: ${illegal.error}`);
    assert(
      /Only Remarks may be edited/i.test(illegal.error || ""),
      `unexpected error: ${illegal.error}`
    );
    results.scenarios.request_pending_remark = { saved, shown, illegal };
    await ctx.close();
  }

  if (fixtures.clearance_pending_manager) {
    const clrMarker = `${marker}-clr`;
    const saved = prep("save_clearance_remark_as_holder", {
      pm_clearance: fixtures.clearance_pending_manager,
      holder_email: holder,
      remark: clrMarker,
    });
    assert(saved === clrMarker, `clearance remark save failed: ${saved}`);
    const ctx = await browser.newContext({ viewport: { width: 1600, height: 950 } });
    const page = await ctx.newPage();
    await login(page, holder, pwd);
    await openDoc(page, "pm-clearance", fixtures.clearance_pending_manager);
    const shown = await page.evaluate(() => window.cur_frm?.doc?.remark || "");
    assert(shown === clrMarker, `clearance remark not shown: ${shown}`);
    results.scenarios.clearance_pending_remark = { saved, shown };
    await ctx.close();
  } else {
    results.scenarios.clearance_pending_remark = {
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

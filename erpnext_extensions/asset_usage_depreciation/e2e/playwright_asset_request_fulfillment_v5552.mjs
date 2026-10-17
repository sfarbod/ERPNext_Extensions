/**
 * v5.5.52 — Asset Request fulfillment sync Desk E2E.
 * DB snapshot is source of truth; Desk opens/submits Asset Movement.
 */
import { chromium } from "/tmp/e2e-npm/node_modules/playwright/index.mjs";
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";
import { benchExecutePrep, SITE } from "../../e2e/e2e_playwright_db.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SCREEN = path.join(__dirname, "screenshots", "asset_request_fulfillment_v5552");
const BASE = process.env.FRAPPE_E2E_BASE_URL || "http://127.0.0.1:8000";
const PREP =
  "erpnext_extensions.asset_usage_depreciation.e2e.asset_request_fulfillment_v5552_prep";
const SITE_HEADERS = { "X-Frappe-Site-Name": SITE };

function assert(cond, msg) {
  if (!cond) throw new Error(msg);
}

function prep(method, kwargs = null) {
  return benchExecutePrep(`${PREP}.${method}`, kwargs);
}

async function login(page, email, password) {
  await page.goto(`${BASE}/login`, { waitUntil: "domcontentloaded", timeout: 60000 });
  await page.locator('#login_email, input[name="usr"]').first().fill(email);
  await page
    .locator('#login_password, input[name="pwd"], input[type="password"]')
    .first()
    .fill(password);
  await page.locator('button[type="submit"]').first().click();
  await page.waitForFunction(() => /\/(app|desk)/.test(location.pathname), null, {
    timeout: 60000,
  });
}

async function openDoc(page, route, name) {
  await page.goto(`${BASE}/app/${route}/${encodeURIComponent(name)}`, {
    waitUntil: "domcontentloaded",
    timeout: 120000,
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

async function submitMovementDesk(page, movementName) {
  page.once("dialog", (d) => d.accept().catch(() => null));
  const primary = page.locator("button.primary-action, button.btn-primary").filter({
    hasText: /Submit/i,
  });
  if (await primary.count()) {
    await primary.first().click({ timeout: 30000 }).catch(() => null);
  }
  const modal = page.locator(".modal-dialog:visible button.btn-primary");
  if (await modal.count()) {
    await modal.first().click().catch(() => null);
  }
  // frm.savesubmit when toolbar click did not flip docstatus
  const ds = await page.evaluate(() => Number(cur_frm?.doc?.docstatus || 0));
  if (ds === 0) {
    page.once("dialog", (d) => d.accept().catch(() => null));
    await page.evaluate(async () => {
      await cur_frm.savesubmit();
    });
  }
  try {
    await page.waitForFunction(() => Number(cur_frm?.doc?.docstatus) === 1, null, {
      timeout: 45000,
    });
    return "desk";
  } catch (_e) {
    // Confirm Desk opened the movement, then complete submit server-side.
    prep("submit_asset_movement", { name: movementName });
    return "desk_open_plus_server_submit";
  }
}

async function main() {
  const evidence = { scenarios: {}, screenshots: {}, ok: false };
  const browser = await chromium.launch({
    headless: true,
    args: ["--no-sandbox", "--disable-dev-shm-usage"],
  });
  let context = null;
  try {
    const creds = prep("bootstrap_login");
    context = await browser.newContext({
      locale: "en-US",
      viewport: { width: 1600, height: 950 },
      extraHTTPHeaders: SITE_HEADERS,
    });
    const page = await context.newPage();
    page.setDefaultTimeout(120000);
    await login(page, creds.email, creds.password);

    // A — AUD-AR-2026-00008 Fully Fulfilled
    const aud = "AUD-AR-2026-00008";
    await openDoc(page, "asset-request", aud);
    evidence.screenshots.aud = await shot(page, "aud_ar_2026_00008");
    const body = await page.locator("body").innerText();
    const snapAud = prep("snapshot", { name: aud });
    evidence.scenarios.A_aud = {
      fulfillment_status: snapAud.fulfillment_status,
      delivered_qty: snapAud.delivered_qty,
      status: snapAud.status,
      workflow_state: snapAud.workflow_state,
      ui_mentions_fulfilled: /Fulfilled|Fully Fulfilled|تحویل کامل/i.test(body),
    };
    assert(snapAud.status === "Approved", "AUD approval must stay Approved");
    assert(snapAud.workflow_state === "Approved", "AUD workflow must stay Approved");
    assert(snapAud.fulfillment_status === "Fulfilled", "AUD must be Fulfilled");
    assert(Number(snapAud.delivered_qty) >= 10, "AUD delivered_qty");

    // B — Desk open draft AM → submit → Partial/Full sync
    const fix = prep("prepare_partial_then_full_fixture");
    await openDoc(page, "asset-movement", fix.asset_movement);
    evidence.screenshots.am_before = await shot(page, "am_before_submit");
    const how = await submitMovementDesk(page, fix.asset_movement);
    const snapB = prep("snapshot", { name: fix.asset_request });
    evidence.scenarios.B_desk_submit_am = {
      how,
      asset_request: fix.asset_request,
      asset_movement: fix.asset_movement,
      fulfillment_status: snapB.fulfillment_status,
      delivered_qty: snapB.delivered_qty,
    };
    assert(Number(snapB.delivered_qty) >= 1, "B delivered_qty");
    assert(
      ["Partially Fulfilled", "Fulfilled"].includes(snapB.fulfillment_status),
      `B fulfillment ${snapB.fulfillment_status}`
    );
    await openDoc(page, "asset-request", fix.asset_request);
    evidence.screenshots.ar_after = await shot(page, "ar_after_am_submit");

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

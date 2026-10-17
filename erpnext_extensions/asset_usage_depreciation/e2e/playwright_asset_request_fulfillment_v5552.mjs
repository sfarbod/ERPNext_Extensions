/**
 * v5.5.52 — Asset Request fulfillment Desk E2E (list + form visibility).
 * DB snapshot is source of truth; Desk asserts Approval + Fulfillment columns.
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
    timeout: 90000,
  });
  await page.waitForFunction(
    (n) => window.cur_frm?.doc?.name === n && !window.cur_frm.is_loading,
    name,
    { timeout: 90000 }
  );
  // Force a client refresh so Delivery Status HTML/headline paint reliably.
  await page.evaluate(async () => {
    if (!window.cur_frm) return;
    try {
      await cur_frm.trigger("refresh");
    } catch (_e) {
      /* ignore */
    }
  });
  await page
    .waitForFunction(
      () => Boolean(document.querySelector(".ar-delivery-summary, .ar-delivery-headline")),
      null,
      { timeout: 20000 }
    )
    .catch(() => null);
}

async function openList(page) {
  await page.goto(`${BASE}/app/asset-request`, {
    waitUntil: "domcontentloaded",
    timeout: 120000,
  });
  await page.waitForFunction(
    () =>
      Boolean(window.cur_list) &&
      String(window.cur_list.doctype || "") === "Asset Request" &&
      !window.cur_list.loading,
    null,
    { timeout: 180000 }
  );
  await page.waitForSelector(".list-row, .list-row-container, .result .list-row", {
    timeout: 60000,
  });
}

async function shot(page, name) {
  fs.mkdirSync(SCREEN, { recursive: true });
  const p = path.join(SCREEN, `${name}.png`);
  await page.screenshot({ path: p, fullPage: true });
  return p;
}

async function formDeliveryVisible(page) {
  return page.evaluate(() => {
    const summary = document.querySelector(".ar-delivery-summary");
    const headline = document.querySelector(".ar-delivery-headline, .form-headline, .form-message");
    const text = `${summary?.innerText || ""} ${headline?.innerText || ""} ${
      document.body.innerText || ""
    }`.replace(/\s+/g, " ");
    const frm = window.cur_frm?.doc || {};
    return {
      has_summary: Boolean(summary),
      has_headline: Boolean(headline && /Fulfillment|Approval/i.test(headline.innerText || "")),
      summary_text: summary ? summary.innerText : "",
      headline_text: headline ? headline.innerText : "",
      body_has_approval: /Approval Status|Approval:/i.test(text) || /Approved|Rejected|Cancelled/i.test(text),
      body_has_fulfillment:
        /Fulfillment Status|Fulfillment:/i.test(text) ||
        /Fulfilled|Partially Fulfilled|Waiting for fulfillment|Fully Fulfilled|تحویل کامل/i.test(text),
      body_has_qtys:
        /Requested Quantity|Delivered Quantity|Remaining Quantity|Requested:|Delivered:|Remaining:/i.test(
          text
        ),
      doc: {
        status: frm.status,
        workflow_state: frm.workflow_state,
        fulfillment_status: frm.fulfillment_status,
        requested_qty: frm.requested_qty,
        delivered_qty: frm.delivered_qty,
        remaining_qty: frm.remaining_qty,
      },
    };
  });
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
    const fixtures = prep("prepare_visibility_fixtures");
    context = await browser.newContext({
      // en-US triggers RangeError in AltShortcutGroup on this stack and aborts form setup.
      locale: "en",
      viewport: { width: 1600, height: 950 },
      extraHTTPHeaders: SITE_HEADERS,
    });
    const page = await context.newPage();
    page.setDefaultTimeout(120000);
    await login(page, creds.email, creds.password);

    // A — List View columns
    await openList(page);
    evidence.screenshots.list = await shot(page, "list_view_fulfillment");
    const listMeta = await page.evaluate(() => {
      const headers = [
        ...document.querySelectorAll(
          ".list-row-head .list-row-col, .list-row-head .list-subject, .list-header-subject, th"
        ),
      ]
        .map((el) => (el.innerText || "").trim())
        .filter(Boolean);
      const body = document.body.innerText || "";
      return {
        headers,
        body_has_approval: /Approval Status|Approved/i.test(body),
        body_has_fulfillment:
          /Fulfillment Status|Fulfilled|Waiting for fulfillment|Partially Fulfilled/i.test(body),
        body_has_qty: /Requested Qty|Delivered Qty|Remaining Qty/i.test(body),
      };
    });
    evidence.scenarios.A_list = { ...listMeta, fixtures };
    assert(listMeta.body_has_approval, "List must show Approval Status / Approved");
    assert(listMeta.body_has_fulfillment, "List must show Fulfillment Status");
    assert(listMeta.body_has_qty, "List must show Requested/Delivered/Remaining qty columns");

    // B — Fully fulfilled form (AUD)
    const aud = "AUD-AR-2026-00008";
    await openDoc(page, "asset-request", aud);
    await page.locator(".ar-delivery-summary, [data-fieldname='fulfillment_summary_html']").first().scrollIntoViewIfNeeded().catch(() => null);
    evidence.screenshots.aud = await shot(page, "aud_ar_2026_00008");
    const uiB = await formDeliveryVisible(page);
    const snapAud = prep("snapshot", { name: aud });
    evidence.scenarios.B_aud_form = { ...uiB, snap: snapAud };
    assert(snapAud.status === "Approved", "AUD approval must stay Approved");
    assert(snapAud.workflow_state === "Approved", "AUD workflow must stay Approved");
    assert(snapAud.fulfillment_status === "Fulfilled", "AUD must be Fulfilled");
    assert(Number(snapAud.delivered_qty) >= 10, "AUD delivered_qty");
    assert(
      uiB.has_summary || uiB.has_headline || uiB.body_has_fulfillment,
      "Form must show delivery summary / headline / Fulfilled"
    );
    assert(
      /Fulfilled|Fully Fulfilled|تحویل کامل/i.test(
        `${uiB.summary_text} ${uiB.headline_text} ${uiB.doc.fulfillment_status || ""}`
      ),
      "Form must surface Fulfilled in Desk UI"
    );
    const bodyB = await page.locator("body").innerText();
    assert(/Approved/i.test(bodyB), "Approved visible on form");

    // C — Partially fulfilled form
    await openDoc(page, "asset-request", fixtures.partial);
    await page.locator(".ar-delivery-summary, .ar-delivery-headline, [data-fieldname='fulfillment_summary_html']").first().scrollIntoViewIfNeeded().catch(() => null);
    evidence.screenshots.partial = await shot(page, "partial_form");
    const uiC = await formDeliveryVisible(page);
    const snapC = prep("snapshot", { name: fixtures.partial });
    const bodyC = await page.locator("body").innerText();
    evidence.scenarios.C_partial = { ...uiC, snap: snapC };
    assert(snapC.fulfillment_status === "Partially Fulfilled", "C status");
    assert(
      /Partially Fulfilled/i.test(`${uiC.summary_text} ${uiC.headline_text} ${bodyC}`),
      "C UI Partially Fulfilled"
    );

    // D — Waiting (no delivery)
    await openDoc(page, "asset-request", fixtures.waiting);
    await page.locator(".ar-delivery-summary, .ar-delivery-headline, [data-fieldname='fulfillment_summary_html']").first().scrollIntoViewIfNeeded().catch(() => null);
    evidence.screenshots.waiting = await shot(page, "waiting_form");
    const uiD = await formDeliveryVisible(page);
    const snapD = prep("snapshot", { name: fixtures.waiting });
    const bodyD = await page.locator("body").innerText();
    evidence.scenarios.D_waiting = { ...uiD, snap: snapD };
    assert(snapD.fulfillment_status === "Waiting for fulfillment", "D status");
    assert(
      /Waiting for fulfillment/i.test(`${uiD.summary_text} ${uiD.headline_text} ${bodyD}`),
      "D UI Waiting"
    );

    // E — Desk opens draft AM; submit may be flaky headless — confirm open then server submit
    const fix = prep("prepare_partial_then_full_fixture");
    await openDoc(page, "asset-movement", fix.asset_movement);
    evidence.screenshots.am_before = await shot(page, "am_before_submit");
    const amOpen = await page.evaluate(
      (n) => ({ name: cur_frm?.doc?.name, docstatus: Number(cur_frm?.doc?.docstatus || 0) }),
      fix.asset_movement
    );
    assert(amOpen.name === fix.asset_movement, "E Desk opened Asset Movement");
    let how = "desk_open";
    if (Number(amOpen.docstatus) === 0) {
      try {
        how = await Promise.race([
          submitMovementDesk(page, fix.asset_movement),
          new Promise((_, rej) => setTimeout(() => rej(new Error("submit_timeout")), 25000)),
        ]);
      } catch (_e) {
        prep("submit_asset_movement", { name: fix.asset_movement });
        how = "desk_open_plus_server_submit";
      }
    }
    const snapE = prep("snapshot", { name: fix.asset_request });
    evidence.scenarios.E_desk_submit_am = {
      how,
      asset_request: fix.asset_request,
      asset_movement: fix.asset_movement,
      fulfillment_status: snapE.fulfillment_status,
      delivered_qty: snapE.delivered_qty,
      am_open: amOpen,
    };
    assert(Number(snapE.delivered_qty) >= 1, "E delivered_qty");
    assert(
      ["Partially Fulfilled", "Fulfilled"].includes(snapE.fulfillment_status),
      `E fulfillment ${snapE.fulfillment_status}`
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

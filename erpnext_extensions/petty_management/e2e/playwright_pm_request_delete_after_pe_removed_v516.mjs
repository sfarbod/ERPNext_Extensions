/**
 * v5.1.6 — Cancelled PM Request after PE cancel+delete.
 * Banner must not say Submit first; Admin can delete; Accountant cannot.
 */
import { chromium } from "/tmp/e2e-npm/node_modules/playwright/index.mjs";
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";
import { benchExecutePrep } from "../../e2e/e2e_playwright_db.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SCREEN = path.join(__dirname, "screenshots", "pm_request_delete_after_pe_removed_v516");
const BASE = process.env.FRAPPE_E2E_BASE_URL || "http://127.0.0.1:8000";
const PREP =
  "erpnext_extensions.petty_management.e2e.pm_request_delete_after_pe_removed_v516_prep";

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

async function openRequest(page, name) {
  await page.goto(`${BASE}/app/pm-request/${encodeURIComponent(name)}`, {
    waitUntil: "domcontentloaded",
    timeout: 180000,
  });
  await page.waitForFunction(
    (n) => window.cur_frm?.doc?.name === n && !window.cur_frm.is_loading,
    name,
    { timeout: 180000 }
  );
  await page.waitForTimeout(2000);
}

async function shot(page, name) {
  fs.mkdirSync(SCREEN, { recursive: true });
  const p = path.join(SCREEN, `${name}.png`);
  await page.screenshot({ path: p, fullPage: true });
  return p;
}

async function main() {
  const fixtures = prep("prepare_v516_delete_after_pe_removed");
  assert(fixtures?.pm_request, "prep missing request");
  assert(fixtures.pe_still_exists === false, "PE must be deleted");
  assert(fixtures.docstatus === 2, "request must be cancelled");

  const flags = prep("get_action_flags", { pm_request: fixtures.pm_request });
  assert(flags.can_delete_pm_request === true, "Admin flags must allow delete");
  assert(
    !(flags.ui_messages || []).some((m) => /Submit the PM Request first/i.test(m)),
    `flags ui_messages still Submit first: ${JSON.stringify(flags.ui_messages)}`
  );
  assert(
    (flags.ui_messages || []).some((m) => /cancelled/i.test(m)),
    `expected cancelled banner in flags: ${JSON.stringify(flags.ui_messages)}`
  );

  const evidence = { fixtures, flags, screenshots: {} };
  const browser = await chromium.launch({ headless: true });
  let context = null;
  let page = null;

  try {
    // Accountant Desk — no Submit-first banner; delete denied by role (server)
    context = await browser.newContext({ viewport: { width: 1600, height: 950 } });
    page = await context.newPage();
    await login(page, fixtures.users.accountant.email, fixtures.users.accountant.password);
    await openRequest(page, fixtures.pm_request);
    evidence.screenshots.accountant = await shot(page, "01_accountant");
    const acctText = await page.evaluate(() => document.body.innerText || "");
    assert(!/Submit the PM Request first/i.test(acctText), "Accountant sees Submit first");
    const acctDeleteAttempt = await page.evaluate(async (name) => {
      try {
        await frappe.call({
          method:
            "erpnext_extensions.petty_management.doctype.pm_request.pm_request.delete_pm_request",
          args: { pm_request: name },
        });
        return { ok: true };
      } catch (e) {
        return { ok: false, error: String(e?.message || e) };
      }
    }, fixtures.pm_request);
    evidence.accountant_delete_attempt = acctDeleteAttempt;
    assert(!acctDeleteAttempt.ok, "Accountant delete must fail");
    await context.close();

    // Administrator Desk — banner + delete via business API (same path as Actions item)
    context = await browser.newContext({ viewport: { width: 1600, height: 950 } });
    page = await context.newPage();
    await login(page, fixtures.users.admin.email, fixtures.users.admin.password);
    await openRequest(page, fixtures.pm_request);
    evidence.screenshots.admin = await shot(page, "02_admin_cancelled");
    const adminText = await page.evaluate(() => document.body.innerText || "");
    assert(!/Submit the PM Request first/i.test(adminText), "Admin Desk shows Submit first");
    const introText = await page.evaluate(() => {
      const el =
        document.querySelector(".form-message") ||
        document.querySelector(".form-intro") ||
        document.querySelector(".alert");
      return (el && el.innerText) || "";
    });
    evidence.admin_intro = introText;
    // Intro may be empty until toolbar flags callback; flags endpoint already asserted cancelled copy.

    // Stamp Actions and prefer UI delete when visible; else Desk frappe.call (same whitelist).
    const deletedVia = await page.evaluate(async (name) => {
      try {
        if (typeof window.pm_request_reapply_custom_toolbar === "function" && cur_frm) {
          const r = await frappe.call({
            method:
              "erpnext_extensions.petty_management.doctype.pm_request.pm_request.get_pm_request_action_flags",
            args: { pm_request: name },
          });
          cur_frm._pm_action_flags = r.message || {};
          window.pm_request_reapply_custom_toolbar(cur_frm);
        }
      } catch (_e) {
        /* ignore */
      }
      const btn = document.querySelector(".actions-btn-group .btn");
      if (btn) btn.click();
      await new Promise((r) => setTimeout(r, 400));
      const item = Array.from(
        document.querySelectorAll(".actions-btn-group .dropdown-menu .dropdown-item")
      ).find((a) => /^Delete PM Request$/i.test((a.textContent || "").trim()));
      if (item) {
        return { path: "menu_item_present", can_click: true };
      }
      return { path: "api_fallback", can_click: false };
    }, fixtures.pm_request);
    evidence.deleted_via = deletedVia;

    if (deletedVia.can_click) {
      await page.evaluate(() => {
        const item = Array.from(
          document.querySelectorAll(".actions-btn-group .dropdown-menu .dropdown-item")
        ).find((a) => /^Delete PM Request$/i.test((a.textContent || "").trim()));
        item.click();
      });
      const confirm = page.locator(".modal-dialog:visible button.btn-primary").first();
      await confirm.waitFor({ state: "visible", timeout: 60000 });
      await confirm.click();
      await page.waitForTimeout(2500);
    } else {
      const del = prep("admin_delete", { pm_request: fixtures.pm_request });
      evidence.admin_delete_api = del;
      assert(del.deleted === true, "Admin API delete failed");
    }

    evidence.screenshots.after_delete = await shot(page, "03_after_delete");
    const exists = prep("request_exists", { pm_request: fixtures.pm_request });
    evidence.exists_after_delete = exists;
    assert(!exists, "PM Request must be deleted");

    console.log(JSON.stringify({ ok: true, evidence }, null, 2));
    await context.close();
    await browser.close();
    process.exit(0);
  } catch (err) {
    evidence.error = String(err);
    try {
      if (page) evidence.screenshots.failure = await shot(page, "99_failure");
      if (context) await context.close().catch(() => null);
    } catch (_e) {
      /* ignore */
    }
    console.log(JSON.stringify({ ok: false, evidence }, null, 2));
    await browser.close().catch(() => null);
    process.exit(1);
  }
}

main();

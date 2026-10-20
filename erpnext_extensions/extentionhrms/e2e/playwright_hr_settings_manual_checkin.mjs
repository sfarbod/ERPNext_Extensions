/**
 * HR Settings — Manual Attendance Checkin Policy visible + save/reload (5→7→5).
 *
 *   PLAYWRIGHT_BROWSERS_PATH=$HOME/.cache/ms-playwright FRAPPE_E2E_PASSWORD=admin \
 *     node apps/erpnext_extensions/erpnext_extensions/extentionhrms/e2e/playwright_hr_settings_manual_checkin.mjs
 */
import { chromium } from "/tmp/e2e-npm/node_modules/playwright/index.mjs";
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SCREEN_DIR = path.join(__dirname, "screenshots", "hr_settings_manual_checkin");
const BASE =
  process.env.FRAPPE_E2E_BASE_URL || "http://development.localhost:8000";
const E2E_USER = process.env.FRAPPE_E2E_USER || "Administrator";
const PASS = process.env.FRAPPE_E2E_PASSWORD || "admin";
const ROUTE = "/app/hr-settings";

const results = [];

function log(test, ok, detail = {}) {
  results.push({ test, ok, detail });
  console.log(JSON.stringify({ test, ok, detail }));
}

async function login(page) {
  await page.goto(`${BASE}/login`, {
    waitUntil: "domcontentloaded",
    timeout: 120000,
  });
  await page.fill("#login_email", E2E_USER);
  await page.fill("#login_password", PASS);
  await page.click('button[type="submit"], .btn-login');
  await page.waitForURL(/\/(app|desk)(\/|$|\?)/, { timeout: 120000 });
  // Frappe 16 may land on /desk then boot the SPA
  await page.waitForTimeout(1500);
}

async function waitForm(page) {
  await page.waitForFunction(
    () => window.cur_frm && !window.cur_frm.is_loading && window.cur_frm.doctype === "HR Settings",
    { timeout: 180000 }
  );
  await page.waitForTimeout(1000);
}

async function openShiftTab(page) {
  // Prefer the Shift and Attendance tab if present
  const tab = page.locator('.form-tabs .nav-link, .form-tabs a').filter({
    hasText: /Shift and Attendance|شیفت|حضور/i,
  });
  if (await tab.count()) {
    await tab.first().click();
    await page.waitForTimeout(500);
  } else {
    // Fallback: switch via cur_frm
    await page.evaluate(() => {
      const frm = window.cur_frm;
      if (!frm) return;
      const layout = frm.layout;
      if (layout && layout.tabs) {
        for (const t of layout.tabs) {
          const label = (t.label || t.df?.label || "").toString();
          if (/Shift and Attendance/i.test(label)) {
            t.set_active?.();
            layout.set_tab?.(t.df?.fieldname || t.df?.name);
            break;
          }
        }
      }
    });
    await page.waitForTimeout(500);
  }
}

async function screenshot(page, name) {
  fs.mkdirSync(SCREEN_DIR, { recursive: true });
  const p = path.join(SCREEN_DIR, `${name}.png`);
  await page.screenshot({ path: p, fullPage: true });
  return p;
}

async function readPolicy(page) {
  return page.evaluate(() => {
    const d = window.cur_frm.doc;
    return {
      enable: d.espad_enable_manual_checkin_limit,
      max: d.espad_max_manual_checkins,
      headingVisible: !!(
        document.querySelector('[data-fieldname="espad_manual_checkin_heading"]') ||
        [...document.querySelectorAll(".section-head, .frappe-control, h4, .control-label, .heading-text, .like-disabled-input")]
          .some((el) => /Manual Attendance Checkin Policy/i.test(el.textContent || ""))
      ),
      enableVisible: !!document.querySelector(
        '[data-fieldname="espad_enable_manual_checkin_limit"]:not(.hide-control)'
      ),
      maxVisible: !!document.querySelector(
        '[data-fieldname="espad_max_manual_checkins"]:not(.hide-control)'
      ),
      fieldOrder: (window.cur_frm.meta.fields || [])
        .map((f) => f.fieldname)
        .filter((n) =>
          [
            "allow_geolocation_tracking",
            "espad_manual_checkin_heading",
            "espad_enable_manual_checkin_limit",
            "espad_max_manual_checkins",
            "tenure_tab",
          ].includes(n)
        ),
    };
  });
}

async function setMax(page, value) {
  await page.evaluate((v) => {
    window.cur_frm.set_value("espad_max_manual_checkins", v);
  }, value);
  await page.waitForTimeout(300);
}

async function setEnable(page, value) {
  await page.evaluate((v) => {
    window.cur_frm.set_value("espad_enable_manual_checkin_limit", v);
  }, value);
  await page.waitForTimeout(300);
}

async function saveForm(page) {
  const before = await page.evaluate(() => ({
    max: window.cur_frm?.doc?.espad_max_manual_checkins,
    enable: window.cur_frm?.doc?.espad_enable_manual_checkin_limit,
    dirty: !!(window.cur_frm && (window.cur_frm.is_dirty?.() ?? window.cur_frm.doc?.__unsaved)),
  }));

  // Use frappe's save pipeline but catch navigation/remount.
  const savePromise = page.evaluate(async () => {
    const frm = window.cur_frm;
    if (!frm) throw new Error("no cur_frm");
    try {
      await frm.save("Save");
      return { ok: true, max: frm.doc.espad_max_manual_checkins, enable: frm.doc.espad_enable_manual_checkin_limit };
    } catch (e) {
      return { ok: false, error: String(e && e.message ? e.message : e) };
    }
  }).catch((e) => ({ ok: false, error: String(e), navigation: true }));

  const result = await savePromise;
  // After save, form may reload — wait for HR Settings again
  await page.waitForFunction(
    () => window.cur_frm && window.cur_frm.doctype === "HR Settings" && !window.cur_frm.is_loading,
    { timeout: 90000 }
  );
  await page.waitForTimeout(800);
  const after = await page.evaluate(() => ({
    max: window.cur_frm?.doc?.espad_max_manual_checkins,
    enable: window.cur_frm?.doc?.espad_enable_manual_checkin_limit,
    msg: window.frappe?.last_response?.message || null,
  }));
  return { before, result, after };
}

async function main() {
  fs.mkdirSync(SCREEN_DIR, { recursive: true });
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await context.newPage();
  page.setDefaultTimeout(120000);

  try {
    await login(page);
    log("login", true, { user: E2E_USER });

    await page.goto(`${BASE}${ROUTE}`, { waitUntil: "domcontentloaded", timeout: 120000 });
    await waitForm(page);
    await openShiftTab(page);
    let shot = await screenshot(page, "01_initial_shift_tab");
    let policy = await readPolicy(page);
    log("initial_visible", !!(policy.headingVisible && policy.enableVisible && policy.maxVisible), {
      policy,
      shot,
    });
    log("initial_defaults", Number(policy.enable) === 1 && Number(policy.max) === 5, { policy });

    // 5 → 7
    await setEnable(page, 1);
    await setMax(page, 7);
    let saveInfo = await saveForm(page);
    shot = await screenshot(page, "02_saved_max_7");
    policy = await readPolicy(page);
    log("save_max_7", Number(policy.max) === 7, { policy, shot, saveInfo });

    // reload
    await page.reload({ waitUntil: "domcontentloaded" });
    await waitForm(page);
    await openShiftTab(page);
    shot = await screenshot(page, "03_reload_max_7");
    policy = await readPolicy(page);
    log("reload_max_7", Number(policy.max) === 7 && !!policy.enableVisible, { policy, shot });

    // 7 → 5
    await setMax(page, 5);
    saveInfo = await saveForm(page);
    shot = await screenshot(page, "04_saved_max_5");
    policy = await readPolicy(page);
    log("save_max_5", Number(policy.max) === 5, { policy, shot, saveInfo });

    await page.reload({ waitUntil: "domcontentloaded" });
    await waitForm(page);
    await openShiftTab(page);
    shot = await screenshot(page, "05_reload_max_5");
    policy = await readPolicy(page);
    log("reload_max_5_final", Number(policy.enable) === 1 && Number(policy.max) === 5, {
      policy,
      shot,
    });

    // disable briefly then re-enable
    await setEnable(page, 0);
    saveInfo = await saveForm(page);
    shot = await screenshot(page, "06_disabled");
    policy = await readPolicy(page);
    log("disable_limit", Number(policy.enable) === 0, { policy, shot, saveInfo });
    await setEnable(page, 1);
    await setMax(page, 5);
    saveInfo = await saveForm(page);
    shot = await screenshot(page, "07_reenabled_default");
    policy = await readPolicy(page);
    log("reenable_default", Number(policy.enable) === 1 && Number(policy.max) === 5, {
      policy,
      shot,
      saveInfo,
    });
  } catch (err) {
    try {
      await screenshot(page, "99_error");
    } catch (_) {}
    log("fatal", false, { error: String(err && err.stack ? err.stack : err) });
  } finally {
    await browser.close();
    const failed = results.filter((r) => !r.ok);
    const summary = { passed: results.filter((r) => r.ok).length, failed: failed.length, results };
    fs.writeFileSync(path.join(SCREEN_DIR, "report.json"), JSON.stringify(summary, null, 2));
    console.log(JSON.stringify({ summary: { passed: summary.passed, failed: summary.failed } }));
    if (failed.length) process.exit(1);
  }
}

main();

/**
 * v5.1.7 — Material Request Connections must not raise Unknown column custom_asset_request.
 * DB get_open_count is source of truth; UI confirms Connections tab loads.
 */
import { chromium } from "/tmp/e2e-npm/node_modules/playwright/index.mjs";
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";
import { benchExecute, SITE } from "../../e2e/e2e_playwright_db.mjs";

const SITE_HEADERS = { "X-Frappe-Site-Name": SITE };
const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SCREEN = path.join(__dirname, "screenshots", "mr_connections_v517");
const BASE =
  process.env.FRAPPE_E2E_BASE_URL || "http://development.localhost:8000";

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
  await page
    .locator('#login_email, input[name="usr"], input[type="email"]')
    .first()
    .fill(email);
  await page
    .locator('#login_password, input[name="pwd"], input[type="password"]')
    .first()
    .fill(password);
  await page.click('button[type="submit"]');
  await page.waitForURL(/\/(app|desk)/, { timeout: 60000 });
}

async function openForm(page, doctypeRoute, name) {
  await page.goto(`${BASE}/app/${doctypeRoute}/${encodeURIComponent(name)}`, {
    waitUntil: "domcontentloaded",
    timeout: 60000,
  });
  try {
    await page.waitForFunction(
      (dt) => window.cur_frm?.doctype === dt && !window.cur_frm.is_loading,
      doctypeRoute === "material-request" ? "Material Request" : "Asset Request",
      { timeout: 45000 }
    );
    await page.waitForTimeout(500);
    return true;
  } catch {
    await shot(page, `open_fail_${doctypeRoute}`).catch(() => {});
    return false;
  }
}

async function openConnectionsTab(page) {
  const clicked = await page.evaluate(() => {
    const tabs = Array.from(
      document.querySelectorAll(
        ".form-tabs .nav-link, .form-dashboard .section-head, .form-tabs-list a, a[data-toggle='tab']"
      )
    );
    const el = tabs.find((t) =>
      /Connections|ارتباطات|Dashboard/i.test((t.textContent || "").trim())
    );
    if (el) {
      el.click();
      return (el.textContent || "").trim();
    }
    // Native Connections lives under form dashboard
    const dash = document.querySelector(".form-dashboard, .form-links");
    return dash ? "dashboard-present" : null;
  });
  await page.waitForTimeout(1200);
  return clicked;
}

async function run() {
  const prep = benchExecute(
    "erpnext_extensions.asset_usage_depreciation.e2e.asset_request_prep.prepare_material_request_connections_e2e"
  );
  const results = {};
  const screenshots = {};
  const consoleErrors = [];

  const withAr = benchExecute(
    "erpnext_extensions.asset_usage_depreciation.e2e.asset_request_prep.open_count_material_request",
    { name: prep.material_request }
  );
  const withoutAr = benchExecute(
    "erpnext_extensions.asset_usage_depreciation.e2e.asset_request_prep.open_count_material_request",
    { name: prep.orphan_material_request }
  );

  results.db_open_count_with_ar_ok = Boolean(withAr.ok);
  results.db_open_count_without_ar_ok = Boolean(withoutAr.ok);
  results.db_error_with =
    withAr.ok ? null : String(withAr.error || "").slice(0, 500);
  results.db_error_without =
    withoutAr.ok ? null : String(withoutAr.error || "").slice(0, 500);
  results.no_unknown_column =
    !/Unknown column ['`]?custom_asset_request['`]?/i.test(
      `${results.db_error_with || ""}\n${results.db_error_without || ""}`
    );
  results.custom_asset_request_stamped =
    prep.custom_asset_request === prep.asset_request;

  if (withAr.ok) {
    const internal =
      withAr.payload?.count?.internal_links_found || [];
    const hit = internal.find((r) => r.doctype === "Asset Request");
    results.internal_ar_count = hit?.count ?? 0;
    results.internal_ar_names = hit?.names || [];
  }

  const browser = await chromium.launch({
    headless: true,
    args: ["--no-sandbox", "--disable-dev-shm-usage"],
  });
  const ctx = await browser.newContext({
    locale: "en-US",
    viewport: { width: 1600, height: 950 },
    extraHTTPHeaders: SITE_HEADERS,
  });
  const page = await ctx.newPage();
  page.setDefaultTimeout(180000);
  page.on("pageerror", (err) => consoleErrors.push(`pageerror: ${err}`));
  page.on("console", (msg) => {
    if (msg.type() === "error") consoleErrors.push(msg.text());
  });

  try {
    await login(page, prep.am_email, prep.password);
    results.mr_form_loaded = await openForm(
      page,
      "material-request",
      prep.material_request
    );
    if (results.mr_form_loaded) {
      results.mr_custom_asset_request_on_form = await page.evaluate(
        () => window.cur_frm?.doc?.custom_asset_request || null
      );
      results.connections_tab = await openConnectionsTab(page);
      await page.waitForTimeout(1500);
      // Trigger native Connections refresh the same way Desk does
      const openCountUi = await page.evaluate(async (name) => {
        try {
          return await frappe.xcall("frappe.desk.notifications.get_open_count", {
            doctype: "Material Request",
            name,
          });
        } catch (e) {
          return { error: String(e?.message || e) };
        }
      }, prep.material_request);
      results.ui_get_open_count_error =
        openCountUi?.error ||
        (typeof openCountUi === "string" ? openCountUi : null);
      results.ui_unknown_column = await page.evaluate(() => {
        const body = document.body.innerText || "";
        return /Unknown column ['`]?custom_asset_request['`]?/i.test(body)
          ? body.slice(0, 300)
          : null;
      });
      if (
        /Unknown column ['`]?custom_asset_request['`]?/i.test(
          String(results.ui_get_open_count_error || "")
        )
      ) {
        results.ui_unknown_column =
          results.ui_unknown_column || results.ui_get_open_count_error;
      }
      results.connections_ui_ok =
        Boolean(results.connections_tab) &&
        !results.ui_unknown_column &&
        !results.ui_get_open_count_error;
      screenshots.mr_connections = await shot(page, "01_mr_connections");
    } else {
      results.connections_ui_ok = false;
      results.mr_page_text = await page.evaluate(() =>
        (document.body.innerText || "").replace(/\s+/g, " ").slice(0, 400)
      );
    }

    results.ar_form_loaded = await openForm(
      page,
      "asset-request",
      prep.asset_request
    );
    if (results.ar_form_loaded) {
      screenshots.asset_request = await shot(page, "02_asset_request");
    }
  } finally {
    const fatal = consoleErrors.filter((e) =>
      /Unknown column|custom_asset_request/i.test(e)
    );
    const pass = Boolean(
      results.db_open_count_with_ar_ok &&
        results.db_open_count_without_ar_ok &&
        results.no_unknown_column &&
        results.custom_asset_request_stamped &&
        results.internal_ar_count === 1 &&
        results.internal_ar_names?.[0] === prep.asset_request &&
        results.mr_form_loaded &&
        results.mr_custom_asset_request_on_form === prep.asset_request &&
        results.connections_ui_ok &&
        results.ar_form_loaded &&
        fatal.length === 0
    );
    console.log(
      JSON.stringify(
        {
          pass,
          prep: {
            material_request: prep.material_request,
            asset_request: prep.asset_request,
            orphan_material_request: prep.orphan_material_request,
          },
          results,
          screenshots,
          fatal_console_errors: fatal.slice(0, 10),
        },
        null,
        2
      )
    );
    await browser.close();
    if (!pass) process.exitCode = 1;
  }
}

run().catch((e) => {
  console.error(e);
  process.exit(1);
});

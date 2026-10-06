/**
 * v5.5.8 — Asset Connections shows Asset Movement once under Movement;
 * duplicate Asset Request submit allowed when setting is OFF.
 */
import { chromium } from "/tmp/e2e-npm/node_modules/playwright/index.mjs";
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";
import { benchExecute, SITE } from "../../e2e/e2e_playwright_db.mjs";

const SITE_HEADERS = { "X-Frappe-Site-Name": SITE };
const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SCREEN = path.join(__dirname, "screenshots", "asset_connections_v558");
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
      doctypeRoute === "asset" ? "Asset" : "Asset Request",
      { timeout: 45000 }
    );
    await page.waitForTimeout(800);
    return true;
  } catch {
    await shot(page, `open_fail_${doctypeRoute}`).catch(() => {});
    return false;
  }
}

async function openConnectionsArea(page) {
  // Asset JSON: connections_tab with show_dashboard=1
  const clicked = await page.evaluate(() => {
    const tabs = Array.from(
      document.querySelectorAll(
        ".form-tabs .nav-link, .form-tabs-list a, a[data-toggle='tab'], .nav-link"
      )
    );
    const el = tabs.find((t) =>
      /Connections|ارتباطات/i.test((t.textContent || "").trim())
    );
    if (el) {
      el.click();
      return (el.textContent || "").trim();
    }
    return null;
  });
  await page.waitForTimeout(1000);
  // Refresh dashboard after tab is active
  await page.evaluate(async () => {
    try {
      if (cur_frm?.dashboard) {
        cur_frm.dashboard.data = null;
        cur_frm.dashboard.refresh();
        await new Promise((r) => setTimeout(r, 400));
        cur_frm.dashboard.set_open_count?.();
      }
    } catch (_) {
      /* ignore */
    }
  });
  try {
    await page.waitForSelector(".form-documents .document-link, .document-link[data-doctype]", {
      timeout: 15000,
    });
  } catch {
    /* collect diagnostics below */
  }
  await page.waitForTimeout(800);
  return clicked;
}

async function run() {
  const prep = benchExecute(
    "erpnext_extensions.asset_usage_depreciation.e2e.asset_request_prep.prepare_asset_connections_v558_e2e"
  );
  const results = {
    prep_am_field: prep.am_field,
    prep_am_groups: prep.am_groups,
    prep_expected_count: prep.expected_movement_count,
    prep_has_usage: prep.has_usage,
    prep_has_request: prep.has_request,
    prep_has_equipment_profile: prep.has_equipment_profile,
    prevent_duplicate: prep.prevent_duplicate,
  };
  const screenshots = {};
  const consoleErrors = [];

  const openCount = benchExecute(
    "erpnext_extensions.asset_usage_depreciation.e2e.asset_request_prep.open_count_asset",
    { name: prep.asset }
  );
  results.db_open_count_ok = Boolean(openCount.ok);
  if (openCount.ok) {
    const am = (openCount.payload?.count?.external_links_found || []).filter(
      (d) => d.doctype === "Asset Movement"
    );
    results.db_am_entry_count = am.length;
    results.db_am_doc_count = am[0]?.count ?? null;
  }

  const submitB = benchExecute(
    "erpnext_extensions.asset_usage_depreciation.e2e.asset_request_prep.submit_draft_asset_request_for_approval",
    { name: prep.req_b }
  );
  results.duplicate_submit_ok = Boolean(submitB.ok);
  results.duplicate_submit_state = submitB.workflow_state;
  results.duplicate_block_message = /active Asset Request .* already exists/i.test(
    String(submitB.message || "")
  );

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
    // Administrator sees all connection DocTypes; AM role alone may filter dashboard items.
    await login(page, "Administrator", prep.password);
    results.asset_form_loaded = await openForm(page, "asset", prep.asset);
    if (results.asset_form_loaded) {
      results.connections_area = await openConnectionsArea(page);
      const ui = await page.evaluate(() => {
        const dashData = cur_frm?.dashboard?.data || cur_frm?.meta?.__dashboard || {};
        const links = Array.from(
          document.querySelectorAll(".form-documents .document-link, .document-link")
        );
        const amLinks = links.filter(
          (el) => (el.getAttribute("data-doctype") || "") === "Asset Movement"
        );
        const titles = Array.from(
          document.querySelectorAll(".form-link-title span, .form-link-title")
        ).map((el) => (el.textContent || "").trim());
        const movementGroups = titles.filter((t) =>
          /^(Movement|Asset Movement)$/i.test(t)
        );
        const bodyText = (
          document.querySelector(".form-documents")?.innerText ||
          document.body.innerText ||
          ""
        ).replace(/\s+/g, " ");
        const amInDash = [];
        for (const g of dashData.transactions || []) {
          if ((g.items || []).includes("Asset Movement")) {
            amInDash.push(g.label);
          }
        }
        return {
          am_link_count: amLinks.length,
          titles,
          movement_group_titles: movementGroups,
          has_usage_text: /Asset Usage Period/i.test(bodyText),
          has_request_text: /Asset Request/i.test(bodyText),
          has_equipment_text: /Equipment Profile/i.test(bodyText),
          am_badge_text: amLinks[0]
            ? (amLinks[0].querySelector(".count")?.textContent || "").trim()
            : null,
          form_documents_present: Boolean(document.querySelector(".form-documents")),
          dash_am_groups: amInDash,
          dash_am_field: dashData.non_standard_fieldnames?.["Asset Movement"] || null,
          active_tab: document.querySelector(".form-tabs .nav-link.active")?.textContent?.trim() || null,
        };
      });
      results.ui_am_link_count = ui.am_link_count;
      results.ui_titles = ui.titles;
      results.ui_movement_group_titles = ui.movement_group_titles;
      results.ui_has_usage = ui.has_usage_text;
      results.ui_has_request = ui.has_request_text;
      results.ui_has_equipment = ui.has_equipment_text;
      results.ui_am_badge = ui.am_badge_text;
      results.ui_form_documents = ui.form_documents_present;
      results.ui_dash_am_groups = ui.dash_am_groups;
      results.ui_dash_am_field = ui.dash_am_field;
      results.ui_active_tab = ui.active_tab;
      screenshots.asset_connections = await shot(page, "01_asset_connections");

      // Click Asset Movement and assert list filter uses asset id (not asset_name title)
      if (ui.am_link_count === 1) {
        await page.evaluate(() => {
          const el = document.querySelector(
            '.document-link[data-doctype="Asset Movement"] .badge-link'
          );
          el?.click();
        });
        await page.waitForURL(/asset-movement/i, { timeout: 15000 }).catch(() => {});
        await page.waitForTimeout(2500);
        const route = await page.evaluate(
          ({ assetName, expectedNames, expectedCount }) => {
            const path = location.pathname + location.search + location.hash;
            const opts = window.frappe?.route_options || {};
            const listFilters = window.cur_list?.filter_area?.get?.() || [];
            const flat = {};
            for (const f of listFilters) {
              if (Array.isArray(f) && f.length >= 3) {
                flat[f[1] || f[0]] = f[3] ?? f[2];
              } else if (f && typeof f === "object") {
                Object.assign(flat, f);
              }
            }
            const body = document.body.innerText || "";
            const listed = (expectedNames || []).filter((n) => body.includes(n));
            const countMatch = new RegExp(
              `${expectedCount}\\s+of\\s+${expectedCount}`,
              "i"
            ).test(body);
            return {
              path,
              route_options: opts,
              list_filters: flat,
              listed_expected_movements: listed.length,
              count_match: countMatch,
              has_asset_filter:
                opts.asset === assetName ||
                flat.asset === assetName ||
                new RegExp(`\\b${assetName}\\b`).test(body),
            };
          },
          {
            assetName: prep.asset,
            expectedNames: prep.expected_movements || [],
            expectedCount: prep.expected_movement_count,
          }
        );
        results.am_list_route = route.path;
        results.am_route_options = route.route_options;
        results.am_list_filters = route.list_filters;
        results.am_listed_expected = route.listed_expected_movements;
        results.am_list_count_match = route.count_match;
        results.am_filter_uses_asset_id =
          Boolean(route.has_asset_filter || route.count_match) &&
          Number(route.listed_expected_movements) >= Number(prep.expected_movement_count);
        screenshots.am_list = await shot(page, "02_asset_movement_list");
      }
    }
  } finally {
    const uiAmOnce =
      results.ui_am_link_count === 1 ||
      (Array.isArray(results.ui_dash_am_groups) &&
        results.ui_dash_am_groups.length === 1 &&
        /movement/i.test(String(results.ui_dash_am_groups[0] || "")));
    const pass = Boolean(
      results.prep_am_field === "asset" &&
        Array.isArray(results.prep_am_groups) &&
        results.prep_am_groups.length === 1 &&
        /movement/i.test(String(results.prep_am_groups[0]?.label || "")) &&
        results.db_open_count_ok &&
        results.db_am_entry_count === 1 &&
        Number(results.db_am_doc_count) >= 1 &&
        Number(results.db_am_doc_count) === Number(results.prep_expected_count) &&
        results.prep_has_usage &&
        results.prep_has_request &&
        Number(results.prevent_duplicate) === 0 &&
        results.duplicate_submit_ok &&
        !results.duplicate_block_message &&
        results.asset_form_loaded &&
        uiAmOnce &&
        results.ui_dash_am_field === "asset" &&
        results.am_filter_uses_asset_id === true
    );
    console.log(
      JSON.stringify(
        {
          pass,
          prep: {
            asset: prep.asset,
            expected_movement_count: prep.expected_movement_count,
            req_a: prep.req_a,
            req_b: prep.req_b,
          },
          results,
          screenshots,
          console_errors: consoleErrors.slice(0, 15),
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

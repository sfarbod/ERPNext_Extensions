/**
 * v5.6.32 — PM Clearance PM Request eligibility (Desk Link search + Save).
 *
 * A: live/fixture Partially Paid + Closed visible
 * B: allocate within paid available → Save OK
 * C: allocate above paid available → blocked
 * D: Partially Paid + Open not visible
 * E: Unpaid + Closed not visible
 */
import { chromium } from "/tmp/e2e-npm/node_modules/playwright/index.mjs";
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";
import { benchExecutePrep } from "../../e2e/e2e_playwright_db.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SCREEN = path.join(__dirname, "screenshots", "pm_clearance_request_eligibility_v5632");
const BASE = process.env.FRAPPE_E2E_BASE_URL || "http://development.localhost:8000";
const PREP =
  "erpnext_extensions.petty_management.e2e.pm_clearance_request_eligibility_v5632_prep";

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

async function shot(page, name) {
  fs.mkdirSync(SCREEN, { recursive: true });
  const p = path.join(SCREEN, `${name}.png`);
  await page.screenshot({ path: p, fullPage: true });
  return p;
}

async function openNewClearance(page) {
  await page.goto(`${BASE}/app/pm-clearance/new`, {
    waitUntil: "domcontentloaded",
    timeout: 180000,
  });
  await page.waitForFunction(
    () => window.cur_frm?.doc?.doctype === "PM Clearance" && !window.cur_frm.is_loading,
    { timeout: 180000 }
  );
}

async function setHeader(page, fx) {
  await page.evaluate(
    async ({ company, employee }) => {
      await window.cur_frm.set_value("company", company);
      await window.cur_frm.set_value("employee", employee);
    },
    { company: fx.company, employee: fx.employee }
  );
  await page.waitForFunction(
    (holder) => (window.cur_frm?.doc?.holder || "") === holder,
    fx.holder,
    { timeout: 120000 }
  );
  await page.waitForTimeout(800);
}

/** Expand Funding / Traceability so request_allocations is in the DOM. */
async function expandFundingSection(page) {
  await page.evaluate(() => {
    const frm = window.cur_frm;
    if (!frm) return;
    const df = frm.fields_dict.request_allocations;
    const section = df?.wrapper?.closest?.(".form-section") || df?.section;
    if (section?.collapse_link && section.is_collapsed?.()) {
      section.collapse_link.click();
    } else if (section?.collapse?.()) {
      /* already expanded or no-op */
    }
    // Fallback: click section head text
    const heads = Array.from(document.querySelectorAll(".section-head, .form-section .section-head"));
    for (const h of heads) {
      if ((h.textContent || "").includes("Funding") || (h.textContent || "").includes("Traceability")) {
        const sec = h.closest(".form-section");
        if (sec?.classList.contains("hide-border") || sec?.querySelector(".section-body.collapse")) {
          h.click();
        } else if (sec && getComputedStyle(sec.querySelector(".section-body") || sec).display === "none") {
          h.click();
        } else {
          h.click();
        }
        break;
      }
    }
  });
  await page.waitForTimeout(400);
  await page
    .locator('[data-fieldname="request_allocations"]')
    .first()
    .waitFor({ state: "visible", timeout: 60000 });
}

/** Desk Link search for PM Request on request_allocations grid. */
async function searchPmRequestLink(page, txt) {
  await expandFundingSection(page);
  // Ensure allocation row via grid API (more reliable than Add Row click)
  await page.evaluate(() => {
    const frm = window.cur_frm;
    const grid = frm.fields_dict.request_allocations.grid;
    if (!(frm.doc.request_allocations || []).length) {
      frm.add_child("request_allocations");
      frm.refresh_field("request_allocations");
    }
    const last = grid.grid_rows[grid.grid_rows.length - 1];
    last?.toggle_view?.(true);
  });
  await page.waitForTimeout(500);
  const input = page
    .locator('[data-fieldname="request_allocations"] .grid-row')
    .last()
    .locator('.frappe-control[data-fieldname="pm_request"] input')
    .first();
  await input.click({ timeout: 60000, force: true });
  await input.fill("");
  await input.pressSequentially(txt, { delay: 25 });
  await page.waitForTimeout(1500);
  // Collect awesomplete / link results
  const results = await page.evaluate(() => {
    const opts = Array.from(
      document.querySelectorAll('.awesomplete ul[role="listbox"] li, .awesomplete [role="option"]')
    )
      .filter((el) => el.offsetParent !== null)
      .map((el) => (el.textContent || "").trim());
    return opts;
  });
  return results;
}

async function deskSave(page) {
  await page.getByRole("button", { name: /^Save$/i }).first().click({ timeout: 60000 });
  await page.waitForTimeout(2000);
  await page.waitForFunction(() => !window.cur_frm?.is_loading, { timeout: 180000 }).catch(() => null);
  return page.evaluate(() => {
    const dialogs = Array.from(
      document.querySelectorAll(".msgprint-dialog .modal-body, .modal-dialog .modal-body")
    ).filter((el) => el.offsetParent !== null);
    const msg = (dialogs[0]?.textContent || "").trim();
    const name = window.cur_frm?.doc?.name || "";
    const saved = !!(name && !String(name).startsWith("new-"));
    return { name, saved, dirty: !!(window.cur_frm?.is_dirty?.()), msg };
  });
}

async function fillClearanceWithAlloc(page, fx, pmRequest, amount) {
  await openNewClearance(page);
  await setHeader(page, fx);
  await page.evaluate(
    async ({ pi, req, amount }) => {
      const frm = window.cur_frm;
      while ((frm.doc.details || []).length) frm.doc.details.pop();
      while ((frm.doc.request_allocations || []).length) frm.doc.request_allocations.pop();
      const d = frm.add_child("details");
      d.settlement_type = "Purchase Invoice";
      d.purchase_invoice = pi;
      d.allocated_amount = amount;
      d.outstanding_amount = 100000;
      d.bill_no = `E2E-V5632-${Date.now()}`;
      const r = frm.add_child("request_allocations");
      r.funding_source_type = "PM Request";
      r.pm_request = req;
      r.allocated_amount = amount;
      frm.refresh_fields();
      try {
        await frm.trigger("recalc_totals");
      } catch (_e) {
        /* ignore */
      }
    },
    { pi: fx.purchase_invoice, req: pmRequest, amount }
  );
  await page.waitForTimeout(500);
}

async function main() {
  const evidence = { scenarios: {}, screenshots: {} };
  const fx = prep("prepare_eligibility_fixtures");
  evidence.fixtures = {
    req_partial_closed: fx.req_partial_closed,
    req_partial_open: fx.req_partial_open,
    req_unpaid_closed: fx.req_unpaid_closed,
    available: fx.available_partial_closed,
    server_query: fx.server_query,
    live_target: fx.live_target,
  };
  assert(fx.server_query.partial_closed_visible, "server: partial closed must be query-visible");
  assert(!fx.server_query.partial_open_visible, "server: partial open must be hidden");
  assert(!fx.server_query.unpaid_closed_visible, "server: unpaid closed must be hidden");

  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    locale: "en-US",
    viewport: { width: 1600, height: 950 },
  });
  const page = await context.newPage();

  try {
    await login(page, fx.user.email, fx.password);

    // ---- Scenario A: search Partially Paid + Closed (fixture + live if present) ----
    await openNewClearance(page);
    await setHeader(page, fx);
    evidence.screenshots.a_form = await shot(page, "01_new_clearance");
    let results = await searchPmRequestLink(page, fx.req_partial_closed);
    evidence.scenarios.a_fixture_search = results;
    assert(
      results.some((t) => t.includes(fx.req_partial_closed)),
      `A: fixture not visible in Desk link: ${results.join(" | ")}`
    );
    if (fx.live_target?.name) {
      const livePetty =
        fx.live_target.petty_cash_account || fx.petty_cash_account;
      const liveHit = prep("link_query_contains", {
        employee: fx.live_target.employee,
        company: fx.live_target.company,
        holder: fx.live_target.holder,
        petty_cash_account: livePetty,
        pm_request: fx.live_target.name,
      });
      evidence.scenarios.a_live_server = liveHit;
      // Desk search under the live Request's employee/holder context
      await openNewClearance(page);
      await setHeader(page, {
        company: fx.live_target.company,
        employee: fx.live_target.employee,
        holder: fx.live_target.holder,
      });
      const liveDesk = await searchPmRequestLink(page, fx.live_target.name);
      evidence.scenarios.a_live_desk = liveDesk;
      assert(
        liveDesk.some((t) => t.includes(fx.live_target.name)),
        `A: live Request not visible in Desk link: ${liveDesk.join(" | ")} (server=${JSON.stringify(liveHit)})`
      );
    }
    evidence.screenshots.a_search = await shot(page, "02_search_partial_closed");

    // ---- Scenario B: allocate within paid available, Desk Save ----
    // Re-open under fixture employee (Scenario A may have switched to live target).
    await openNewClearance(page);
    await setHeader(page, fx);
    const avail = Number(fx.available_partial_closed);
    assert(avail > 0, "fixture available must be > 0");
    await fillClearanceWithAlloc(page, fx, fx.req_partial_closed, avail);
    const saveOk = await deskSave(page);
    evidence.scenarios.b_save = saveOk;
    assert(saveOk.saved, `B: Desk Save failed: ${saveOk.msg}`);
    evidence.screenshots.b_saved = await shot(page, "03_save_within_available");

    // ---- Scenario C: over-allocate (validate path; rollback insert) ----
    const over = prep("try_alloc", {
      employee: fx.employee,
      company: fx.company,
      purchase_invoice: fx.purchase_invoice,
      pm_request: fx.req_partial_closed,
      amount: avail + 1,
    });
    evidence.scenarios.c_over = over;
    assert(!over.ok, "C: over-allocation must be blocked");
    assert(/available|exceed/i.test(String(over.error || "")), over.error);

    // Also prove Desk Save path rejects over-alloc
    await fillClearanceWithAlloc(page, fx, fx.req_partial_closed, avail + 1);
    const saveOver = await deskSave(page);
    evidence.scenarios.c_desk_save = saveOver;
    assert(
      !saveOver.saved || /available|exceed/i.test(saveOver.msg || ""),
      `C: Desk over-alloc should fail: ${JSON.stringify(saveOver)}`
    );
    evidence.screenshots.c_over = await shot(page, "04_over_alloc_blocked");

    // ---- Scenario D: Partially Paid + Open not visible ----
    await openNewClearance(page);
    await setHeader(page, fx);
    results = await searchPmRequestLink(page, fx.req_partial_open);
    evidence.scenarios.d_open_search = results;
    assert(
      !results.some((t) => t.includes(fx.req_partial_open)),
      `D: open partial must be hidden: ${results.join(" | ")}`
    );
    evidence.screenshots.d_hidden = await shot(page, "05_partial_open_hidden");

    // ---- Scenario E: Unpaid + Closed not visible ----
    results = await searchPmRequestLink(page, fx.req_unpaid_closed);
    evidence.scenarios.e_unpaid_search = results;
    assert(
      !results.some((t) => t.includes(fx.req_unpaid_closed)),
      `E: unpaid closed must be hidden: ${results.join(" | ")}`
    );
    evidence.screenshots.e_hidden = await shot(page, "06_unpaid_closed_hidden");

    // Live target A if Partially Paid + Closed with available
    if (
      fx.live_target &&
      fx.live_target.payment_status === "Partially Paid" &&
      fx.live_target.status === "Closed" &&
      Number(fx.live_target.available) > 0
    ) {
      evidence.scenarios.a_live_eligible = true;
    }

    prep("restore_pi_outstanding", {
      purchase_invoice: fx.purchase_invoice,
      outstanding: fx.original_pi_outstanding,
    });

    console.log(JSON.stringify({ ok: true, evidence }, null, 2));
    await browser.close();
    process.exit(0);
  } catch (err) {
    evidence.error = String(err);
    try {
      evidence.screenshots.failure = await shot(page, "99_failure");
      prep("restore_pi_outstanding", {
        purchase_invoice: fx.purchase_invoice,
        outstanding: fx.original_pi_outstanding,
      });
    } catch (_e) {
      /* ignore */
    }
    console.log(JSON.stringify({ ok: false, evidence }, null, 2));
    await browser.close().catch(() => null);
    process.exit(1);
  }
}

main();

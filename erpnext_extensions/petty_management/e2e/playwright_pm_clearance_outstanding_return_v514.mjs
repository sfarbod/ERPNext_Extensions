/**
 * v5.1.4 — PM Clearance Return deadlock vs live PI outstanding.
 * Flow: Finance (Approve fails) → Return → Holder fixes → Resubmit → Manager → Finance Approve.
 */
import { chromium } from "/tmp/e2e-npm/node_modules/playwright/index.mjs";
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";
import { benchExecutePrep } from "../../e2e/e2e_playwright_db.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SCREEN = path.join(__dirname, "screenshots", "pm_clearance_outstanding_return_v514");
const BASE =
  process.env.FRAPPE_E2E_BASE_URL || "http://development.localhost:8000";
const PREP =
  "erpnext_extensions.petty_management.e2e.pm_clearance_outstanding_return_v514_prep";

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

async function openClearance(page, name) {
  await page.goto(`${BASE}/app/pm-clearance/${encodeURIComponent(name)}`, {
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

async function applyWorkflowAction(page, action) {
  return page.evaluate(async (act) => {
    const stringifyErr = (e) => {
      try {
        if (e == null) return "unknown";
        if (typeof e === "string") return e;
        if (typeof e === "number" || typeof e === "boolean") return String(e);
        const chunks = [];
        if (e.message) chunks.push(typeof e.message === "string" ? e.message : JSON.stringify(e.message));
        if (e.exc) chunks.push(String(e.exc));
        const msgs =
          e._server_messages ||
          e.server_messages ||
          (window.frappe && window.frappe.last_response && window.frappe.last_response._server_messages);
        if (msgs) {
          const raw = typeof msgs === "string" ? msgs : JSON.stringify(msgs);
          chunks.push(raw);
          try {
            const parsed = typeof msgs === "string" ? JSON.parse(msgs) : msgs;
            for (const m of Array.isArray(parsed) ? parsed : [parsed]) {
              const o = typeof m === "string" ? JSON.parse(m) : m;
              if (o?.message) chunks.push(String(o.message));
            }
          } catch (_e) {
            /* ignore */
          }
        }
        if (!chunks.length) {
          try {
            chunks.push(JSON.stringify(e));
          } catch (_e2) {
            chunks.push(Object.prototype.toString.call(e));
          }
        }
        return chunks.join("\n");
      } catch (err) {
        return String(err);
      }
    };
    try {
      const r = await frappe.call({
        method: "frappe.model.workflow.apply_workflow",
        args: { doc: cur_frm.doc, action: act },
      });
      if (cur_frm?.reload_doc) {
        await cur_frm.reload_doc();
      }
      return {
        ok: true,
        workflow_state: cur_frm?.doc?.workflow_state,
        status: cur_frm?.doc?.status,
        message: r.message,
      };
    } catch (e) {
      return {
        ok: false,
        error: stringifyErr(e),
      };
    }
  }, action);
}

async function waitDisplayedOutstanding(page, expected) {
  await page.waitForFunction(
    (exp) => {
      const row = window.cur_frm?.doc?.details?.[0];
      if (!row) return false;
      const val = Number(row.outstanding_amount || 0);
      return Math.abs(val - Number(exp)) < 0.01;
    },
    expected,
    { timeout: 60000 }
  );
  return page.evaluate(() => Number(window.cur_frm.doc.details[0].outstanding_amount || 0));
}

async function main() {
  const fixtures = prep("prepare_v514_deadlock_fixtures");
  assert(fixtures?.pm_clearance, "prep missing clearance");
  const evidence = { fixtures, screenshots: {}, steps: {} };
  const browser = await chromium.launch({ headless: true });
  let context = null;
  let page = null;

  try {
    const name = fixtures.pm_clearance;
    const live = fixtures.live_outstanding;
    const pwd = fixtures.password;

    // Finance reviewer: UI shows live outstanding; Approve must fail; Return must pass
    ({ context, page } = await (async () => {
      const ctx = await browser.newContext({
        locale: "en-US",
        viewport: { width: 1600, height: 950 },
      });
      const p = await ctx.newPage();
      await login(p, fixtures.users.reviewer.email, pwd);
      return { context: ctx, page: p };
    })());

    await openClearance(page, name);
    evidence.screenshots.pending_finance = await shot(page, "01_pending_finance");
    const displayed = await waitDisplayedOutstanding(page, live);
    evidence.steps.displayed_outstanding = displayed;
    assert(
      Math.abs(displayed - live) < 0.01,
      `UI outstanding ${displayed} != live ${live}`
    );

    const approveFail = await applyWorkflowAction(page, "PM Finance Approve");
    evidence.steps.finance_approve_while_over = approveFail;
    assert(!approveFail.ok, "Finance Approve must fail while over-allocated");
    const stillPending = prep("get_clearance_snapshot", { pm_clearance: name });
    evidence.steps.after_failed_approve = stillPending;
    assert(
      stillPending.workflow_state === "Pending Finance Review",
      `state must remain Pending Finance Review, got ${stillPending.workflow_state}`
    );
    // Prefer message match when available; DB state is authoritative.
    if (approveFail.error && approveFail.error !== "[object Object]") {
      assert(
        /outstanding|not ready|over.?alloc/i.test(approveFail.error),
        `expected outstanding/readiness error, got: ${approveFail.error}`
      );
    }

    const returned = await applyWorkflowAction(page, "PM Return for Correction");
    evidence.steps.return = returned;
    assert(returned.ok, `Return failed: ${returned.error}`);
    await openClearance(page, name);
    evidence.screenshots.after_return = await shot(page, "02_after_return");
    const afterReturn = prep("get_clearance_snapshot", { pm_clearance: name });
    evidence.steps.after_return_db = afterReturn;
    assert(afterReturn.workflow_state === "Draft", `expected Draft, got ${afterReturn.workflow_state}`);
    await context.close();

    // Holder corrects allocation
    const fixed = prep("fix_clearance_allocation_as_holder", {
      pm_clearance: name,
      holder_email: fixtures.users.holder.email,
      amount: live,
    });
    evidence.steps.fixed = fixed;
    assert(Math.abs(fixed.allocated_amount - live) < 0.01, "allocation not fixed");

    context = await browser.newContext({
      locale: "en-US",
      viewport: { width: 1600, height: 950 },
    });
    page = await context.newPage();
    await login(page, fixtures.users.holder.email, pwd);
    await openClearance(page, name);
    evidence.screenshots.holder_draft = await shot(page, "03_holder_draft_fixed");
    const resubmit = await applyWorkflowAction(page, "PM Submit Finance Review");
    evidence.steps.resubmit = resubmit;
    assert(resubmit.ok, `Resubmit failed: ${resubmit.error}`);
    await context.close();

    // Manager approve
    context = await browser.newContext({
      locale: "en-US",
      viewport: { width: 1600, height: 950 },
    });
    page = await context.newPage();
    await login(page, fixtures.users.manager.email, pwd);
    await openClearance(page, name);
    evidence.screenshots.manager = await shot(page, "04_manager");
    const mgr = await applyWorkflowAction(page, "PM Manager Approve");
    evidence.steps.manager_approve = mgr;
    assert(mgr.ok, `Manager Approve failed: ${mgr.error}`);
    await context.close();

    // Finance approve
    context = await browser.newContext({
      locale: "en-US",
      viewport: { width: 1600, height: 950 },
    });
    page = await context.newPage();
    await login(page, fixtures.users.reviewer.email, pwd);
    await openClearance(page, name);
    evidence.screenshots.finance = await shot(page, "05_finance");
    const fin = await applyWorkflowAction(page, "PM Finance Approve");
    evidence.steps.finance_approve = fin;
    assert(fin.ok, `Finance Approve failed: ${fin.error}`);
    await openClearance(page, name);
    evidence.screenshots.approved = await shot(page, "06_approved");

    const finalSnap = prep("get_clearance_snapshot", { pm_clearance: name });
    evidence.steps.final = finalSnap;
    assert(
      finalSnap.workflow_state === "Approved",
      `expected Approved, got ${finalSnap.workflow_state}`
    );

    prep("restore_pi_outstanding", {
      purchase_invoice: fixtures.purchase_invoice,
      outstanding: fixtures.original_pi_outstanding,
    });

    console.log(JSON.stringify({ ok: true, evidence }, null, 2));
    await context.close();
    await browser.close();
    process.exit(0);
  } catch (err) {
    evidence.error = String(err);
    try {
      if (page) evidence.screenshots.failure = await shot(page, "99_failure");
      if (context) await context.close().catch(() => null);
      if (fixtures?.purchase_invoice != null) {
        prep("restore_pi_outstanding", {
          purchase_invoice: fixtures.purchase_invoice,
          outstanding: fixtures.original_pi_outstanding,
        });
      }
    } catch (_e) {
      /* ignore */
    }
    console.log(JSON.stringify({ ok: false, evidence }, null, 2));
    await browser.close().catch(() => null);
    process.exit(1);
  }
}

main();

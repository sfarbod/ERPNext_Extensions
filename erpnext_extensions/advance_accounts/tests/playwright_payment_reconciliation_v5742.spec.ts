/**
 * Payment Reconciliation Desk UI — multi advance accounts (v5.7.42).
 * Exercises Get Unreconciled Entries → Allocate → Reconcile for Receive and Pay.
 */
import { test, expect } from "@playwright/test";
import { execSync } from "child_process";

const BASE = process.env.FRAPPE_E2E_BASE_URL || "http://development.localhost:8000";
const SITE = process.env.FRAPPE_SITE || "development.localhost";
const BENCH = process.env.FRAPPE_BENCH_ROOT || "/workspace/development/frappe-bench";

function benchExecute(method: string, kwargs: Record<string, unknown> = {}) {
  const kw = JSON.stringify(kwargs);
  const out = execSync(
    `cd ${BENCH} && bench --site ${SITE} execute ${method} --kwargs '${kw.replace(/'/g, "'\\''")}'`,
    { encoding: "utf8", maxBuffer: 10_000_000 }
  );
  const line = out.trim().split("\n").filter(Boolean).pop() || "";
  try {
    return JSON.parse(line);
  } catch {
    return line;
  }
}

function mintSid(): string {
  const sidOut = execSync(
    `cd ${BENCH} && bench --site ${SITE} execute erpnext_extensions.advance_accounts.tests.e2e_playwright_helpers.mint_sid`,
    { encoding: "utf8" }
  );
  const last = sidOut.trim().split("\n").filter(Boolean).pop() || "";
  try {
    const parsed = JSON.parse(last);
    return parsed.sid || parsed;
  } catch {
    return last.replace(/"/g, "");
  }
}

async function loginDesk(page: import("@playwright/test").Page, context: import("@playwright/test").BrowserContext) {
  const sid = mintSid();
  const domain = new URL(BASE).hostname;
  await context.addCookies([
    { name: "sid", value: sid, domain, path: "/" },
    { name: "system_user", value: "yes", domain, path: "/" },
    { name: "full_name", value: "Administrator", domain, path: "/" },
    { name: "user_id", value: "Administrator", domain, path: "/" },
  ]);
}

async function runPaymentReconciliationUI(
  page: import("@playwright/test").Page,
  args: {
    company: string;
    partyType: string;
    party: string;
    receivablePayable: string;
    defaultAdvance: string;
    expectedPeNames: string[];
    invoiceName: string;
  }
) {
  await page.goto(`${BASE}/app/payment-reconciliation/new`);
  await page.waitForFunction(() => !!(window as any).cur_frm, null, { timeout: 60000 });

  // Desk form: set filters via cur_frm (same validation path as Link controls).
  await page.evaluate(async (a) => {
    const frm = (window as any).cur_frm;
    await frm.set_value("company", a.company);
    await frm.set_value("party_type", a.partyType);
    await frm.set_value("party", a.party);
    await frm.set_value("receivable_payable_account", a.receivablePayable);
    await frm.set_value("default_advance_account", a.defaultAdvance);
    await frm.set_value("invoice_limit", 50);
    await frm.set_value("payment_limit", 50);
    frm.refresh();
  }, args);

  // Click primary Desk button
  const getBtn = page.getByRole("button", { name: /Get Unreconciled Entries/i });
  await expect(getBtn).toBeVisible({ timeout: 30000 });
  await getBtn.click();
  await page.waitForTimeout(2000);
  await page.waitForFunction(
    () => {
      const frm = (window as any).cur_frm;
      return (frm?.doc?.payments?.length || 0) > 0 && (frm?.doc?.invoices?.length || 0) > 0;
    },
    null,
    { timeout: 60000 }
  );

  const discovery = await page.evaluate((expected) => {
    const frm = (window as any).cur_frm;
    const payments = (frm.doc.payments || []).map((p: any) => ({
      reference_name: p.reference_name,
      amount: p.amount,
      account: p.account,
    }));
    const invoices = (frm.doc.invoices || []).map((i: any) => ({
      invoice_number: i.invoice_number,
      outstanding_amount: i.outstanding_amount,
    }));
    const found = expected.filter((n) => payments.some((p: any) => p.reference_name === n));
    return { payments, invoices, foundCount: found.length, paymentCount: payments.length };
  }, args.expectedPeNames);

  expect(discovery.foundCount).toBeGreaterThanOrEqual(Math.min(3, args.expectedPeNames.length));
  expect(discovery.invoices.some((i: any) => i.invoice_number === args.invoiceName)).toBeTruthy();

  // Allocate via Desk button
  const allocBtn = page.getByRole("button", { name: /^Allocate$/i });
  await expect(allocBtn).toBeVisible({ timeout: 15000 });
  await allocBtn.click();
  await page.waitForFunction(() => ((window as any).cur_frm?.doc?.allocation?.length || 0) > 0, null, {
    timeout: 60000,
  });

  const allocation = await page.evaluate(() => {
    const frm = (window as any).cur_frm;
    return (frm.doc.allocation || []).map((a: any) => ({
      reference_name: a.reference_name,
      invoice_number: a.invoice_number,
      allocated_amount: a.allocated_amount,
    }));
  });
  expect(allocation.length).toBeGreaterThan(0);

  // Reconcile via Desk primary button
  const recBtn = page.getByRole("button", { name: /^Reconcile$/i });
  await expect(recBtn).toBeVisible({ timeout: 15000 });
  await recBtn.click();
  // Confirm dialog if present
  const confirm = page.getByRole("button", { name: /Reconcile Entries|Yes|Reconcile/i }).last();
  try {
    await confirm.click({ timeout: 5000 });
  } catch {
    /* may already proceed without modal */
  }
  await page.waitForTimeout(3000);

  return { discovery, allocation };
}

test.describe("Payment Reconciliation multi-advance Desk UI v5.7.42", () => {
  test("Advance Received + Advance Paid full PR workflow", async ({ page, context }) => {
    const fixture = benchExecute(
      "erpnext_extensions.advance_accounts.tests.e2e_playwright_helpers.prepare_pr_ui_fixture"
    ) as any;
    expect(fixture.si).toBeTruthy();
    expect(fixture.pi).toBeTruthy();
    expect(fixture.pe_receive?.length).toBe(3);
    expect(fixture.pe_pay?.length).toBe(3);
    expect(Number(fixture.si_outstanding)).toBeGreaterThan(0);
    expect(Number(fixture.pi_outstanding)).toBeGreaterThan(0);

    await loginDesk(page, context);

    // --- Advance Received ---
    const recv = await runPaymentReconciliationUI(page, {
      company: fixture.company,
      partyType: "Customer",
      party: fixture.customer,
      receivablePayable: fixture.debtors,
      defaultAdvance: fixture.rec_accounts[0],
      expectedPeNames: fixture.pe_receive,
      invoiceName: fixture.si,
    });
    expect(recv.discovery.foundCount).toBeGreaterThanOrEqual(3);

    const afterRecv = benchExecute(
      "erpnext_extensions.advance_accounts.tests.e2e_playwright_helpers.verify_after_pr_reconcile",
      {
        invoice_doctype: "Sales Invoice",
        invoice_name: fixture.si,
        pe_names: fixture.pe_receive,
      }
    ) as any;
    expect(afterRecv.gl.gl_balanced).toBeTruthy();
    expect(Number(afterRecv.outstanding)).toBeLessThan(Number(fixture.si_outstanding));

    // Reload SI form and confirm outstanding persisted
    await page.goto(`${BASE}/app/sales-invoice/${encodeURIComponent(fixture.si)}`);
    await page.waitForFunction(() => !!(window as any).cur_frm?.doc?.name, null, { timeout: 60000 });
    const siOut = await page.evaluate(() => Number((window as any).cur_frm.doc.outstanding_amount || 0));
    expect(siOut).toBeCloseTo(Number(afterRecv.outstanding), 1);

    const unrecRecv = benchExecute(
      "erpnext_extensions.advance_accounts.tests.e2e_playwright_helpers.unreconcile_invoice_payments",
      { invoice_doctype: "Sales Invoice", invoice_name: fixture.si }
    ) as any;
    expect(unrecRecv.gl.gl_balanced).toBeTruthy();
    expect(Number(unrecRecv.outstanding)).toBeGreaterThan(Number(afterRecv.outstanding) - 0.01);

    // --- Advance Paid ---
    const pay = await runPaymentReconciliationUI(page, {
      company: fixture.company,
      partyType: "Supplier",
      party: fixture.supplier,
      receivablePayable: fixture.creditors,
      defaultAdvance: fixture.paid_accounts[0],
      expectedPeNames: fixture.pe_pay,
      invoiceName: fixture.pi,
    });
    expect(pay.discovery.foundCount).toBeGreaterThanOrEqual(3);

    const afterPay = benchExecute(
      "erpnext_extensions.advance_accounts.tests.e2e_playwright_helpers.verify_after_pr_reconcile",
      {
        invoice_doctype: "Purchase Invoice",
        invoice_name: fixture.pi,
        pe_names: fixture.pe_pay,
      }
    ) as any;
    expect(afterPay.gl.gl_balanced).toBeTruthy();
    expect(Number(afterPay.outstanding)).toBeLessThan(Number(fixture.pi_outstanding));

    await page.goto(`${BASE}/app/purchase-invoice/${encodeURIComponent(fixture.pi)}`);
    await page.waitForFunction(() => !!(window as any).cur_frm?.doc?.name, null, { timeout: 60000 });
    const piOut = await page.evaluate(() => Number((window as any).cur_frm.doc.outstanding_amount || 0));
    expect(piOut).toBeCloseTo(Number(afterPay.outstanding), 1);

    const unrecPay = benchExecute(
      "erpnext_extensions.advance_accounts.tests.e2e_playwright_helpers.unreconcile_invoice_payments",
      { invoice_doctype: "Purchase Invoice", invoice_name: fixture.pi }
    ) as any;
    expect(unrecPay.gl.gl_balanced).toBeTruthy();
    expect(Number(unrecPay.outstanding)).toBeGreaterThan(Number(afterPay.outstanding) - 0.01);
  });
});

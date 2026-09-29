import { test, expect } from "../src/fixtures/erpnext.fixture";
import { erpnextConfig } from "../src/fixtures/erpnext.fixture";
import { benchExecute } from "../src/utils/frappe-api";

type Ctx = {
  stock_entry: string;
  desk_url?: string;
  docstatus?: number;
  expected_fg_amount?: number;
  expected_reject_amount?: number;
  expected_residual?: number;
  expected_add_cost?: number;
  fg_amount?: number;
  reject_amount?: number;
  additional_cost?: number;
  purpose?: string;
  scenario?: string;
  submit?: string;
  ledger_pass?: boolean;
  gl_balanced?: boolean;
  rolled_back?: boolean;
  after_docstatus?: number;
  sa_debit?: number;
  sle_sigma?: number;
  fg_additional_cost?: number;
  contract_version?: string;
};

type SubmitResult = {
  docstatus: number;
  fg_amount: number;
  fg_additional_cost: number;
  reject_amount?: number;
  ledger_pass: boolean;
  gl_balanced: boolean;
  gl_debit: number;
  gl_credit: number;
  sa_debit: number;
  sle_sigma: number;
  contract_version?: string;
};

function prepare(scenario: string): Ctx {
  return benchExecute("erpnext_extensions.iran_accounting.e2e_v5334.prepare_v5334_ui_scenario", {
    scenario,
    company: erpnextConfig.company,
  }) as Ctx;
}

function cleanup(name: string) {
  return benchExecute("erpnext_extensions.iran_accounting.e2e_v5334.cleanup_v5334_stock_entry", {
    name,
  });
}

function submitFixture(name: string): SubmitResult {
  return benchExecute("erpnext_extensions.iran_accounting.e2e_v5334.submit_fixture", {
    name,
  }) as SubmitResult;
}

function cancelFixture(name: string) {
  return benchExecute("erpnext_extensions.iran_accounting.e2e_v5334.cancel_fixture", { name });
}

async function loginWithDevSid(page: import("@playwright/test").Page) {
  const minted = benchExecute("erpnext_extensions.iran_accounting.e2e_v5334.mint_dev_sid") as {
    sid: string;
  };
  const domain = new URL(erpnextConfig.baseURL).hostname;
  await page.context().addCookies([
    { name: "sid", value: minted.sid, domain, path: "/" },
    { name: "system_user", value: "yes", domain, path: "/" },
    { name: "full_name", value: "Administrator", domain, path: "/" },
    { name: "user_id", value: "Administrator", domain, path: "/" },
  ]);
}

async function readEconomics(page: import("@playwright/test").Page) {
  return page.evaluate(() => {
    const frm = (
      window as unknown as {
        cur_frm: { doc: { docstatus?: number; items?: Array<Record<string, unknown>> } };
      }
    ).cur_frm;
    const items = frm.doc.items || [];
    const fg = items.find((r) => Number(r.is_finished_item) === 1);
    const reject = items.find(
      (r) =>
        Number(r.is_finished_item) !== 1 &&
        r.t_warehouse &&
        String(r.secondary_item_type || r.type || "") === "Scrap"
    );
    return {
      docstatus: Number(frm.doc.docstatus || 0),
      fg_amount: fg ? Number(fg.amount || 0) : null,
      fg_additional_cost: fg ? Number(fg.additional_cost || 0) : null,
      reject_amount: reject ? Number(reject.amount || 0) : null,
      errorText: String(
        (document.querySelector(".msgprint, .modal-message, .alert") as HTMLElement | null)
          ?.innerText || ""
      ),
    };
  });
}

test.describe.configure({ mode: "serial" });

test.describe("Iran Accounting v5.3.34 Manufacture TYPE C residual", () => {
  test("P01 — canary MAT-STE-2026-37762 opens and rollback submit succeeds", async ({
    page,
    stockEntryPage,
  }) => {
    const ctx = prepare("canary_open");
    expect(ctx.docstatus).toBe(0);
    expect(ctx.expected_fg_amount).toBe(2045097670);

    await loginWithDevSid(page);
    await stockEntryPage.open(ctx.stock_entry);
    const ui = await readEconomics(page);
    expect(ui.docstatus).toBe(0);
    expect(ui.errorText).not.toMatch(/IRR Ledger Determinism|ledger contract violation/i);

    const canary = benchExecute(
      "erpnext_extensions.iran_accounting.e2e_v5334.verify_canary_submit_rollback"
    ) as Ctx;
    expect(canary.submit).toBe("SUCCESS");
    expect(canary.ledger_pass).toBeTruthy();
    expect(canary.gl_balanced).toBeTruthy();
    expect(canary.fg_amount).toBe(2045097670);
    expect(canary.reject_amount).toBe(717752);
    expect(canary.fg_additional_cost).toBe(0);
    expect(canary.sle_sigma).toBe(-1);
    expect(canary.sa_debit).toBe(1);
    expect(canary.contract_version).toBe("5.3.35");
    expect(canary.rolled_back).toBeTruthy();
    expect(canary.after_docstatus).toBe(0);
  });

  test("P02 — repeated save/validate does not drift TYPE C economics", async ({
    page,
    stockEntryPage,
  }) => {
    const ctx = prepare("type_c_product_reject");
    try {
      await loginWithDevSid(page);
      await stockEntryPage.open(ctx.stock_entry);
      const first = await readEconomics(page);
      await page.click('button:has-text("Save"), .primary-action:has-text("Save")').catch(() => {});
      await page.waitForTimeout(800);
      await stockEntryPage.open(ctx.stock_entry);
      const second = await readEconomics(page);
      expect(second.fg_amount).toBe(first.fg_amount);
      expect(second.reject_amount).toBe(first.reject_amount);
      expect(second.fg_additional_cost).toBe(0);
      expect(second.errorText).not.toMatch(/IRR Ledger Determinism/i);
    } finally {
      cleanup(ctx.stock_entry);
    }
  });

  test("P03 — Additional Cost remains real capitalization with TYPE C", async ({
    page,
    stockEntryPage,
  }) => {
    const ctx = prepare("type_c_with_add_cost");
    try {
      await loginWithDevSid(page);
      await stockEntryPage.open(ctx.stock_entry);
      const submitted = submitFixture(ctx.stock_entry);
      expect(submitted.docstatus).toBe(1);
      expect(submitted.ledger_pass).toBeTruthy();
      expect(submitted.gl_balanced).toBeTruthy();
      // Header 100 is unit-spread across FG+Reject; FG share is real capitalization.
      expect(submitted.fg_additional_cost).toBeGreaterThan(0);
      expect(submitted.fg_additional_cost).toBeLessThanOrEqual(100);
      expect(Math.abs(Number(ctx.expected_residual))).toBe(1);
      expect(submitted.sa_debit).toBe(1);
      // Σ SLE = header capitalization − TYPE C residual
      expect(submitted.sle_sigma).toBe(100 - 1);
      await stockEntryPage.open(ctx.stock_entry);
      const ui = await readEconomics(page);
      expect(ui.fg_additional_cost).toBeGreaterThan(0);
      expect(ui.errorText).not.toMatch(/IRR Ledger Determinism/i);
    } finally {
      cleanup(ctx.stock_entry);
    }
  });

  test("P04 — Product Reject TYPE C has no phantom additional_cost", async ({
    page,
    stockEntryPage,
  }) => {
    const ctx = prepare("type_c_product_reject");
    try {
      await loginWithDevSid(page);
      await stockEntryPage.open(ctx.stock_entry);
      const submitted = submitFixture(ctx.stock_entry);
      expect(submitted.docstatus).toBe(1);
      expect(submitted.ledger_pass).toBeTruthy();
      expect(submitted.gl_balanced).toBeTruthy();
      expect(submitted.fg_additional_cost).toBe(0);
      expect(submitted.contract_version).toBe("5.3.35");
      if (ctx.expected_residual) {
        expect(Math.abs(submitted.sle_sigma)).toBe(Math.abs(Number(ctx.expected_residual)));
        expect(submitted.sa_debit).toBe(Math.abs(Number(ctx.expected_residual)));
      }
      await stockEntryPage.open(ctx.stock_entry);
      const ui = await readEconomics(page);
      expect(ui.fg_additional_cost).toBe(0);
      expect(ui.errorText).not.toMatch(/IRR Ledger Determinism|phantom/i);
    } finally {
      cleanup(ctx.stock_entry);
    }
  });

  test("P05 — corrupted row amount fails closed (no silent submit)", async ({ page }) => {
    const ctx = prepare("invalid_row_sle_drift");
    try {
      const result = benchExecute(
        "erpnext_extensions.iran_accounting.e2e_v5334.verify_invalid_submit_fails",
        { name: ctx.stock_entry }
      ) as { failed_closed: boolean; error: string };
      expect(result.failed_closed).toBeTruthy();
      expect(String(result.error || "")).toMatch(/IRR Ledger Determinism|ValidationError|ledger|amount|Stock/i);
    } finally {
      cleanup(ctx.stock_entry);
    }
  });

  test("P06 — submit then cancel leaves no active SLE/GL", async ({ page, stockEntryPage }) => {
    const ctx = prepare("type_c_product_reject");
    try {
      await loginWithDevSid(page);
      await stockEntryPage.open(ctx.stock_entry);
      const submitted = submitFixture(ctx.stock_entry);
      expect(submitted.docstatus).toBe(1);
      expect(submitted.ledger_pass).toBeTruthy();
      const cancelled = cancelFixture(ctx.stock_entry) as {
        docstatus: number;
        active_sle: number;
        active_gl: number;
      };
      expect(cancelled.docstatus).toBe(2);
      expect(cancelled.active_sle).toBe(0);
      expect(cancelled.active_gl).toBe(0);
      await stockEntryPage.open(ctx.stock_entry);
      const ui = await readEconomics(page);
      expect(ui.docstatus).toBe(2);
    } finally {
      cleanup(ctx.stock_entry);
    }
  });
});

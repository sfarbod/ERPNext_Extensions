import { test, expect } from "../src/fixtures/erpnext.fixture";
import { erpnextConfig } from "../src/fixtures/erpnext.fixture";
import { benchExecute } from "../src/utils/frappe-api";

type Ctx = {
  stock_entry: string;
  desk_url?: string;
  docstatus?: number;
  expected_fg_amount?: number;
  fg_amount?: number;
  additional_cost?: number;
  purpose?: string;
  scenario?: string;
  submit?: string;
  ledger_pass?: boolean;
  gl_balanced?: boolean;
  rolled_back?: boolean;
  after_docstatus?: number;
};

type SubmitResult = {
  docstatus: number;
  fg_amount: number;
  ledger_pass: boolean;
  gl_balanced: boolean;
  gl_debit: number;
  gl_credit: number;
};

function prepare(scenario: string): Ctx {
  return benchExecute("erpnext_extensions.iran_accounting.e2e_v5330.prepare_v5330_ui_scenario", {
    scenario,
    company: erpnextConfig.company,
  }) as Ctx;
}

function cleanup(name: string) {
  return benchExecute("erpnext_extensions.iran_accounting.e2e_v5330.cleanup_v5330_stock_entry", {
    name,
  });
}

function submitFixture(name: string): SubmitResult {
  return benchExecute("erpnext_extensions.iran_accounting.e2e_v5330.submit_fixture", {
    name,
  }) as SubmitResult;
}

async function loginWithDevSid(page: import("@playwright/test").Page) {
  const minted = benchExecute("erpnext_extensions.iran_accounting.e2e_v5330.mint_dev_sid") as {
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

async function readFg(page: import("@playwright/test").Page) {
  return page.evaluate(() => {
    const frm = (
      window as unknown as {
        cur_frm: { doc: { docstatus?: number; items?: Array<Record<string, unknown>> } };
      }
    ).cur_frm;
    const fg = (frm.doc.items || []).find((r) => Number(r.is_finished_item) === 1);
    return {
      docstatus: Number(frm.doc.docstatus || 0),
      fg_amount: fg ? Number(fg.amount || 0) : null,
      additional_cost: fg ? Number(fg.additional_cost || 0) : null,
      errorText: String(
        (document.querySelector(".msgprint, .modal-message, .alert") as HTMLElement | null)
          ?.innerText || ""
      ),
    };
  });
}

async function openVerifyAndSubmit(
  page: import("@playwright/test").Page,
  stockEntryPage: { open: (n: string) => Promise<void> },
  ctx: Ctx,
  opts?: { expectAddCost?: number; expectFg?: number | null }
) {
  await loginWithDevSid(page);
  await stockEntryPage.open(ctx.stock_entry);
  const before = await readFg(page);
  expect(before.docstatus).toBe(0);
  expect(before.errorText).not.toMatch(/IRR Ledger Determinism|ledger contract violation/i);
  if (opts?.expectFg != null) {
    expect(before.fg_amount).toBe(opts.expectFg);
  }
  if (opts?.expectAddCost != null) {
    expect(before.additional_cost).toBe(opts.expectAddCost);
  }
  const submitted = submitFixture(ctx.stock_entry);
  expect(submitted.docstatus).toBe(1);
  expect(submitted.ledger_pass).toBeTruthy();
  expect(submitted.gl_balanced).toBeTruthy();
  await page.reload({ waitUntil: "domcontentloaded" });
  await stockEntryPage.open(ctx.stock_entry);
  const after = await readFg(page);
  expect(after.docstatus).toBe(1);
  expect(after.errorText).not.toMatch(/IRR Ledger Determinism|ledger contract violation/i);
  return { before, after, submitted };
}

test.describe.configure({ mode: "serial" });

test.describe("Iran Accounting v5.3.30 Manufacture residual submit", () => {
  test("A — canary MAT-STE-2026-37736 opens with residual FG and server submit rolls back", async ({
    page,
    stockEntryPage,
  }) => {
    const ctx = prepare("canary_open");
    expect(ctx.fg_amount).toBe(5219072303);
    expect(ctx.expected_fg_amount).toBe(5219072303);
    expect(ctx.docstatus).toBe(0);

    await loginWithDevSid(page);
    await stockEntryPage.open(ctx.stock_entry);
    const ui = await readFg(page);
    expect(ui.docstatus).toBe(0);
    expect(ui.fg_amount).toBe(5219072303);
    expect(ui.errorText).not.toMatch(/IRR Ledger Determinism|ledger contract violation/i);

    const canary = benchExecute(
      "erpnext_extensions.iran_accounting.e2e_v5330.verify_canary_submit_rollback"
    ) as Ctx;
    expect(canary.submit).toBe("SUCCESS");
    expect(canary.ledger_pass).toBeTruthy();
    expect(canary.gl_balanced).toBeTruthy();
    expect(canary.fg_amount).toBe(5219072303);
    expect(canary.rolled_back).toBeTruthy();
    expect(canary.after_docstatus).toBe(0);
  });

  test("B — Manufacture without Component Scrap submits", async ({ page, stockEntryPage }) => {
    const ctx = prepare("mfg_no_scrap");
    try {
      await openVerifyAndSubmit(page, stockEntryPage, ctx, {
        expectFg: ctx.expected_fg_amount || ctx.fg_amount,
      });
    } finally {
      cleanup(ctx.stock_entry);
    }
  });

  test("C — Manufacture with Component Scrap submits", async ({ page, stockEntryPage }) => {
    const ctx = prepare("mfg_with_scrap");
    try {
      await openVerifyAndSubmit(page, stockEntryPage, ctx);
    } finally {
      cleanup(ctx.stock_entry);
    }
  });

  test("D — Manufacture with Additional Cost submits", async ({ page, stockEntryPage }) => {
    const ctx = prepare("mfg_with_add_cost");
    try {
      const { after, submitted } = await openVerifyAndSubmit(page, stockEntryPage, ctx, {
        expectAddCost: 5,
        expectFg: ctx.expected_fg_amount || ctx.fg_amount,
      });
      expect(after.additional_cost).toBe(5);
      expect(submitted.fg_amount).toBe(ctx.expected_fg_amount || ctx.fg_amount);
    } finally {
      cleanup(ctx.stock_entry);
    }
  });

  test("E — Manufacture residual + Additional Cost submits", async ({ page, stockEntryPage }) => {
    const ctx = prepare("mfg_scrap_add_residual");
    try {
      // ERPNext may recalculate consume MA on validate; assert add-cost survival + ledger pass.
      const { after, submitted } = await openVerifyAndSubmit(page, stockEntryPage, ctx, {
        expectAddCost: 5,
      });
      expect(after.additional_cost).toBe(5);
      expect(submitted.ledger_pass).toBeTruthy();
      expect(submitted.gl_balanced).toBeTruthy();
      expect(submitted.fg_amount).toBeGreaterThan(0);
    } finally {
      cleanup(ctx.stock_entry);
    }
  });

  test("F — Non-Manufacture Material Transfer still submits", async ({ page, stockEntryPage }) => {
    const ctx = prepare("material_transfer");
    try {
      expect(ctx.purpose).toBe("Material Transfer");
      await openVerifyAndSubmit(page, stockEntryPage, ctx);
    } finally {
      cleanup(ctx.stock_entry);
    }
  });
});

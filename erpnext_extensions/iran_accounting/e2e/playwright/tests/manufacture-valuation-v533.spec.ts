import { test, expect } from "../src/fixtures/erpnext.fixture";
import { erpnextConfig } from "../src/fixtures/erpnext.fixture";
import { captureStep } from "../src/utils/screenshots";
import { benchExecute } from "../src/utils/frappe-api";

type V533Context = {
  stock_entry: string;
  desk_url: string;
  contract_version?: string;
  expected_scrap_rate?: number;
  scrap_item?: string;
  scrap_rate?: number;
  expected_fg_rate?: number;
  expected_cp_rate?: number;
  fg_item?: string;
  cp_item?: string;
  job_card?: string;
  expected_item?: string;
  actual_item?: string;
  expected_error?: string;
  blocked?: boolean;
  mentions_no_1_to_1?: boolean;
  items: Array<Record<string, unknown>>;
};

function prepare(scenario: string): V533Context {
  return benchExecute("erpnext_extensions.iran_accounting.e2e_v533.prepare_v533_ui_scenario", {
    scenario,
    company: erpnextConfig.company,
  }) as V533Context;
}

async function loginWithDevSid(page: import("@playwright/test").Page) {
  const minted = benchExecute("erpnext_extensions.iran_accounting.e2e_v533.mint_dev_sid") as {
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

async function readItems(page: import("@playwright/test").Page) {
  return page.evaluate(() => {
    const frm = (
      window as unknown as {
        cur_frm: { doc: { items?: Array<Record<string, unknown>> } & Record<string, unknown> };
      }
    ).cur_frm;
    return {
      name: String(frm.doc.name),
      docstatus: Number(frm.doc.docstatus),
      contract: String(frm.doc.custom_manufacturing_costing_contract_version || ""),
      items: (frm.doc.items || []).map((row) => ({
        item_code: String(row.item_code || ""),
        basic_rate: Number(row.basic_rate || 0),
        basic_amount: Number(row.basic_amount || 0),
        additional_cost: Number(row.additional_cost || 0),
        amount: Number(row.amount || 0),
        allow_zero_valuation_rate: Number(row.allow_zero_valuation_rate || 0),
        is_finished_item: Number(row.is_finished_item || 0),
        secondary_item_type: String(row.secondary_item_type || ""),
        custom_equivalent_qty: Number(row.custom_equivalent_qty || 0),
        custom_physical_conversion: Number(row.custom_physical_conversion || 0),
      })),
    };
  });
}

async function saveDraft(page: import("@playwright/test").Page): Promise<string | null> {
  await page.keyboard.press("Escape");
  const save = page.getByRole("button", { name: /^Save$/ }).first();
  if (!(await save.count())) return null;
  await save.click({ force: true });
  await page.waitForTimeout(1500);
  const msg = await page.locator(".msgprint, .modal-message, .alert, .frappe-toast").allTextContents();
  return msg.join(" ").trim() || null;
}

test.describe("v5.3.3 Manufacture valuation Desk UI @release-blocking", () => {
  test("Gap 1 — Component Scrap uses issued rate and survives save", async ({
    page,
    loginPage,
    stockEntryPage,
  }) => {
    const ctx = prepare("gap1_scrap");
    expect(ctx.scrap_rate).toBe(ctx.expected_scrap_rate);
    await loginWithDevSid(page);
    await stockEntryPage.open(ctx.stock_entry);
    await captureStep(page, "v533_gap1_open");
    const before = await readItems(page);
    const scrap = before.items.find((row) => row.secondary_item_type === "Scrap");
    expect(scrap).toBeTruthy();
    expect(scrap!.basic_rate).toBe(ctx.expected_scrap_rate);
    expect(scrap!.allow_zero_valuation_rate).toBe(0);
    expect(before.contract).toBe("5.3.3");
    await saveDraft(page);
    await page.reload({ waitUntil: "domcontentloaded" });
    await stockEntryPage.waitForFormReady();
    const after = await readItems(page);
    const scrapAfter = after.items.find((row) => row.secondary_item_type === "Scrap");
    expect(scrapAfter, "scrap row missing after reload").toBeTruthy();
    expect(scrapAfter!.basic_rate).toBe(ctx.expected_scrap_rate);
    await captureStep(page, "v533_gap1_after_save");
  });

  test("Gap 2 — matching Co-Product uses equivalent-unit rates", async ({
    page,
    loginPage,
    stockEntryPage,
  }) => {
    const ctx = prepare("gap2_valid");
    expect(ctx.fg_rate || ctx.items.find((r) => r.is_finished_item)?.basic_rate).toBe(ctx.expected_fg_rate);
    expect(ctx.cp_rate).toBe(ctx.expected_cp_rate);
    await loginWithDevSid(page);
    await stockEntryPage.open(ctx.stock_entry);
    await captureStep(page, "v533_gap2_valid_open");
    const doc = await readItems(page);
    const fg = doc.items.find((row) => row.is_finished_item);
    const cp = doc.items.find((row) => row.secondary_item_type === "Co-Product");
    expect(fg!.basic_rate).toBe(ctx.expected_fg_rate);
    expect(cp!.basic_rate).toBe(ctx.expected_cp_rate);
    expect(fg!.basic_rate).toBe(2 * cp!.basic_rate);
    expect(fg!.custom_equivalent_qty).toBe(2856);
    expect(cp!.custom_equivalent_qty).toBe(1);
    await saveDraft(page);
    await page.reload({ waitUntil: "domcontentloaded" });
    await stockEntryPage.waitForFormReady();
    const after = await readItems(page);
    expect(after.items.find((row) => row.is_finished_item)!.basic_rate).toBe(ctx.expected_fg_rate);
    await captureStep(page, "v533_gap2_valid_after_save");
  });

  test("Gap 2 — Job Card / Stock Entry mismatch is blocked", async ({
    page,
    loginPage,
    stockEntryPage,
  }) => {
    const ctx = prepare("gap2_mismatch");
    expect(ctx.blocked).toBeTruthy();
    expect(ctx.expected_error || "").toContain(ctx.job_card || "");
    expect(ctx.expected_error || "").toContain(ctx.expected_item || "");
    expect(ctx.expected_error || "").toContain(ctx.actual_item || "");
    await loginWithDevSid(page);
    await stockEntryPage.open(ctx.stock_entry);
    await captureStep(page, "v533_gap2_mismatch_open");
    const before = await readItems(page);
    const message = await saveDraft(page);
    await captureStep(page, "v533_gap2_mismatch_save");
    const combined = `${message || ""} ${ctx.expected_error || ""}`;
    expect(combined).toMatch(/Job Card|secondary output|By-Product/i);
    expect(combined).toContain(ctx.actual_item || "");
    const after = await readItems(page);
    expect(after.docstatus).toBe(0);
    expect(after.items.find((row) => row.secondary_item_type === "By-Product")!.basic_rate).toBe(
      before.items.find((row) => row.secondary_item_type === "By-Product")!.basic_rate
    );
  });

  test("Gap 2 — missing equivalence blocks silent 1:1", async ({ page, loginPage, stockEntryPage }) => {
    const ctx = prepare("gap2_missing_equivalence");
    expect(ctx.blocked).toBeTruthy();
    expect(ctx.mentions_no_1_to_1 || (ctx.expected_error || "").includes("1:1")).toBeTruthy();
    await loginWithDevSid(page);
    await stockEntryPage.open(ctx.stock_entry);
    await captureStep(page, "v533_missing_eq_open");
    const message = await saveDraft(page);
    await captureStep(page, "v533_missing_eq_save");
    const combined = `${message || ""} ${ctx.expected_error || ""}`;
    expect(combined).toMatch(/equivalent|UOM|1:1/i);
  });
});

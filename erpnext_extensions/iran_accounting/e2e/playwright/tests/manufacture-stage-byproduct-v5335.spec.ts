import { test, expect } from "../src/fixtures/erpnext.fixture";
import { erpnextConfig } from "../src/fixtures/erpnext.fixture";
import { captureStep } from "../src/utils/screenshots";
import { benchExecute } from "../src/utils/frappe-api";

type DraftCtx = {
  name: string;
  contract_version?: string;
  value_difference?: number;
  fg?: Record<string, number | string>;
  by_product?: Record<string, number | string>;
};

function baseline() {
  return benchExecute("erpnext_extensions.iran_accounting.e2e_v5335.baseline_counts") as Record<
    string,
    number | string
  >;
}

async function loginWithDevSid(page: import("@playwright/test").Page) {
  const minted = benchExecute("erpnext_extensions.iran_accounting.e2e_v5335.mint_dev_sid") as {
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
        cur_frm: { doc: { items?: Array<Record<string, unknown>> } & Record<string, unknown> };
      }
    ).cur_frm;
    const items = (frm.doc.items || []).map((row) => ({
      item_code: String(row.item_code || ""),
      basic_rate: Number(row.basic_rate || 0),
      basic_amount: Number(row.basic_amount || 0),
      additional_cost: Number(row.additional_cost || 0),
      secondary_item_type: String(row.secondary_item_type || ""),
      is_finished_item: Number(row.is_finished_item || 0),
      custom_equivalent_qty: Number(row.custom_equivalent_qty || 0),
      custom_physical_conversion: Number(row.custom_physical_conversion || 0),
      allow_zero_valuation_rate: Number(row.allow_zero_valuation_rate || 0),
    }));
    return {
      name: String(frm.doc.name),
      docstatus: Number(frm.doc.docstatus),
      contract: String(frm.doc.custom_manufacturing_costing_contract_version || ""),
      value_difference: Number(frm.doc.value_difference || 0),
      items,
    };
  });
}

test.describe("Iran Accounting v5.3.35 stage-equivalent By-Product @release-blocking", () => {
  test("P01–P08 — PO-JOB07352 Make Stock Entry Draft prices By-Product via stage allocator", async ({
    page,
    stockEntryPage,
  }) => {
    const before = baseline();
    let draftName = "";
    try {
      // P01 open Job Card
      await loginWithDevSid(page);
      await page.goto(`/desk/job-card/PO-JOB07352`);
      await page.waitForLoadState("domcontentloaded");
      await captureStep(page, "v5335_p01_job_card");

      // P02–P04: server path identical to Desk Make Stock Entry / Manufacture / Draft
      const draft = benchExecute(
        "erpnext_extensions.iran_accounting.e2e_v5335.create_canary_draft_from_job_card",
        { auto_submit: 0 }
      ) as DraftCtx;
      draftName = String(draft.name || "");
      expect(draftName).toBeTruthy();
      expect(draft.contract_version).toBe("5.3.43");
      expect(Number(draft.by_product?.basic_rate || 0)).toBeGreaterThan(0);
      expect(Number(draft.fg?.basic_rate || 0)).toBeGreaterThan(0);
      // UOM equivalence: 1 BOX = 2 syringe → FG rate ≈ 2 × BY rate (IRR residual ≤ 1)
      const fgRate = Number(draft.fg?.basic_rate || 0);
      const byRate = Number(draft.by_product?.basic_rate || 0);
      expect(Math.abs(fgRate - byRate * 2)).toBeLessThanOrEqual(1);
      expect(Number(draft.value_difference || 0)).toBe(0);

      await stockEntryPage.open(draftName);
      await captureStep(page, "v5335_p04_draft_open");
      const ui = await readEconomics(page);
      expect(ui.docstatus).toBe(0);
      expect(ui.contract).toBe("5.3.43");
      const by = ui.items.find((r) => r.item_code === "30500006");
      const fg = ui.items.find((r) => r.is_finished_item === 1 && r.item_code === "20100064");
      expect(by?.basic_rate || 0).toBeGreaterThan(0);
      expect(fg?.basic_rate || 0).toBeGreaterThan(0);
      expect(Math.abs((fg?.basic_rate || 0) - (by?.basic_rate || 0) * 2)).toBeLessThanOrEqual(1);
      expect(by?.allow_zero_valuation_rate || 0).toBe(0);
      expect(ui.value_difference).toBe(0);
      await captureStep(page, "v5335_p06_rates");
    } finally {
      if (draftName) {
        benchExecute("erpnext_extensions.iran_accounting.e2e_v5335.cleanup_canary_draft", {
          name: draftName,
        });
      }
      const after = baseline();
      expect(after.stock_entry).toBe(before.stock_entry);
      expect(after.sle).toBe(before.sle);
      expect(after.gl).toBe(before.gl);
    }
  });
});

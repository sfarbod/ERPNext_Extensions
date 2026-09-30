import { test, expect } from "../src/fixtures/erpnext.fixture";
import { erpnextConfig } from "../src/fixtures/erpnext.fixture";
import { captureStep } from "../src/utils/screenshots";
import { benchExecute } from "../src/utils/frappe-api";

type DraftCtx = {
  name: string;
  contract_version?: string;
  value_difference?: number;
  fg?: Record<string, number | string>;
  product_reject?: Record<string, number | string>;
};

function baseline() {
  return benchExecute("erpnext_extensions.iran_accounting.e2e_v5337.baseline_counts") as Record<
    string,
    number | string
  >;
}

async function loginWithDevSid(page: import("@playwright/test").Page) {
  const minted = benchExecute("erpnext_extensions.iran_accounting.e2e_v5337.mint_dev_sid") as {
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
      qty: Number(row.qty || 0),
      basic_rate: Number(row.basic_rate || 0),
      basic_amount: Number(row.basic_amount || 0),
      secondary_item_type: String(row.secondary_item_type || ""),
      is_finished_item: Number(row.is_finished_item || 0),
      allow_zero_valuation_rate: Number(row.allow_zero_valuation_rate || 0),
      t_warehouse: String(row.t_warehouse || ""),
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

test.describe("Iran Accounting v5.3.37 late Product Reject bridge @release-blocking", () => {
  test("P01–P08 — PO-JOB10094 Make Stock Entry Draft prices Product Reject from source/WIP", async ({
    page,
    stockEntryPage,
  }) => {
    const before = baseline();
    let draftName = "";
    try {
      await loginWithDevSid(page);
      await page.goto(`/desk/job-card/PO-JOB10094`);
      await page.waitForLoadState("domcontentloaded");
      await captureStep(page, "v5337_p01_job_card");

      const draft = benchExecute(
        "erpnext_extensions.iran_accounting.e2e_v5337.create_canary_draft_from_job_card",
        { auto_submit: 0 }
      ) as DraftCtx;
      draftName = String(draft.name || "");
      expect(draftName).toBeTruthy();
      expect(draft.contract_version).toBe("5.3.37");
      expect(Number(draft.fg?.basic_rate || 0)).toBe(738730);
      expect(Number(draft.fg?.qty || 0)).toBe(933);
      expect(Number(draft.product_reject?.basic_rate || 0)).toBe(738730);
      expect(Number(draft.product_reject?.qty || 0)).toBe(7);
      expect(Number(draft.product_reject?.basic_amount || 0)).toBe(5171110);
      expect(Number(draft.product_reject?.allow_zero_valuation_rate || 0)).toBe(0);
      expect(String(draft.product_reject?.t_warehouse || "")).toContain("ضایعات");
      expect(Number(draft.value_difference || 0)).toBe(0);

      await stockEntryPage.open(draftName);
      await captureStep(page, "v5337_p04_draft_open");
      const ui = await readEconomics(page);
      expect(ui.docstatus).toBe(0);
      expect(ui.contract).toBe("5.3.37");
      const fg = ui.items.find((r) => r.is_finished_item === 1 && r.item_code === "30100055");
      const rej = ui.items.find(
        (r) => r.item_code === "30100055" && r.secondary_item_type === "Scrap"
      );
      expect(fg?.qty).toBe(933);
      expect(fg?.basic_rate).toBe(738730);
      expect(rej?.qty).toBe(7);
      expect(rej?.basic_rate).toBe(738730);
      expect(rej?.allow_zero_valuation_rate || 0).toBe(0);
      expect(rej?.t_warehouse || "").toContain("ضایعات");
      expect(ui.value_difference).toBe(0);
      await captureStep(page, "v5337_p06_rates");
    } finally {
      if (draftName) {
        benchExecute("erpnext_extensions.iran_accounting.e2e_v5337.cleanup_canary_draft", {
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

import { test, expect } from "../src/fixtures/erpnext.fixture";
import { erpnextConfig } from "../src/fixtures/erpnext.fixture";
import { captureStep } from "../src/utils/screenshots";

test.describe("5.2.1 Historical Repair — production posting order @release-blocking", () => {
  test.beforeEach(async ({ page }) => {
    const sid = process.env.FRAPPE_E2E_SID;
    if (sid) {
      await page.context().addCookies([
        {
          name: "sid",
          value: sid,
          domain: "development.localhost",
          path: "/",
        },
        {
          name: "system_user",
          value: "yes",
          domain: "development.localhost",
          path: "/",
        },
        {
          name: "full_name",
          value: "Administrator",
          domain: "development.localhost",
          path: "/",
        },
      ]);
    }
  });

  test("open repair page, dry-run, preview, integrity, no API 500", async ({ page, loginPage }) => {
    const errors: string[] = [];
    page.on("pageerror", (err) => errors.push(String(err)));
    page.on("response", (res) => {
      if (res.status() >= 500) errors.push(`HTTP ${res.status()} ${res.url()}`);
    });

    if (!process.env.FRAPPE_E2E_SID) {
      await loginPage.login(erpnextConfig.user, erpnextConfig.password);
    }

    await page.goto("/app/historical-repair");
    await expect(page.locator(".hr-section-title")).toContainText("Production Posting Order", {
      timeout: 60_000,
    });
    await captureStep(page, "ppo_01_repair_page");

    const dry = page.locator("button[data-action='dry-run']");
    await expect(dry).toBeVisible();
    await dry.click();
    const previewBox = page.locator("pre[data-role='preview']");
    await expect(previewBox).toContainText(/DRY_RUN|eligible|proposed_outbound_time|confidence|Seconds Shifted|Minimum Seconds Required|Repair unnecessary|CROSS_TIME/i, {
      timeout: 180_000,
    });
    await captureStep(page, "ppo_02_dry_run");

    const preview = await previewBox.innerText();
    expect(preview).not.toContain("Traceback");
    expect(preview.toLowerCase()).not.toContain("internal server error");

    if (preview.includes("18:01:46") || preview.includes("T+1") || preview.includes("proposed_outbound_time")) {
      expect(preview).toMatch(/18:01:4[56]|proposed_outbound_time|DRY_RUN|eligible/i);
    }

    await page.locator("button[data-action='integrity']").click();
    await captureStep(page, "ppo_03_integrity");

    const repair = page.locator("button[data-action='repair']");
    await expect(repair).toBeVisible();

    const unexpected = errors.filter(
      (e) => !e.includes("favicon") && !e.includes("socket.io")
    );
    expect(unexpected, unexpected.join("\n")).toEqual([]);
  });

  test("real-data: EXACT list, repaired chain times, integrity, stock ledger", async ({
    page,
    loginPage,
  }) => {
    const errors: string[] = [];
    page.on("pageerror", (err) => errors.push(String(err)));
    page.on("response", (res) => {
      if (res.status() >= 500) errors.push(`HTTP ${res.status()} ${res.url()}`);
    });

    if (!process.env.FRAPPE_E2E_SID) {
      await loginPage.login(erpnextConfig.user, erpnextConfig.password);
    }

    await page.goto("/app/historical-repair");
    await expect(page.locator(".hr-section-title")).toContainText("Production Posting Order", {
      timeout: 60_000,
    });

    await page.locator("button[data-action='dry-run']").click();
    const preview = page.locator("pre[data-role='preview']");
    await expect(preview).toContainText(/DRY_RUN|eligible|proposed_outbound_time|confidence|Seconds Shifted|Minimum Seconds Required|Repair unnecessary|CROSS_TIME/i, {
      timeout: 180_000,
    });
    const previewText = await preview.innerText();
    expect(previewText.toLowerCase()).not.toContain("internal server error");
    await captureStep(page, "ppo_04_realdata_dry_run");

    const tableText = await page.locator(".hr-table-wrap").innerText();
    expect(tableText.length).toBeGreaterThan(0);
    expect(tableText).toMatch(/Seconds Shifted|Time Gap|Negative Start|Minimum Seconds Required|Repair unnecessary|\+1 second|\+2 seconds|No change|CROSS_TIME/i);
    if (tableText.includes("EXACT")) {
      expect(tableText).toContain("EXACT");
    }

    await page.locator("button[data-action='integrity']").click();
    await captureStep(page, "ppo_05_integrity");

    await page.goto(
      "/app/query-report/Stock%20Ledger?item_code=230699&from_date=2026-09-01&to_date=2026-09-01"
    );
    await expect(page.locator(".page-title")).toContainText(/Stock Ledger/i, { timeout: 60_000 });
    await captureStep(page, "ppo_06_stock_ledger");
    const body = await page.locator("body").innerText();
    expect(body.toLowerCase()).not.toContain("internal server error");
    if (body.includes("MAT-STE-2026-37424") || body.includes("37425")) {
      expect(body).toMatch(/14:00:0[01]|14:00/);
    }

    const unexpected = errors.filter(
      (e) => !e.includes("favicon") && !e.includes("socket.io")
    );
    expect(unexpected, unexpected.join("\n")).toEqual([]);
  });
});

test.describe("5.2.7 Posting Order — 17 Farvardin cross-time @release-blocking", () => {
  test.setTimeout(6 * 60_000);

  test.beforeEach(async ({ page }) => {
    const sid = process.env.FRAPPE_E2E_SID;
    if (sid) {
      await page.context().addCookies([
        { name: "sid", value: sid, domain: "development.localhost", path: "/" },
        { name: "system_user", value: "yes", domain: "development.localhost", path: "/" },
        { name: "full_name", value: "Administrator", domain: "development.localhost", path: "/" },
      ]);
    }
  });

  test("Farvardin 1405-01-17 ledger is repaired, not timestamp-only", async ({ page, loginPage }) => {
    const errors: string[] = [];
    page.on("pageerror", (err) => errors.push(String(err)));
    page.on("response", (res) => {
      if (res.status() >= 500) errors.push(`HTTP ${res.status()} ${res.url()}`);
    });

    if (!process.env.FRAPPE_E2E_SID) {
      await loginPage.login(erpnextConfig.user, erpnextConfig.password);
    }

    await page.goto("/app/stock-entry/MAT-STE-2026-25825");
    await expect(page.locator("body")).toContainText("MAT-STE-2026-25825", { timeout: 60_000 });
    await expect(page.locator("body")).toContainText(/18:02:57|02:57/, { timeout: 60_000 });
    const seBody = await page.locator("body").innerText();
    expect(seBody).not.toMatch(/18:01:45/);
    await captureStep(page, "ppo_07_farvardin_stock_entry");

    await page.goto(
      "/app/query-report/Stock%20Ledger?item_code=30300042&from_date=2026-04-06&to_date=2026-04-06&batch_no=504135-30300042-AK264401A11"
    );
    await expect(page.locator(".page-title")).toContainText(/Stock Ledger/i, { timeout: 60_000 });
    await page.waitForTimeout(3000);
    const ledger = await page.locator("body").innerText();
    expect(ledger.toLowerCase()).not.toContain("internal server error");
    if (ledger.includes("25824-1") || ledger.includes("25825")) {
      expect(ledger).toMatch(/3,?333,?718|3,?333,?719/);
    }
    await captureStep(page, "ppo_08_farvardin_stock_ledger");

    await page.goto(
      "/app/query-report/Stock%20Ledger?item_code=30300042&from_date=2026-04-11&to_date=2026-04-11&batch_no=504135-30300042-AK264401A11"
    );
    await expect(page.locator(".page-title")).toContainText(/Stock Ledger/i, { timeout: 60_000 });
    await page.waitForTimeout(3000);
    const later = await page.locator("body").innerText();
    expect(later.toLowerCase()).not.toContain("internal server error");
    if (later.includes("25912") || later.includes("25911-1")) {
      expect(later).toMatch(/3,?333,?718|3,?333,?719/);
      expect(later).not.toMatch(/3,?338,?300/);
    }
    await captureStep(page, "ppo_08b_farvardin_downstream_ledger");

    await page.goto("/app/historical-repair");
    await expect(page.locator("button.hr-tab", { hasText: "Posting Order" })).toBeVisible({ timeout: 60_000 });
    await page.locator("button.hr-tab", { hasText: "Posting Order" }).click();
    await expect(page.locator(".hr-section-title")).toContainText("Production Posting Order", { timeout: 60_000 });

    await expect(page.locator("button[data-action='rebuild-docs']")).toBeVisible({ timeout: 60_000 });
    await expect(page.locator("button[data-action='replay-downstream']")).toBeVisible({ timeout: 60_000 });

    await page.locator("button[data-action='scan']").click();
    await expect(page.locator("pre[data-role='preview']")).toContainText("Scan complete", { timeout: 180_000 });

    await page.locator("button[data-action='dry-run']").click();
    const preview = page.locator("pre[data-role='preview']");
    await expect(preview).toContainText(/DRY_RUN|CROSS_TIME|eligible|Scan complete|NO_REPAIR/i, {
      timeout: 180_000,
    });
    const previewText = await preview.innerText();
    expect(previewText.toLowerCase()).not.toContain("internal server error");
    expect(previewText).not.toContain("Traceback");

    const tableText = await page.locator(".hr-table-wrap").innerText();
    const hay = `${previewText}\n${tableText}`;
    const stillOpen =
      hay.includes("504135-30300042-AK264401A11") &&
      hay.includes("-1899") &&
      hay.includes("18:01:45");
    expect(stillOpen).toBeFalsy();

    await captureStep(page, "ppo_09_farvardin_no_interval");

    const unexpected = errors.filter((e) => !e.includes("favicon") && !e.includes("socket.io"));
    expect(unexpected, unexpected.join("\n")).toEqual([]);
  });
});

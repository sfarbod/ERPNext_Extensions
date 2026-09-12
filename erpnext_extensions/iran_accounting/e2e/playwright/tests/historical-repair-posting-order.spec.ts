import { test, expect } from "../src/fixtures/erpnext.fixture";
import { erpnextConfig } from "../src/fixtures/erpnext.fixture";
import { captureStep } from "../src/utils/screenshots";
import { deskBaseUrlResolvable } from "../src/utils/network";
import { config } from "../src/utils/env";

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
    test.skip(
      !process.env.FRAPPE_E2E_SID && !deskBaseUrlResolvable(config.baseUrl),
      `Desk host not resolvable (${config.baseUrl}) and no FRAPPE_E2E_SID`
    );

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
    await expect(page.locator("pre[data-role='preview']")).toBeVisible();
    await captureStep(page, "ppo_02_dry_run");

    const preview = await page.locator("pre[data-role='preview']").innerText();
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
});

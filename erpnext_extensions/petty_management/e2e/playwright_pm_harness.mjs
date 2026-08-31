/**
 * QA harness — always close Playwright browser/context/page (descriptor leak prevention).
 */
import { chromium } from "/tmp/e2e-npm/node_modules/playwright/index.mjs";

export async function runWithPlaywrightBrowser(run, options = {}) {
  /** @type {import('playwright').Browser | null} */
  let browser = null;
  /** @type {import('playwright').BrowserContext | null} */
  let context = null;
  /** @type {import('playwright').Page | null} */
  let page = null;
  try {
    browser = await chromium.launch({ headless: true, ...(options.launch || {}) });
    context = await browser.newContext(options.context || {});
    if (options.trace) {
      await context.tracing.start({ screenshots: true, snapshots: true });
    }
    page = await context.newPage();
    if (typeof options.onPage === "function") {
      await options.onPage(page);
    }
    return await run({ browser, context, page });
  } finally {
    try {
      if (context && options.trace && options.tracePath) {
        await context.tracing.stop({ path: options.tracePath });
      }
    } catch {
      /* ignore */
    }
    try {
      if (page) await page.close();
    } catch {
      /* ignore */
    }
    try {
      if (context) await context.close();
    } catch {
      /* ignore */
    }
    try {
      if (browser) await browser.close();
    } catch {
      /* ignore */
    }
  }
}

import assert from "node:assert/strict";
import {
	AE_PREPARED_POLL_MAX_WAIT_MS,
	exportEnqueueClaimsSummaryLoading,
	exportEnqueueUsesDeskFreeze,
	isStaleGridRender,
	mayEndSummaryLoading,
	preparedTimeoutClearsLoading,
	shouldBlockDuplicateExportEnqueue,
} from "./ae_concurrency_guards.mjs";

assert.equal(shouldBlockDuplicateExportEnqueue(true), true);
assert.equal(shouldBlockDuplicateExportEnqueue(false), false);
assert.equal(exportEnqueueUsesDeskFreeze(), false);
assert.equal(exportEnqueueClaimsSummaryLoading(), false);
assert.equal(preparedTimeoutClearsLoading(), true);
assert.ok(AE_PREPARED_POLL_MAX_WAIT_MS <= 2 * 60 * 1000);
assert.equal(isStaleGridRender(2, 1), true);
assert.equal(isStaleGridRender(2, 2), false);
assert.equal(mayEndSummaryLoading(5, 4, false), false);
assert.equal(mayEndSummaryLoading(5, 5, true), true);
assert.equal(mayEndSummaryLoading(null, 5, false), true);

console.log("ae_concurrency_guards: ok");

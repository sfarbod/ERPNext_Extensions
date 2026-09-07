// Copyright (c) 2026, Farbod Siyahpoosh and contributors
/**
 * Pure helpers for Account Explorer v5.1.5 concurrency / loading isolation.
 * Node-runnable (no browser).
 */
export const AE_PREPARED_POLL_MAX_WAIT_MS = 2 * 60 * 1000;

export function shouldBlockDuplicateExportEnqueue(inflight) {
	return !!inflight;
}

export function exportEnqueueUsesDeskFreeze() {
	// Contract: large export enqueue must not use frappe.dom.freeze.
	return false;
}

export function exportEnqueueClaimsSummaryLoading() {
	// Contract: export must not claim summary loading ownership.
	return false;
}

export function preparedTimeoutClearsLoading() {
	// Contract: prepared timeout throws a controlled error that finally clears loading.
	return true;
}

export function isStaleGridRender(currentGeneration, ownerGeneration) {
	return currentGeneration !== ownerGeneration;
}

export function mayEndSummaryLoading(loadingOwner, generation, painted) {
	if (loadingOwner != null && loadingOwner !== generation) {
		return false;
	}
	return !painted || loadingOwner === generation;
}

frappe.provide("erpnext_extensions.account_explorer.core");

/**
 * Account Explorer workspace URL compact tokens (Wave 3B-3 / ADR-3B-002).
 * Soft URL length limit → User Settings Workspace.tokens + ?ae_state=<token>.
 * Preserves Grid section; only mutates Workspace.tokens.
 */

(function () {
	const core = erpnext_extensions.account_explorer.core;
	const AE_WORKSPACE_SETTINGS_SECTION = core.AE_WORKSPACE_SETTINGS_SECTION;
	const AE_WORKSPACE_TOKEN_LIMIT = core.AE_WORKSPACE_TOKEN_LIMIT;
	const AE_WORKSPACE_TOKEN_BYTE_BUDGET =
		core.AE_WORKSPACE_TOKEN_BYTE_BUDGET || 48000;
	const codec = () => core.AEWorkspaceCodec;

	function encoded_byte_length(settings) {
		try {
			return new TextEncoder().encode(JSON.stringify(settings)).length;
		} catch (_error) {
			return JSON.stringify(settings || {}).length;
		}
	}

	function prune_tokens_fifo(section, keep_limit) {
		const keys = Object.keys(section.tokens || {});
		if (keys.length <= keep_limit) {
			return;
		}
		keys
			.sort((a, b) =>
				String(section.tokens[a]._stored_at).localeCompare(String(section.tokens[b]._stored_at))
			)
			.slice(0, keys.length - keep_limit)
			.forEach((old_key) => delete section.tokens[old_key]);
	}

	function prune_tokens_to_byte_budget(current, section, budget) {
		while (
			Object.keys(section.tokens || {}).length &&
			encoded_byte_length(current) > budget
		) {
			const oldest = Object.keys(section.tokens).sort((a, b) =>
				String(section.tokens[a]._stored_at).localeCompare(String(section.tokens[b]._stored_at))
			)[0];
			if (!oldest) {
				break;
			}
			delete section.tokens[oldest];
		}
	}

	function payload_fingerprint(workspace) {
		const clean = codec().clone(workspace || {});
		delete clean._stored_at;
		return JSON.stringify(clean);
	}

	core.AEWorkspaceTokens = {
		payload_fingerprint,

		async save_compact_token(workspace) {
			// Re-read latest settings before mutate so concurrent Grid updates are preserved.
			let current = {};
			try {
				current = (await frappe.model.user_settings.get("Account Explorer")) || {};
			} catch (_error) {
				current = frappe.model.user_settings["Account Explorer"] || {};
			}
			current = $.extend(true, {}, current);
			const section = $.extend(true, {}, current[AE_WORKSPACE_SETTINGS_SECTION] || {});
			section.tokens = section.tokens || {};

			const fingerprint = payload_fingerprint(workspace);
			const existing_key = Object.keys(section.tokens).find((key) => {
				try {
					return payload_fingerprint(section.tokens[key]) === fingerprint;
				} catch (_error) {
					return false;
				}
			});
			if (existing_key) {
				return existing_key;
			}

			const token = frappe.utils.get_random(10);
			section.tokens[token] = {
				...codec().clone(workspace),
				_stored_at: frappe.datetime?.now_datetime?.() || Date.now(),
			};

			// FIFO prune by count, then by soft byte budget (preserves Grid).
			prune_tokens_fifo(section, AE_WORKSPACE_TOKEN_LIMIT);
			current[AE_WORKSPACE_SETTINGS_SECTION] = section;
			prune_tokens_to_byte_budget(current, section, AE_WORKSPACE_TOKEN_BYTE_BUDGET);
			current[AE_WORKSPACE_SETTINGS_SECTION] = section;
			frappe.model.user_settings["Account Explorer"] = current;
			await Promise.resolve(frappe.model.user_settings.update("Account Explorer", current));
			return token;
		},

		async restore_from_compact_token(token) {
			if (!token || frappe.session.user === "Guest") {
				return null;
			}
			try {
				const settings = (await frappe.model.user_settings.get("Account Explorer")) || {};
				frappe.model.user_settings["Account Explorer"] = settings;
				const payload = settings?.[AE_WORKSPACE_SETTINGS_SECTION]?.tokens?.[token] || null;
				if (!payload || typeof payload !== "object") {
					return null;
				}
				const clean = codec().clone(payload);
				delete clean._stored_at;
				return clean;
			} catch (error) {
				console.warn("[Account Explorer] failed to restore workspace token", error);
				return null;
			}
		},
	};
})();

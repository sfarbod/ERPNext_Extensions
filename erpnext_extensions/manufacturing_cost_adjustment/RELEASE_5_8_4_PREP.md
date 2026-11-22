# erpnext_extensions 5.8.4 — preparation (not released)

**Status:** draft / not bumped / not tagged.  
**Current `__version__`:** `5.8.3`  
**Target when authorized:** `5.8.4`

## Intended scope

1. MCA Iran bootstrap + remirror integerize (SLE/SABE fractional-rate hole).
2. Queue split: `process_mca` → `mca`; RIV execute → `long`.
3. Write-limit raise; Failed-RIV remirror; permanent `negative pool_value` skip.
4. Quarantine `MCA-2026-00004` (operational exclusion).
5. Worker health probe + deployment docs (`WORKER.md`, ensure scripts).

## Must update on bump (when authorized)

- `erpnext_extensions/__init__.py` → `__version__ = "5.8.4"`
- Create `RELEASE_5_8_4.md` from this prep (SCR notes style of 5.8.3)
- Confirm no intermediate versions; follow existing `RELEASE_5_8_*.md` convention

## Blockers before bump/commit/release

- [ ] Explicit authorization to commit
- [ ] MCA-00004 remains quarantined (or recovered under written plan)
- [ ] Non-mutating unit gates green
- [ ] Processing pause respected (no mass run)
- [ ] Host `common_site_config.json` / bench scripts documented for ops (out-of-app)

## Out of scope for 5.8.4

- Mass processing of 1405-01..06 MCAs
- Production deploy / tag / push from agent
- Mutating integration/accounting E2E suites as release gates

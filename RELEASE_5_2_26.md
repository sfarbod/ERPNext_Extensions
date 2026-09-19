# Release 5.2.26 — Harden RIV declared-target resolution + durable anomaly audit

## Follow-up to 5.2.25

### Declared-target fix

`get_riv_target_item_codes` must **not** treat ERPNext’s expanding `items_to_be_repost` list (or the current `args.item_code` under dependant expansion) as blocking scope. Those fields become the multi-hop dependant closure and would incorrectly re-classify unrelated FG items (e.g. `20100064`) as in-scope.

For Item-and-Warehouse RIV, declared target is solely `Repost Item Valuation.item_code`.

### Durable out-of-scope audit

`repost()` rolls back the open DB transaction when a later GL step fails. Out-of-scope anomalies are now written to:

- `sites/<site>/logs/iran_riv_out_of_scope.log` (survives rollback)
- Error Log, with an immediate `db.commit()` so the desk row also survives

### Tests

Added `test_dependant_args_item_code_does_not_expand_targets`.

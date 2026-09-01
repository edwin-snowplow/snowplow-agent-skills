# Troubleshooting — Athena Iceberg failed-events recovery

Failure modes seen (or anticipated from Athena/Trino semantics) during recovery, in **symptom →
likely cause → verify → fix** form. Each phase of `SKILL.md` points here when the relevant step can go
wrong.

---

## Test SELECT / INSERT errors: cast or coercion failure

**Symptom.** The Phase 4 test SELECT (or the INSERT) fails with a cast error like `Cannot cast
'abc' to bigint` on a recovered field.

**Likely cause.** A value in the payload genuinely doesn't convert to the target type — so the failed
rows don't all share one recovery — or you built the recovery expression before running the Phase 3.5
profiling that would have caught it.

**Verify.** Run the **field profiling query** (`templates/recovery-sql.md`) for that field with its
target type in the `TRY_CAST`; a non-zero `_uncoercible` count confirms un-castable values exist.

**Fix.** Profiling is the gate that prevents this. If `_uncoercible > 0`, **partition the offending
rows off** (list them, decide scope with the user) and recover only the coercible population — don't
force the cast. Do **not** switch the recovery expression to `TRY_CAST` to dodge the error: that hides
un-castable values as silent NULLs, writing an incomplete recovery. `TRY_CAST` belongs only in the
profiling query.

---

## `typeof()` doesn't match the good-table column type

**Symptom.** In the Phase 4 test output, `recovered_type` differs from the good-table column's type —
a different field name, a different type, or a reordered field — even though the query ran.

**Likely cause.** The reconstructed `ROW(...)` doesn't match the good-table struct field-for-field:
most often a reordered field, a missing `_schema_version` first field, or a field cast to the wrong
Athena type.

**Verify.** Put the recovery expression's `AS ARRAY(ROW(...))` field list side by side with the
good-table column's type from `DESCRIBE` (after Hive→Athena conversion) and check name, type, and
position of each.

**Fix.** Rebuild the `CAST(ARRAY[ROW(...)] AS ARRAY(ROW(...)))` to match the DESCRIBE field-for-field,
in order, with `_schema_version` first. Re-run the test SELECT until `typeof()` matches. Athena inserts
positionally into a struct, so order is load-bearing.

---

## Athena rejects the ROW(...) type: reserved word as a field name

**Symptom.** The recovery expression (or a `CAST(NULL AS row(...))` in the generated INSERT) fails to
parse, complaining about a field name like `timestamp`, `type`, `value`, `all`, `position`, or
`current_time`.

**Likely cause.** A struct field name equals an Athena reserved word and isn't quoted, so Athena
rejects the type definition.

**Verify.** Check the field names in the `ROW(...)` against the reserved-word list in
`reference/column-classification-and-validation.md`.

**Fix.** Double-quote the field name inside the `ROW(...)` type, e.g. `row("timestamp" varchar, ...)`,
`row("value" varchar, ...)`. Confirmed real cases: `"current_time"`, `"timestamp"`, `"all"`, `"type"`,
`"value"`, `"position"` (see the reference for which schemas). The bundled generator quotes these
automatically.

---

## The generated INSERT is missing the recovery expression (recovery column passed through instead)

**Symptom.** In the INSERT that `scripts/generate_insert.py` wrote, the entity column you're repairing
appears as a bare pass-through column (or `CAST(NULL AS ...)`), not your
`CAST(ARRAY[ROW(...)] AS ARRAY(ROW(...)))` expression. The script also prints
`WARNING: recovery column '<name>' not found in good DESCRIBE.` on stderr when this happens.

**Likely cause.** The `RECOVERY_COLUMN` in `config.json` doesn't match the good table's actual column
name — casing, the wrong major-version suffix, or a name derived from the Iglu URI instead of read from
the good-table `DESCRIBE`.

**Verify.** Re-read the good-table `DESCRIBE` and compare the real `column_name` to the `RECOVERY_COLUMN`
value you set.

**Fix.** Set `RECOVERY_COLUMN` to the exact column name from the `DESCRIBE` and re-run the script (it
matches on `lower(...)`, so case aside it must be the same string). If several major versions came back,
pick the one whose trailing `_<major>` matches the schema version being recovered.

---

## Unexpected NULLs in the reconstructed column (query ran, but values are null)

**Symptom.** The test SELECT shows a reconstructed field is NULL where a value was expected.

**Likely cause.** (a) A wrong JSON path — `$.<field>` doesn't match the actual key (typo, casing,
nested deeper); or (b) the key was genuinely **absent** in those rows (profiling's `_absent` count).
If you see NULLs from a *coercion* failure, someone used `TRY_CAST` in the recovery expression — switch
it back to `CAST(...)`, which errors instead of hiding it.

**Verify.** Run the payload-inspection query (`json_extract_scalar`) and re-check the profiling output
(`_absent`) for that field.

**Fix.** For (a) correct the `$.<field>` path to the actual key. For (b) the field is missing in some
rows: if the schema requires it, partition those rows off; if optional, the NULL is expected. Re-run
the test SELECT and re-check the gate in `reference/column-classification-and-validation.md`.

---

## A payload field is rejected as `additionalProperties` / isn't in the target struct

**Symptom.** The drill-in `errors` show an `additionalProperties` violation — the event carries a
field the schema doesn't recognise (e.g. the payload sends `numbers` but the target struct has no
`numbers`).

**Likely cause.** The schema defines the field under a *different name* (a typo or rename in the
schema itself — e.g. the DESCRIBE shows `nubmers`, not `numbers`), or the field genuinely isn't in the
schema.

**Verify.** Compare the payload keys (payload-inspection query) against the good-table struct field
names (from `DESCRIBE`).

**Fix.** First **ask the user** what should happen to the stray field — dropped, or mapped onto a
field the schema does define. That's their call, not yours; see *Recovering an `additionalProperties`
failure* in the SKILL.md. Then apply it. If it's a renamed/misspelled schema field, **map the payload key onto the target field name**
in the recovery expression — e.g. `CAST(json_extract_scalar(failure_data, '$.numbers') AS BIGINT)` in
the `nubmers` position — the payload key and the struct field name don't have to match. If the field
truly isn't in the schema at all, it can't be recovered into a column that doesn't exist; the schema
must be evolved first (outside this flow).

---

## The failure is a ResolutionError, not a ValidationError

**Symptom.** The identification/drill-in shows the failure is a `ResolutionError` (Iglu couldn't
resolve the schema) rather than a data/field validation problem — and it's tempting to declare it
unrecoverable.

**Likely cause.** The schema referenced by the event couldn't be fetched from any Iglu registry:
a transient/misconfigured registry, a wrong URI (typo or nonexistent version), or a schema that was
never published.

**Verify.** Read the `errors` content to confirm it's a resolution (not validation) failure, and
establish the **correct** schema URI. Then check the good-table `DESCRIBE` for a target column under
the **corrected** entity name — its presence is your confirmation a column exists.

**Fix.** This is recoverable in-warehouse in most cases — follow "Recovering a ResolutionError" in
`SKILL.md`: confirm the correct schema now resolves and a target column exists; **filter** the failed
rows on the URI recorded in the failure context (`contexts_..._failure_1[1].schema` — the original,
possibly-wrong one) but **reconstruct** into the corrected URI's column with the corrected
`_schema_version`. Only give up if the schema can't be made to resolve at all or no target column can
exist.

---

## Duplicate pre-check returns > 0

**Symptom.** The duplicate pre-check finds `event_id`s already present in the good table.

**Likely cause.** This schema + app_id + window was (at least partly) recovered before — those events
only exist because they failed validation, so any match means a prior recovery.

**Verify.** Run the second duplicate-pre-check query to list the overlapping `event_id`s.

**Fix.** Decide with the user: stop, narrow the time window to exclude the already-recovered rows, or
proceed knowingly (accepting duplicates). Never run the INSERT blind after a non-zero pre-check.

---

## Promote step misaligns columns (staging path)

**Symptom.** `INSERT INTO <good> SELECT * FROM <staging>` errors on column count/type, or lands values
in the wrong columns.

**Likely cause.** The staging table wasn't created as an exact mirror of the good table, so `SELECT *`
column order doesn't line up.

**Verify.** Compare the two schemas with `DESCRIBE` (staging vs good), in order.

**Fix.** Recreate staging with the `CREATE TABLE <staging> ... AS SELECT * FROM <good> WHERE 1 = 0`
pattern (`templates/recovery-sql.md`), which clones exact columns/types/order, re-run the recovery
INSERT into it, then promote. `WHERE 1 = 0` is what makes the positional `SELECT *` promote safe.

---

## Row count landed ≠ expected failed-event count

**Symptom.** After the INSERT, the good/staging table has fewer (or more) recovered rows than the
identification query predicted.

**Likely cause.** A different time window (or predicate) between the identification query and the
INSERT/prep — so the recovered set doesn't match the identified set.

**Verify.** Confirm the `load_tstamp` window, `app_id`, and `schema` predicates are **identical**
across the identification, test, and INSERT queries (and, for the generator, the `LOAD_TSTAMP_*` /
`DAYS_LOOKBACK`, `APP_ID`, `FAILED_SCHEMA` config).

**Fix.** Align all queries to the same window/predicates (see the "Time window" note in
`templates/recovery-sql.md`) and re-run. One run recovers one schema for one app_id — split multiple
schemas/app_ids into separate runs.

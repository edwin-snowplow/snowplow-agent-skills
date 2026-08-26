# Troubleshooting — Databricks failed-events recovery

Failure modes seen (or anticipated from Databricks / Delta Lake semantics) during recovery, in
**symptom → likely cause → verify → fix** form. Each phase of `SKILL.md` points here when the
relevant step can go wrong.

---

## Unexpected NULLs in the reconstructed column (query ran, but values are null)

**Symptom.** The test SELECT or per-field null-count check shows a reconstructed field is NULL where a
value was expected — but the query itself ran without error.

**Likely cause.** This is the defining Databricks trap. Either (a) a wrong JSON path —
`GET_JSON_OBJECT(data, '$.<field>')` doesn't match the actual key (typo, casing, nested deeper), which
returns NULL rather than erroring — or (b) `CAST` returned NULL on an un-castable value in legacy
(non-ANSI) mode instead of erroring, or (c) the key was genuinely **absent** in those rows
(profiling's `_absent` count). "The query ran" proves none of this.

**Verify.** Run the payload-inspection query (`GET_JSON_OBJECT`) to see the real key, and re-check the
Phase 3.5 profiling output (`_absent`, `_uncoercible`) for that field.

**Fix.** For (a) correct the path to the actual key. For (b) profiling would have flagged
`_uncoercible > 0` — those values genuinely won't cast; partition them off. For (c) the field is
missing in some rows: if the schema requires it, partition those rows off; if optional, the NULL is
expected. Re-run the test SELECT and re-check the gate in `reference/strict-typing-and-classification.md`.

---

## The test SELECT "ran clean" but you're unsure the casts held (ANSI vs legacy)

**Symptom.** The test SELECT returned rows with no error, and you're tempted to treat that as proof.

**Likely cause.** On a legacy (non-ANSI) cluster, `CAST` returns NULL on a bad value rather than
erroring, and `GET_JSON_OBJECT` never errors on a bad path — so a clean run is **not** evidence the
recovery is correct. ANSI mode (`spark.sql.ansi.enabled = true`) is the default on Databricks SQL
warehouses and recent DBR, but not on every cluster.

**Verify.** Run the optional per-field null-count check (`templates/recovery-sql.md`); every required
field should return 0. Eyeball the actual `recovered` values against expectations.

**Fix.** Don't rely on "the query ran". Confirm coercibility with the Phase 3.5 profiling query
*before* building the expression, and validate NULLs *after*. Both are already in the flow precisely
because Databricks doesn't error the way Snowflake/BigQuery do.

---

## Struct field-name mismatch on the Delta INSERT

**Symptom.** The generated INSERT fails with a type/field mismatch on the recovery column even though
the test SELECT passed.

**Likely cause.** Delta resolves struct fields **by name**, so the reconstructed struct's field
**names** (and, for a clean positional INSERT, its types and order) must match the target column's
`full_data_type` exactly — most often a misnamed field, a reordered field, or a missing
`_schema_version` first field.

**Verify.** Put the recovery expression's fields side by side with the recovery column's
`full_data_type` (from the "Finding the recovery column name" query) and check name, type, and
position of each.

**Fix.** Rebuild the `ARRAY(NAMED_STRUCT(...))` to match `full_data_type` field-for-field, in order,
with `_schema_version` first. Re-run the test SELECT, then regenerate the INSERT.

---

## A payload field is rejected as `additionalProperties` / isn't in the target struct

**Symptom.** The drill-in `errors` show an `additionalProperties` violation — the event carries a
field the schema doesn't recognise (e.g. the payload sends `numbers` but the target struct has no
`numbers`).

**Likely cause.** The schema defines the field under a *different name* (a typo or rename in the
schema itself — e.g. the column `full_data_type` shows `nubmers`, not `numbers`), or the field
genuinely isn't in the schema.

**Verify.** Compare the payload keys (payload-inspection query) against the recovery column's
`full_data_type` field names (the "finding the recovery column name" query).

**Fix.** If it's a renamed/misspelled schema field, **map the payload key onto the target field name**
in the recovery expression — e.g.
`'nubmers', CAST(GET_JSON_OBJECT(...data, '$.numbers') AS BIGINT)` — the payload key (`$.numbers`) and
the struct field name (`nubmers`) don't have to match. If the field truly isn't in the schema at all,
it can't be recovered into a column that doesn't exist; the schema must be evolved first (outside this
flow).

---

## The in-warehouse generator's INSERT comes out with unquoted string literals (won't parse)

**Symptom.** You used the **in-warehouse** INSERT-generator (the fallback), ran it, and the emitted
statement fails to parse — string literals appear unquoted (`app_id = test-app` instead of
`app_id = 'test-app'`, `'1-0-0'` → `1-0-0`, `'$.field'` → `$.field`). The structure and newlines are
intact; only the single-quote characters are gone.

**Likely cause.** A single quote inside the generator's string literals was written as a doubled `''`
instead of a backslash escape (`\'`). Spark SQL has no `''` escape: `'a''b'` is read as two adjacent
literals that coalesce to `ab`, so every doubled quote silently disappears. This most often bites the
embedded recovery expression (its `'1-0-0'`, `'$.field'` quotes) and the `app_id` / schema-URI
predicates.

**Verify.** Look at the generator's `concat(...)` literals and the substituted `<recovery_expression>`:
any `''` is the culprit. A correctly escaped generator uses `\'` throughout.

**Fix.** Backslash-escape every single quote in the embedded expression and predicates (`\'`), then
re-run the generator. Better: switch to the **local script** (`scripts/generate_insert.py`, Phase 7
primary in `templates/recovery-sql.md`), which assembles the INSERT in Python with no string-literal
layer, so there is nothing to escape. Don't hand-restore quotes into an already-broken statement —
regenerate cleanly instead.

---

## The generated INSERT is missing the recovery expression (recovery column passed through instead)

**Symptom.** In the statement returned by the Phase 7 generator, the entity column you're repairing
appears as a bare column name (pass-through), not as your `ARRAY(NAMED_STRUCT(...))` expression.

**Likely cause.** The `<recovery_column>` you passed the generator doesn't match the good table's
actual column name (casing, wrong major-version suffix, or derived from the Iglu URI instead of
queried).

**Verify.** Re-run the "Finding the recovery column name" query (`templates/recovery-sql.md`) and
compare the real `column_name` to the value you substituted.

**Fix.** Use the exact `column_name` returned (the generator matches on `lower(...)`, so case aside it
must be the same string). Regenerate. If several major versions came back, pick the one whose trailing
`_<major>` matches the schema version being recovered.

---

## `CAST(NULL AS <full_data_type>)` on the failure column is rejected

**Symptom.** The generated INSERT errors on the failure column — Databricks won't accept a NULL cast
to the failure column's full nested `ARRAY<STRUCT<...>>` type.

**Likely cause.** The failure column's full nested type is unusual and the NULL cast is rejected.

**Verify.** Try the cast alone: `SELECT CAST(NULL AS <the failure column full_data_type>);`.

**Fix.** Fall back to a simplified null of a compatible shape — e.g.
`CAST(NULL AS ARRAY<STRUCT<schema:STRING, data:STRING>>)` — or give the INSERT an **explicit column
list** that omits the failure column entirely. Confirm this early on a first real run.

---

## The failure is a ResolutionError, not a ValidationError

**Symptom.** The identification/drill-in shows the failure is a `ResolutionError` (Iglu couldn't
resolve the schema) rather than a data/field validation problem — and it's tempting to declare it
unrecoverable.

**Likely cause.** The schema referenced by the event couldn't be fetched from any Iglu registry:
a transient/misconfigured registry, a wrong URI (typo or nonexistent version), or a schema that was
never published.

**Verify.** Read the `errors` content to confirm it's a resolution (not validation) failure, and
establish the **correct** schema URI. Then run the "finding the recovery column name" query
(`templates/recovery-sql.md`) against the **corrected** schema name to check a target column exists
in the good table.

**Fix.** This is recoverable in-warehouse in most cases — follow "Recovering a ResolutionError" in
`SKILL.md`: confirm the correct schema now resolves and a target column exists; **filter** the failed
rows on the URI recorded in the failure context (the original, possibly-wrong one) but **reconstruct**
into the corrected URI's column with the corrected `_schema_version`. Only give up if the schema can't
be made to resolve at all or no target column can exist.

---

## Duplicate pre-check returns > 0

**Symptom.** The duplicate pre-check finds `event_id`s already present in the good table.

**Likely cause.** This schema + app_id + window was (at least partly) recovered before — those events
only exist because they failed validation, so any match means a prior recovery.

**Verify.** Run the second duplicate-pre-check query to list the overlapping `event_id`s.

**Fix.** Decide with the user: stop, narrow the time window to exclude the already-recovered rows, or
proceed knowingly (accepting duplicates). Never run the INSERT blind after a non-zero pre-check.

---

## Staging column misalignment via CTAS (staging path)

**Symptom.** `INSERT INTO <good> SELECT * FROM <staging>` errors on column count/type, or lands values
in the wrong columns.

**Likely cause.** The staging table wasn't created as an exact mirror of the good table, so `SELECT *`
column order doesn't line up.

**Verify.** Compare the two schemas: `information_schema.columns` for staging vs good (ordinal order).

**Fix.** Recreate staging with `CREATE TABLE <staging> AS SELECT * FROM <good> LIMIT 0` (copies exact
columns/types/order; `CREATE TABLE <staging> LIKE <good>` is equivalent), re-run the recovery INSERT
into it, then promote. The `LIMIT 0` CTAS is what makes the positional `SELECT *` promote safe.

---

## Row count landed ≠ expected failed-event count

**Symptom.** After the INSERT, the good/staging table has fewer (or more) recovered rows than the
identification query predicted.

**Likely cause.** A different time window (or predicate) between the identification query and the
INSERT/prep — so the recovered set doesn't match the identified set.

**Verify.** Confirm the `load_tstamp` window, `app_id`, and `schema` predicates are **identical**
across the identification, test, and INSERT queries.

**Fix.** Align all queries to the same window/predicates (see the "Time window" note in
`templates/recovery-sql.md`) and re-run. One run recovers one schema for one app_id — split multiple
schemas/app_ids into separate runs.

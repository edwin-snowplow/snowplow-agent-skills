# Troubleshooting — BigQuery failed-events recovery

Failure modes seen (or anticipated from BigQuery semantics) during recovery, in **symptom → likely
cause → verify → fix** form. Each phase of `SKILL.md` points here when the relevant step can go wrong.

---

## The generated INSERT is missing the recovery expression (recovery column passed through instead)

**Symptom.** In the statement returned by the Phase 7 generator, the entity column you're repairing
appears as a bare column name (pass-through), not as your `[STRUCT(...)]` expression.

**Likely cause.** The `<recovery_column>` you passed the generator doesn't match the good table's
actual column name (casing, wrong major-version suffix, or derived from the Iglu URI instead of
queried).

**Verify.** Re-run the "Finding the recovery column name" query (`templates/recovery-sql.md`) and
compare the real `column_name` to the value you substituted.

**Fix.** Use the exact `column_name` returned (the generator matches on `LOWER(...)`, so case aside it
must be the same string). Regenerate. If several major versions came back, pick the one whose trailing
`_<major>` matches the schema version being recovered.

---

## `CAST(NULL AS <data_type>)` on the failure column is rejected

**Symptom.** The generated INSERT errors on the failure column, or the generator's `FORMAT('CAST(NULL
AS %s)...')` produces a type string BigQuery won't accept for a NULL cast (the failure column's full
`ARRAY<STRUCT<... data JSON ...>>` type is unusual).

**Likely cause.** BigQuery is refusing a NULL cast to the full nested failure type.

**Verify.** Try the cast alone: `SELECT CAST(NULL AS <the failure column data_type>);`.

**Fix.** Fall back to a simplified null of the same shape —
`CAST(NULL AS ARRAY<STRUCT<schema STRING, data JSON>>)` — or give the INSERT an **explicit column
list** that omits the failure column entirely (BigQuery leaves unlisted columns NULL). This is the
single highest-risk item on a first real run; confirm it early.

---

## Test SELECT errors: "The provided JSON input is not an integer" (or not a string/bool)

**Symptom.** The Phase 4 test SELECT fails with an error like `The provided JSON input is not an
integer; error in INT64 expression`.

**Likely cause.** The recovery expression used a **strict** JSON accessor (`INT64()`, `STRING()`,
`FLOAT64()`, `BOOL()`) on a value sent as a *different* JSON type — most often a number sent as a
string (`"5"`). The strict accessors require the value to already be that exact JSON type and error
otherwise. Since a wrong-type value is the single most common thing you're recovering, the strict
accessors are the wrong tool here.

**Verify.** Run the payload-inspection query (with `JSON_VALUE`, not `STRING()`) and look at how the
field is encoded — a value shown in quotes was a JSON string.

**Fix.** You skipped or ignored the **Phase 3.5 profiling** step, which exists precisely to choose the
right extractor before you suggest anything. Run the profiling query, then for that field use
**`CAST(JSON_VALUE(...) AS <TYPE>)`** (coerces the string-encoded/mixed value and still errors on a
genuinely bad one). Re-run the test SELECT — it will now run clean because profiling confirmed every
value coerces. Do **not** switch to the lenient `LAX_` accessors to dodge the error: they'd hide any
truly un-coercible value as a silent NULL, writing an incomplete recovery. If profiling shows some
values genuinely won't coerce (`_uncoercible > 0`), partition those rows off instead.

---

## Unexpected NULLs in the reconstructed column (query ran, but values are null)

**Symptom.** The test SELECT / null-count check shows a reconstructed field is NULL where a value was
expected.

**Likely cause.** With strict/`CAST` extractors a NULL means (a) a wrong JSON path — `data.<field>`
doesn't match the actual key (typo, casing, nested deeper); (b) the key was genuinely **absent** in
those rows (profiling's `_absent` count); or (c) the field was present but explicitly JSON `null`
(profiling's `_json_null` count), which `CAST(JSON_VALUE(...))` turns into a SQL NULL. A bad *value*
would have errored, not NULLed, so it isn't that. (If you see NULLs from a coercion failure, someone
used a `LAX_` accessor — switch it back to `CAST(JSON_VALUE(...) AS <TYPE>)`, which errors instead of
hiding it.)

**Verify.** Run the payload-inspection query (`JSON_VALUE`) and re-check the profiling output
(`_absent`, `_json_null`, `_json_types`) for that field.

**Fix.** For (a) correct the path to the actual key. For (b)/(c) the row carries no value: if the schema
requires the field, partition those rows off; if optional, the NULL is expected. Re-run the test SELECT
and re-check the gate in `reference/strict-struct-typing.md`.

---

## A payload field is rejected as `additionalProperties` / isn't in the target struct

**Symptom.** The drill-in `errors` show an `additionalProperties` violation — the event carries a
field the schema doesn't recognise (e.g. the payload sends `numbers` but the target struct has no
`numbers`).

**Likely cause.** The schema defines the field under a *different name* (a typo or rename in the
schema itself — e.g. the column `data_type` shows `nubmers`, not `numbers`), or the field genuinely
isn't in the schema.

**Verify.** Compare the payload keys (payload-inspection query) against the recovery column's
`data_type` field names (the "finding the recovery column name" query).

**Fix.** If it's a renamed/misspelled schema field, **map the payload key onto the target field name**
in the recovery expression — e.g. `CAST(JSON_VALUE(...data.numbers) AS INT64) AS nubmers` (extractor
per the field's profile) — the payload key and the
struct field name don't have to match. If the field truly isn't in the schema at all, it can't be
recovered into a column that doesn't exist; the schema must be evolved first (outside this flow).

---

## Struct type mismatch when running the generated INSERT

**Symptom.** The INSERT fails with a type mismatch on the recovery column even though the test SELECT
passed.

**Likely cause.** The reconstructed struct's field **names, types, or order** don't exactly match the
target column's `data_type` — most often a reordered field or a missing `_schema_version` first field.

**Verify.** Put the recovery expression's fields side by side with the recovery column's `data_type`
(from the "Finding the recovery column name" query) and check name, type, and position of each.

**Fix.** Rebuild the `[STRUCT(...)]` to match `data_type` field-for-field, in order, with
`_schema_version` first. Re-run the test SELECT, then regenerate the INSERT.

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

**Verify.** Compare the two schemas: `INFORMATION_SCHEMA.COLUMNS` for staging vs good (ordinal order).

**Fix.** Recreate staging with `CREATE TABLE <staging> LIKE <good>` (copies exact columns/types/order),
re-run the recovery INSERT into it, then promote. `LIKE` is what makes the positional `SELECT *`
promote safe.

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

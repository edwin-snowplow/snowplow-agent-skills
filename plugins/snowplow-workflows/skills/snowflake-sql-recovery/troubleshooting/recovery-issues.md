# Troubleshooting — Snowflake failed-events recovery

Failure modes seen (or anticipated from Snowflake semantics) during recovery, in **symptom → likely
cause → verify → fix** form. Each phase of `SKILL.md` points here when the relevant step can go wrong.

---

## The generated INSERT is missing the recovery expression (recovery column passed through instead)

**Symptom.** In the statement returned by the Phase 7 generator, the entity column you're repairing
appears as a bare column name (pass-through), not as your `ARRAY_CONSTRUCT(OBJECT_CONSTRUCT(...))`
expression.

**Likely cause.** The `<recovery_column>` you passed the generator doesn't match the good table's
actual column name (casing, wrong major-version suffix, or derived from the Iglu URI instead of
queried).

**Verify.** Re-run the "Finding the recovery column name" query (`templates/recovery-sql.md`) and
compare the real `column_name` to the value you substituted.

**Fix.** Use the exact `column_name` returned (the generator matches on `LOWER(...)`, so case aside it
must be the same string). Regenerate. If several major versions came back, pick the one whose trailing
`_<major>` matches the schema version being recovered.

---

## Test SELECT errors on a `::TYPE` cast (e.g. "Numeric value '...' is not recognized")

**Symptom.** The Phase 4 test SELECT fails when Snowflake tries to cast a `data:<field>` value to its
target type (e.g. `::INTEGER`).

**Likely cause.** A value in some rows genuinely can't become the target type — the failed rows do
**not** all share one recovery. Snowflake errors on a bad `::TYPE` cast rather than continuing, which
is the correct, loud behaviour.

**Verify.** You skipped or ignored the **Phase 3.5 profiling** step, which exists precisely to catch
this before you suggest anything. Run the profiling query and read the `_uncoercible` and
`_variant_types` counts for that field.

**Fix.** If profiling shows `_uncoercible = 0` for every field, the `::TYPE` cast will run clean (an
int sent as `"5"` still coerces) — the earlier error was on a field you had mistyped, so correct its
target type and re-run. If profiling shows `_uncoercible > 0`, some values truly won't coerce:
**partition those rows off** and recover them separately (or fix at source). Do **not** switch the
recovery expression to `TRY_CAST` to dodge the error — that would hide the un-coercible values as
silent NULLs and write an incomplete recovery.

---

## Unexpected NULLs in the reconstructed column (query ran, but values are null)

**Symptom.** The test SELECT shows a reconstructed field is NULL where a value was expected.

**Likely cause.** With a `::TYPE` cast a NULL means (a) a wrong `:` path — `data:<field>` doesn't
match the actual key (typo, casing, nested deeper) — or (b) the key was genuinely **absent** in those
rows (profiling's `_absent` count). A bad *value* would have errored, not NULLed, so it isn't that.
(If you see NULLs from a coercion failure, someone used `TRY_CAST` in the recovery expression — switch
it back to a plain `::TYPE` cast, which errors instead of hiding it.)

**Verify.** Run the payload-inspection query (`LATERAL FLATTEN`) and re-check the profiling output
(`_absent`, `_variant_types`) for that field.

**Fix.** For (a) correct the `:` path to the actual key. For (b) the field is missing in some rows: if
the schema requires it, partition those rows off; if optional, the NULL is expected. Re-run the test
SELECT and re-check the gate in `reference/column-classification-and-validation.md`.

---

## A payload field is rejected as `additionalProperties` / isn't in the target schema

**Symptom.** The drill-in `errors` show an `additionalProperties` violation — the event carries a
field the schema doesn't recognise (e.g. the payload sends `numbers` but the schema has no `numbers`).

**Likely cause.** The schema defines the field under a *different name* (a typo or rename in the
schema itself — e.g. the schema key is `nubmers`, not `numbers`), or the field genuinely isn't in the
schema.

**Verify.** Compare the payload keys (payload-inspection query) against the schema's field names (a
validated good row, or the Iglu schema JSON).

**Fix.** If it's a renamed/misspelled schema field, **map the payload key onto the target key name**
in the recovery expression — e.g. `'nubmers', contexts_..._failure_1[0]:data:numbers::INTEGER` — the
payload key and the object key don't have to match. If the field truly isn't in the schema at all, it
can't be recovered into a column that doesn't exist; the schema must be evolved first (outside this
flow).

---

## The reconstruction rebuilds into the wrong shape (struct/VARIANT issue)

**Symptom.** The recovered entity loads but downstream models/queries can't read a field, or the
loader rejects it — the reconstructed object is missing `_schema_version`, or a key/value type is off.

**Likely cause.** Snowflake has no strict struct typing to catch a shape error at INSERT time, so a
missing `_schema_version` (mandatory for the Streaming Loader) or a wrong `::TYPE` cast passes silently
into the good table.

**Verify.** Re-run the test SELECT and check: `_schema_version` is present and first, and each
`TYPEOF()` returns the intended type against a validated good row (the "Inspect a validated event"
query).

**Fix.** Rebuild the `ARRAY_CONSTRUCT(OBJECT_CONSTRUCT(...))` with `'_schema_version'` as the first
key set to the corrected version, and correct any `::TYPE` cast whose `TYPEOF()` didn't match the
validated good row. Re-run the test SELECT, then regenerate the INSERT.

---

## Test SELECT fails: `Invalid argument types for function 'TYPEOF'`

**Symptom.** The Phase 4 test SELECT errors with `SQL compilation error: Invalid argument types for
function 'TYPEOF': (VARCHAR(...))` (or `INTEGER` / `BOOLEAN`), pointing at a `TYPEOF(...)` line.

**Likely cause.** `TYPEOF()` was wrapped directly around a `::TYPE` cast — e.g.
`TYPEOF(contexts_..._failure_1[0]:data:words::STRING)`. `TYPEOF()` only accepts a **VARIANT**, but a
`::TYPE` cast returns a native scalar (VARCHAR/INTEGER/BOOLEAN), which it rejects.

**Verify.** Look for any `TYPEOF(... ::STRING)` / `::INTEGER` / `::BOOLEAN` in the test SELECT — that
raw-cast-inside-`TYPEOF` shape is the offender.

**Fix.** Build the reconstructed column once in a `recovered_rows` CTE, then call `TYPEOF()` on
**VARIANT navigation** into it — `TYPEOF(recovered[0]:<field>)`, not on the cast. Use the two-CTE
test-SELECT pattern in `templates/recovery-sql.md`; navigating into the array you just built returns a
VARIANT, which `TYPEOF()` accepts.

---

## Duplicate pre-check returns > 0

**Symptom.** The duplicate pre-check finds `event_id`s already present in the good table.

**Likely cause.** This schema + app_id + window was (at least partly) recovered before — those events
only exist because they failed validation, so any match means a prior recovery.

**Verify.** Run the second duplicate-pre-check query to list the overlapping `event_id`s.

**Fix.** Decide with the user: stop, narrow the time window to exclude the already-recovered rows, or
proceed knowingly (accepting duplicates). Never run the INSERT blind after a non-zero pre-check.

---

## The failure is a ResolutionError, not a ValidationError

**Symptom.** The identification/drill-in shows the failure is a `ResolutionError` (Iglu couldn't
resolve the schema) rather than a data/field validation problem — and it's tempting to declare it
unrecoverable.

**Likely cause.** The schema referenced by the event couldn't be fetched from any Iglu registry:
a transient/misconfigured registry, a wrong URI (typo or nonexistent version), or a schema that was
never published.

**Verify.** Read the `errors` content to confirm it's a resolution (not validation) failure, and
establish the **correct** schema URI. Then run the "Finding the recovery column name" query
(`templates/recovery-sql.md`) against the **corrected** schema name to check a target column exists
in the good table.

**Fix.** This is recoverable in-warehouse in most cases — follow "Recovering a ResolutionError" in
`SKILL.md`: confirm the correct schema now resolves and a target column exists; **filter** the failed
rows on the URI recorded in the failure context (the original, possibly-wrong one) but **reconstruct**
into the corrected URI's column with the corrected `_schema_version`. Only give up if the schema can't
be made to resolve at all or no target column can exist.

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

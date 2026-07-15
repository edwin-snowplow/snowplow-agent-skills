---
name: databricks-sql-recovery
description: Recover Snowplow failed events from a Databricks failed events table back into the good atomic events table by generating the complete SQL the user runs — identification, payload inspection, a validation-gated test SELECT, the full INSERT, and post-checks. Use to reprocess, reinsert, or repair schema-validation failed events in Databricks / Delta Lake, especially events that failed on a single custom entity/context. Builds the wide INSERT itself with an INFORMATION_SCHEMA metadata query and reconstructs entities as native ARRAY<STRUCT> via ARRAY(NAMED_STRUCT(...)), matching the target column's field names, types, and order exactly (read from ground truth, never guessed). Handles Databricks specifics: GET_JSON_OBJECT string extraction, CAST typing, and ANSI-mode-dependent cast behaviour.
---

# Databricks failed-events recovery

Generate the **complete set of SQL** a user needs to recover Snowplow events that failed schema
validation on a custom entity, moving them from a failed events table back into the good atomic
events table. Both are Databricks (Delta Lake) tables; the failed events table stores the failure
context as a native `ARRAY<STRUCT<...>>` column whose `data` field is a **JSON string**.

Your job is to be the whole recovery system, from first diagnosis to a ready-to-run INSERT. Start
by offering to help the user *find and identify* recent failures — don't assume they already know
what's failing. Then walk them from a chosen failure to the complete SQL runbook that recovers it.

You cannot run Databricks yourself, so this is human-in-the-loop: you generate queries, the user runs
them and pastes results back, and you use those to generate the next step. **Never ask the user to
confirm a fact a query could establish** — the column name, the field names, the values, the schema
version, whether rows already exist. If it lives in the warehouse, generate the query and have them
run it. The correct shape and types of the repaired entity are **defined by the Iglu schema**, not
chosen by you — so don't guess them either: read them from a validated event in the good table, from
the recovery column's `INFORMATION_SCHEMA` `full_data_type`, or from the schema JSON (see Phase 4).
Only ask the user for genuine decisions: which schema to recover, and the write path. Three facts can
only come from running a query, so you always need results fed back before you can finalise
downstream:

- **what's failing** (revealed by the identification query),
- the actual **`data` field values/names** (revealed by the payload inspection query), and
- the two tables' **column sets** — but in Databricks you do **not** collect these by hand. The
  INSERT-generator query in Phase 7 reads both column sets from `INFORMATION_SCHEMA.COLUMNS` and
  classifies every column itself. No `DESCRIBE` paste, no local script.

At any SQL-generation step, **read `reference/sql-reference.md`** (in this skill's folder) and apply
its column-classification, JSON-navigation, strict-struct-typing, and template rules exactly — they
encode edge cases confirmed against real Snowplow tables. Do not reconstruct them from memory.

One run recovers **one failing schema** for **one app_id**. If the user has several, do them one at
a time.

---

## What makes Databricks different (a hybrid of the Snowflake and Athena flows)

- **The INSERT builds itself — no script (like Snowflake).** A single metadata query joins the good
  and failed tables' `INFORMATION_SCHEMA.COLUMNS`, classifies each column (pass-through / null-cast /
  recovery / load_tstamp / failure), and emits the full INSERT statement as text. There is no
  `generate_insert.py` and no `DESCRIBE` paste.
- **Strict struct typing (like Athena/BigQuery).** Databricks entity columns are native
  `ARRAY<STRUCT<...>>`. For the INSERT to type-match, the reconstructed struct's **field names, field
  types, and field order** must match the target column's type. Delta resolves struct fields by
  **name**, so the names must match exactly; keep types and order matching too. So the entity shape is
  always read from ground truth (a validated good-table row, or the recovery column's `full_data_type`
  in `INFORMATION_SCHEMA`) — never guessed.
- **JSON payload is a STRING.** The failure context's `data` is a **JSON-serialised STRING** (not a
  native JSON/VARIANT type). Read scalars out with `GET_JSON_OBJECT(data, '$.field')`, which returns a
  **STRING**, then wrap in `CAST(... AS <type>)` to get the target type — e.g.
  `CAST(GET_JSON_OBJECT(..., '$.user_id') AS BIGINT)`.
- **Casts and paths can fail silently — the biggest trap.** Unlike Snowflake and BigQuery (which
  error on a bad cast, so a clean run proves the types), Databricks is different in two ways:
  `GET_JSON_OBJECT` returns **NULL** for a missing/invalid path (never errors), and `CAST` returns
  **NULL** on a bad value in legacy mode (it only errors under ANSI mode, which *is* the default on
  Databricks SQL warehouses but not on all clusters). So **a clean run is not sufficient evidence** —
  Phase 5 validation must actively check for unexpected NULLs and eyeball the values, not just that the
  query ran. See the reference's validation checklist.
- **Array access.** The failure context is a native array; `[0]` (zero-based direct index) reads its
  single element, e.g. `contexts_com_snowplowanalytics_snowplow_failure_1[0].data`. `element_at(col, 1)`
  is the 1-based equivalent; `try_element_at(col, 1)` returns NULL instead of erroring.

---

## Phase 1 — Start: gather tables, then offer to help identify the failures

**Ask for both tables up front, in the opening message**, so every later query and prompt already
has the real names — never leave `<good_catalog>.<good_schema>.<good_table>` placeholders to
resolve mid-flow. In Databricks (Unity Catalog) a table is `CATALOG.SCHEMA.TABLE`, so collect all
three parts:

- **Failed events** catalog + schema + table (recover *from*; table usually `events`)
- **Good events** catalog + schema + table (recover *into*; table usually `events`)
- **When the failures occurred** — a date or rough range. Bounding `load_tstamp` scans far less (and
  the good/failed tables are typically Delta tables clustered/partitioned by `load_tstamp`, so a
  bounded range prunes files). If they don't know, fall back to a lookback window (default 7 days) and
  start narrow.

In the same message, ask whether they want help finding the failures or already know the target:

> "First, what are your failed and good events tables (catalog.schema.table each), and roughly when
> did the failures happen (a date or range — helps me scan less)? If you're not sure, I'll use a
> lookback window. And do you want help finding and identifying recent failed events, or do you
> already know the failing schema and app_id you want to recover?"

Carry this time window through **every** query (identification, inspection, test, INSERT, duplicate
pre-check) so the identified, validated, and recovered sets match — see the reference's "Time
window" note. Prefer a bounded `load_tstamp` range when the timeframe is known.

- **They want help identifying** → go to Phase 2.
- **They already know the failure** → collect the failing schema Iglu URI, app_id, and target
  entity column now, then go to Phase 3.

From here on, always refer to the tables by the names given — never re-ask for them.

---

## Phase 2 — Identify the failures

**Lead with the summary identification query** (reference: "Identification query — summary") using
the failed table and the Phase 1 time window. It returns one row per schema × app_id × failure type
with volumes and time span — the right overview, and it avoids dumping every failed row over a wide
window. Help them read the pasted results:

- which **schemas** are failing, at what **volume**, and the **failure types** (e.g. `SchemaViolation`),
- the **app_id(s)** affected and the time span.

Then have the user **pick one failing schema to recover** and confirm its Iglu URI and app_id. Now
run the **row-level drill-in query** (reference: "row-level drill-in"), scoped to that schema +
app_id, to read the `errors`/`data` and form a first hypothesis of the fix (wrong type, missing,
renamed field) — this guides the payload inspection and recovery expression next.

**Offer the duplicate pre-check now (optional).** As soon as the schema is chosen, ask whether they
want to confirm these events haven't already been recovered in a past run:

> "Want me to check whether these events have already been recovered before? You can skip this if
> you know this schema has never been recovered."

If they want it, give the reference's **duplicate pre-check** query (good table vs the failed-event
set for this schema + app_id + time window). Expect **0** — if it returns > 0, list the overlapping
`event_id`s and flag that recovering again would duplicate them, then decide with the user whether
to stop, narrow scope, or proceed knowingly. If they skip, note it and move on.

---

## Phase 3 — Inspect the payload and confirm the fix

Generate the **payload inspection query** (`LATERAL VIEW explode` the failure array + `GET_JSON_OBJECT`
navigation) so the user sees the real values behind each field for the chosen schema. From the
results, confirm the exact field names, their values, and the intended target types — i.e. exactly
how each failed field maps into the repaired entity. This mapping drives the recovery expression in
Phase 4.

---

## Phase 4 — Build and emit the test SELECT

You do **not** need a `DESCRIBE` here. You need two things, and you get both from queries, not
guesses:

1. **The target entity's exact column name** — give the user the reference's **"Finding the recovery
   column name"** query (an `INFORMATION_SCHEMA.COLUMNS` lookup `LIKE '%<schema_name>%'`) and use the
   `column_name` it returns as `<recovery_column>`. Never confirm a column name you derived from the
   Iglu URI.
2. **The target entity's exact struct shape** — because Databricks struct typing is strict (Delta
   resolves struct fields by name), the reconstructed entity's field **names, types, and order** must
   match the target column. The same query returns the recovery column's `full_data_type`, e.g.
   `ARRAY<STRUCT<_schema_version: STRING, user_id: BIGINT, user_name: STRING>>`. That string **is** the
   contract: build your `NAMED_STRUCT(...)` with exactly those fields, in exactly that order, cast to
   exactly those types.

The field values come from the Phase 3 payload inspection (also a query), so both sides of the
mapping are grounded in real output. Then build the **recovery expression**: an array of one struct,
`ARRAY(NAMED_STRUCT('_schema_version', '<ver>', '<field>', CAST(GET_JSON_OBJECT(...) AS <type>), ...))`,
mapping each `contexts_..._failure_1[0].data` field to its target field/type.

**The correct field names, types, and order are defined by the Iglu schema (and materialised in the
column type) — they are not yours to decide or guess.** If there is any doubt, resolve it from ground
truth before building the expression:

- **The recovery column's `full_data_type`** (from step 2 above) is usually enough — it names every
  field, its type, and its order.
- **Query a validated event** — give the user the reference's **"Inspect a validated event"** query
  to pull a recent good row for this entity column from the good table, to cross-check the shape.
- **Or ask for the schema JSON** — if no validated events exist yet (e.g. a brand-new schema) and the
  `full_data_type` is ambiguous, ask the user for the Iglu schema JSON (or its URL) and read the field
  types and `required` list directly from it.

Only once the intended shape is grounded this way do you write the extraction casts.

- **`_schema_version` is mandatory** for the Databricks Loader — always include it as the first struct
  field, set to the corrected major-compatible version.
- Fields are extracted with `GET_JSON_OBJECT(..., '$.field')` (returns STRING) then `CAST(... AS <type>)`.
  Because a bad path or bad value can yield **NULL rather than an error** in Databricks, the test
  SELECT alone doesn't prove correctness — you must inspect the values (see Phase 5).

Emit a **test SELECT** (per the reference's test-SELECT pattern) that returns, for each row:
`event_id` and the reconstructed column. **Do not generate the INSERT yet.** Ask the user to run the
test SELECT and **paste the full output back**, so you can validate before anything touches the good
table.

---

## Phase 5 — Validate the test output (gate — do not skip)

When the user pastes the test output, validate that the reconstructed column is correct, per the
reference's validation checklist. In short, confirm:

- **No unexpected NULLs (decisive here).** Unlike Snowflake/BigQuery, Databricks can return NULL
  silently — `GET_JSON_OBJECT` on a wrong path returns NULL, and `CAST` returns NULL on a bad value in
  legacy (non-ANSI) mode. So a null where a value was expected is the primary signal of a wrong JSON
  path or a genuinely un-castable value — investigate every one before proceeding. Do **not** treat "the
  query ran" as proof the casts held.
- **Values look right** — the repaired field(s) hold the corrected values (e.g. the string is now a
  proper integer, not NULL), and pass-through fields are intact.
- **Struct shape matches the target** — the reconstructed struct's field names, types, and order match
  the recovery column's `full_data_type` exactly (Delta resolves struct fields by name for the INSERT),
  and `_schema_version` is present as the first field.
- **Row count** — matches the number of failed events expected for this schema + app_id.

State the verdict clearly. If anything fails, **fix the recovery expression and re-emit the test
SELECT (return to Phase 4)** — loop until it validates. Only advance to Phase 6 once the values are
correct (no unexpected nulls), the shape matches, and the row count is right.

---

## Phase 6 — Choose the write path (ask before generating the INSERT)

Once the gate passes and before generating the INSERT, ask the user how they want to write:

> "Do you want to recover into a **staging table first** (recommended — you can inspect the actual
> written rows and promote them when you're happy), or **straight into the good table**?"

- **Staging table (recommended):** safer — the recovered rows land in a throwaway table you can
  query and validate before anything touches production; promote with a single INSERT when ready.
- **Straight into the good table:** faster, but writes to production immediately.

**If staging:** guide the user to create an empty staging table that mirrors the good table, using
the reference's "Create an empty staging table" pattern (`CREATE TABLE … AS SELECT * … LIMIT 0`, which
preserves the exact column set, types, and order). The staging table becomes the INSERT target in
Phase 7.

The **INSERT target** for Phase 7 is therefore the staging table (staging path) or the good table
(direct path). Either way, column classification is always derived from the **good** table's
`INFORMATION_SCHEMA`, so the staging table must mirror the good table's schema (which the CTAS
`LIMIT 0` guarantees).

---

## Phase 7 — Generate the INSERT (Databricks builds it itself)

There is **no local script and no `DESCRIBE` paste**. Instead, give the user the reference's
**INSERT-generator query** with the placeholders filled in from everything gathered so far:

- good and failed table identifiers (catalog.schema.table),
- the **INSERT target** chosen in Phase 6 (good or staging table),
- the **recovery column** name,
- the validated **recovery expression** (with single quotes doubled `''` so it survives being
  embedded as a string literal),
- the **failing schema** Iglu URI, **app_id**, and the **time window**.

The generator query joins the good and failed tables' `INFORMATION_SCHEMA.COLUMNS` and, per column
in good-table ordinal order, decides:

- the **failure column** → `CAST(NULL AS <good full_data_type>)` (kept in position, always null in
  recovered rows),
- **`load_tstamp`** → `CURRENT_TIMESTAMP()`,
- the **recovery column** → the embedded recovery expression,
- **columns present in the failed table** → pass through by name,
- **columns only in the good table** → `CAST(NULL AS <target full_data_type>)`.

The user runs it; it returns **one cell containing the full INSERT statement**. Have them paste that
back. Review it with them — confirm the recovery column carries the reconstructed expression, the
failure column is nulled, `load_tstamp` is refreshed, and the column count looks right — then hand
it to Phase 8 to execute. If anything looks off (e.g. the recovery column wasn't matched), fix the
placeholder inputs and regenerate.

---

## Phase 8 — Run, validate, and (staging path) promote

The duplicate pre-check is offered earlier (Phase 2). If the user **skipped** it there and is at all
unsure whether this recovery has been run before, offer it once more now (reference: "duplicate
pre-check") before inserting — running again would create duplicates. Otherwise proceed.

Advise running the INSERT into the chosen target, and:

1. **Validate what landed** — row count matches the expected failed-event count, and spot-check the
   recovered column (reference validation queries, run against the target table).

**Direct path:** that's it — the rows are in the good table. Optionally use Delta time travel to
compare before/after: `SELECT COUNT(*) FROM <good_table> TIMESTAMP AS OF '2024-01-15T12:00:00'` (or
`VERSION AS OF <n>`).

**Staging path:** once the staging table validates,

2. **Promote** — give the `INSERT INTO <good> SELECT * FROM <staging>` query (reference "Promote"
   pattern; column order matches because staging was created with `SELECT * … LIMIT 0`).
3. **Re-validate** the good table (row count + spot-check).
4. **Offer to clean up** — the optional `DROP TABLE <staging>` once they're satisfied.

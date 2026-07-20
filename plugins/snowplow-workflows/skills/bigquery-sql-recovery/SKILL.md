---
name: bigquery-sql-recovery
description: Recover Snowplow failed events from a BigQuery failed events table back into the good atomic events table by generating the complete SQL the user runs — identification, payload inspection, a validation-gated test SELECT, the full INSERT, and post-checks. Use to reprocess, reinsert, or repair schema-validation failed events in BigQuery, especially events that failed on a single custom entity/context. Builds the wide INSERT itself with an INFORMATION_SCHEMA metadata query and reconstructs entities as native ARRAY-of-STRUCT columns, matching the target column's field names, types, and order exactly (read from ground truth, never guessed).
---

# BigQuery failed-events recovery

Generate the **complete set of SQL** a user needs to recover Snowplow events that failed schema
validation on a custom entity, moving them from a failed events table back into the good atomic
events table. Both are BigQuery tables; the failed events table stores the failure context as a
native `ARRAY<STRUCT<...>>` (`REPEATED RECORD`) column whose `data` field is a `JSON` value.

Your job is to be the whole recovery system, from first diagnosis to a ready-to-run INSERT. Start
by offering to help the user *find and identify* recent failures — don't assume they already know
what's failing. Then walk them from a chosen failure to the complete SQL runbook that recovers it.

You cannot run BigQuery yourself, so this is human-in-the-loop: you generate queries, the user runs
them and pastes results back, and you use those to generate the next step. **Never ask the user to
confirm a fact a query could establish** — the column name, the field names, the values, the schema
version, whether rows already exist. If it lives in the warehouse, generate the query and have them
run it. The correct shape and types of the repaired entity are **defined by the Iglu schema**, not
chosen by you — so don't guess them either: read them from a validated event in the good table, from
the recovery column's `INFORMATION_SCHEMA` `data_type`, or from the schema JSON (see Phase 4). Only
ask the user for genuine decisions: which schema to recover, and the write path. Three facts can
only come from running a query, so you always need results fed back before you can finalise
downstream:

- **what's failing** (revealed by the identification query),
- the actual **`data` field values/names** (revealed by the payload inspection query), and
- the two tables' **column sets** — but in BigQuery you do **not** collect these by hand. The
  INSERT-generator query in Phase 7 reads both column sets from `INFORMATION_SCHEMA.COLUMNS` and
  classifies every column itself. No `DESCRIBE` paste, no local script.

At any SQL-generation step, **read `reference/sql-reference.md`** (in this skill's folder) and apply
its column-classification, JSON-navigation, strict-struct-typing, and template rules exactly — they
encode edge cases confirmed against real Snowplow tables. Do not reconstruct them from memory.

One run recovers **one failing schema** for **one app_id**. If the user has several, do them one at
a time.

---

## What makes BigQuery different (a hybrid of the Snowflake and Athena flows)

- **The INSERT builds itself — no script (like Snowflake).** A single metadata query joins the good
  and failed tables' `INFORMATION_SCHEMA.COLUMNS`, classifies each column (pass-through / null-cast /
  recovery / load_tstamp / failure), and emits the full INSERT statement as text. There is no
  `generate_insert.py` and no `DESCRIBE` paste.
- **Strict struct typing (like Athena).** BigQuery entity columns are native `ARRAY<STRUCT<...>>`.
  For a positional INSERT to type-match, the reconstructed struct's **field names, field types, and
  field order** must match the target column's type **exactly**. So the entity shape is always read
  from ground truth (a validated good-table row, or the recovery column's `data_type` in
  `INFORMATION_SCHEMA`) — never guessed.
- **JSON payload — profile, then pick the extractor per field.** The failure context's `data` is a
  `JSON` value. A repaired value may already be the native JSON type or sent as the wrong one, so a
  **profiling step** (Phase 3.5) decides per field: **strict** `INT64()/STRING()/FLOAT64()/BOOL()` where
  the value is already native, `CAST(JSON_VALUE(...) AS <TYPE>)` where it's string-encoded or mixed,
  and *stop & partition* where it won't coerce. Both error on a bad value (never silent-NULL), and
  profiling guarantees they won't. **Don't** use the lenient `LAX_` accessors (they hide failures as
  NULL). See `reference/sql-reference.md`.
- **Array access.** The failure context is a native array; `[0]` (zero-based direct index) reads its
  single element, e.g. `contexts_com_snowplowanalytics_snowplow_failure_1[0].data`. Use
  `[SAFE_OFFSET(0)]` if you want NULL instead of an error on an empty array.

---

## Phase 1 — Start: gather tables, then offer to help identify the failures

**Ask for both tables up front, in the opening message**, so every later query and prompt already
has the real names — never leave `<good_project>.<good_dataset>.<good_table>` placeholders to
resolve mid-flow. In BigQuery a table is `PROJECT.DATASET.TABLE`, so collect all three parts:

- **Failed events** project + dataset + table (recover *from*; table usually `events`)
- **Good events** project + dataset + table (recover *into*; table usually `events`)
- **When the failures occurred** — a date or rough range. Bounding `load_tstamp` scans far less (and
  the good/failed tables are typically partitioned by `load_tstamp`, so a bounded range prunes
  partitions). If they don't know, fall back to a lookback window (default 7 days) and start narrow.

In the same message, ask whether they want help finding the failures or already know the target:

> "First, what are your failed and good events tables (project.dataset.table each), and roughly when
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
renamed field, **or a schema-resolution failure** — see "Recovering a ResolutionError" below) — this
guides the payload inspection and recovery expression next.

**Offer the duplicate pre-check now (optional).** As soon as the schema is chosen, ask whether they
want to confirm these events haven't already been recovered in a past run:

> "Want me to check whether these events have already been recovered before? You can skip this if
> you know this schema has never been recovered."

If they want it, give the reference's **duplicate pre-check** query (good table vs the failed-event
set for this schema + app_id + time window). Expect **0** — if it returns > 0, list the overlapping
`event_id`s and flag that recovering again would duplicate them, then decide with the user whether
to stop, narrow scope, or proceed knowingly. If they skip, note it and move on.

**Ask for the schema JSON (custom schemas).** Once the failing schema is chosen, if it's a **custom
schema** — your own vendor, not a Snowplow / Iglu Central one — ask the user to paste the **Iglu JSON
schema** for it (or its registry URL). This isn't strictly required (the queries you're already
running — the recovery column's type and the Phase 3.5 profiling — ground the recovery) and it doesn't
replace any of them; it's authoritative context the user can reasonably provide that lets you
sanity-check field names, types, the `required` list, and enums/constraints, catch a renamed/misspelled
field, and judge whether an un-coercible value is genuinely bad or a sign the target type is wrong.
Treat it as extra confidence. (For Iglu Central / Snowplow schemas you can reason from the public
schema directly.)

---

## Recovering a ResolutionError (schema couldn't be resolved)

This flow is **not** limited to `ValidationError` cases. A **ResolutionError** means Iglu couldn't
*resolve* (fetch) the referenced schema at all; a **ValidationError** means the schema resolved but
the data didn't conform. Tell them apart from the `failure_type` and the `errors` content — and do
**not** treat a ResolutionError as automatically unrecoverable.

A ResolutionError **is** recoverable in-warehouse in these cases:

- **Transient / misconfigured registry** — the schema was valid; Iglu was just unreachable at
  enrichment time. The payload already conforms; reconstruct it into the correct column unchanged.
- **Wrong URI** — a typo in the vendor/name, or a version that doesn't exist. Re-point to the
  corrected URI, reconstruct into *that* schema's column, and adjust any fields the correct schema
  requires (exactly as in a ValidationError recovery).
- **Never published** — register the schema in Iglu first (outside the warehouse), then recover.

Two things must be true before the INSERT can land — confirm both with the user:

1. **The correct schema now resolves** — it exists in a registry (the same URI if transient, the
   corrected URI if it was wrong, or a newly registered one).
2. **A target column exists** in the good table for the correct schema. Loaders materialise an
   entity's column only after a valid event of that schema has loaded, so a schema that never
   resolved may have **no column yet**. Confirm with the Phase 4 "finding the recovery column name"
   query run against the **corrected** schema name — a returned row is your confirmation. If none
   comes back, the schema must load once (or the column be created) before recovery can land.

**Key subtlety — filter on the recorded URI, build into the corrected one:**

- **Filter** the failed rows by the schema URI **as stored in the failure context**
  (`contexts_com_snowplowanalytics_snowplow_failure_1[0].schema`) — the original, possibly-wrong URI
  the event actually carried. Every WHERE predicate (identification drill-in, payload, test `prep`,
  INSERT `prep`, duplicate pre-check) must match on that recorded value.
- **Derive** the target column and `_schema_version` from the **corrected** URI.
- When the two are the same (transient), nothing else changes. When they differ (typo / version
  fix), keep the predicates on the recorded URI but reconstruct into the corrected column/version.

**Only bail out** when the schema genuinely can't be made to resolve (doesn't exist and won't be
registered) or no target column can exist — then it isn't a data fix. Enrichment failures and
adapter failures remain out of scope regardless.

---

## Phase 3 — Inspect the payload and confirm the fix

Generate the **payload inspection query** (`UNNEST` the failure array + `JSON_VALUE`) so the user
sees the real values behind each field for the chosen schema. From the results, confirm the exact
field names, their values, and **how each value is encoded** (a quoted number is a string-encoded
int). Expect failures to be **compound** — one schema's failed events can combine a renamed/missing
field *and* a wrong type.

---

## Phase 3.5 — Profile & confirm coercibility (gate — do not skip)

Before building any recovery expression, give the user the reference's **field profiling query** — it
can't error — to learn, per field across *all* failed rows, the JSON types present and whether every
value coerces to its target type. Use it to (a) confirm the whole population shares **one** recovery,
and (b) pick the extractor per field: **strict** where the value is native, **`CAST(JSON_VALUE(...) AS
<TYPE>)`** where string-encoded/mixed, and **stop & partition** where anything won't coerce (see the
reference's decision list). Never fall back to `LAX_`.

---

## Phase 4 — Build and emit the test SELECT

You do **not** need a `DESCRIBE` here. You need two things, and you get both from queries, not
guesses:

1. **The target entity's exact column name** — give the user the reference's **"Finding the recovery
   column name"** query (an `INFORMATION_SCHEMA.COLUMNS` lookup `LIKE '%<schema_name>%'`) and use the
   `column_name` it returns as `<recovery_column>`. Never confirm a column name you derived from the
   Iglu URI.
2. **The target entity's exact struct shape** — because BigQuery struct typing is strict, the
   reconstructed entity's field **names, types, and order** must match the target column exactly. The
   same query returns the recovery column's `data_type`, e.g.
   `ARRAY<STRUCT<_schema_version STRING, user_id INT64, user_name STRING>>`. That string **is** the
   contract: build your `STRUCT(...)` with exactly those fields, in exactly that order, cast to
   exactly those types.

The field values come from the Phase 3 payload inspection (also a query), so both sides of the
mapping are grounded in real output. Then build the **recovery expression**: an array of one struct,
`[STRUCT('<ver>' AS _schema_version, <path> AS <field>, ...)]`, mapping each
`contexts_..._failure_1[0].data.<field>` to its target field via the extractor Phase 3.5 selected for
that field (a strict accessor, or `CAST(JSON_VALUE(...) AS <TYPE>)`).

**The correct field names, types, and order are defined by the Iglu schema (and materialised in the
column type) — they are not yours to decide or guess.** If there is any doubt, resolve it from ground
truth before building the expression:

- **The recovery column's `data_type`** (from step 2 above) is usually enough — it names every field,
  its type, and its order.
- **Query a validated event** — give the user the reference's **"Inspect a validated event"** query
  to pull a recent good row for this entity column from the good table, to cross-check the shape.
- **Or ask for the schema JSON** — if no validated events exist yet (e.g. a brand-new schema) and the
  `data_type` is ambiguous, ask the user for the Iglu schema JSON (or its URL) and read the field
  types and `required` list directly from it.

Only once the intended shape is grounded this way do you write the extraction casts.

- **`_schema_version` is mandatory** for the BigQuery Streaming Loader — always include it as the
  first struct field, set to the corrected major-compatible version.
- Fields are extracted with the per-field extractor chosen in Phase 3.5 — a **strict** accessor
  (`INT64()/STRING()/...`) where the value is native, or `CAST(JSON_VALUE(...) AS <TYPE>)` where it's
  string-encoded/mixed. Both error on a bad value (never silent-NULL); profiling already ruled those
  out, so a test SELECT that runs clean proves the casts hold.

Emit a **test SELECT** (per the reference's test-SELECT pattern) that returns, for each row:
`event_id` and the reconstructed column. **Do not generate the INSERT yet.** Ask the user to run the
test SELECT and **paste the full output back**, so you can validate before anything touches the good
table.

---

## Phase 5 — Validate the test output (gate — do not skip)

When the user pastes the test output, validate that the reconstructed column is correct, per the
reference's validation checklist. In short, confirm:

- **Profiling confirmed + the query ran clean** — Phase 3.5 already proved every value coerces, and
  the chosen extractors (strict or `CAST(JSON_VALUE(...))`) error on a bad cast rather than silently
  continuing, so a successful run is strong evidence the types are right.
- **Struct shape matches the target** — the reconstructed struct's field names, types, and order
  match the recovery column's `data_type` exactly (this is what the positional INSERT will require),
  and `_schema_version` is present as the first field.
- **Row count** — matches the number of failed events expected for this schema + app_id.
- **No unexpected nulls** — a null where a value was expected usually means a wrong JSON path;
  investigate before proceeding.
- **Values look right** — the repaired field(s) hold the corrected values (e.g. the string is now a
  proper integer).

State the verdict clearly. If anything fails, **fix the recovery expression and re-emit the test
SELECT (return to Phase 4)** — loop until it validates. Only advance to Phase 6 once the casts hold,
the shape matches, and the values are correct.

---

## Phase 6 — Choose the write path (ask before generating the INSERT)

Once the gate passes and before generating the INSERT, ask the user how they want to write:

> "Do you want to recover into a **staging table first** (recommended — you can inspect the actual
> written rows and promote them when you're happy), or **straight into the good table**?"

- **Staging table (recommended):** safer — the recovered rows land in a throwaway table you can
  query and validate before anything touches production; promote with a single INSERT when ready.
- **Straight into the good table:** faster, but writes to production immediately.

**If staging:** guide the user to create an empty staging table that mirrors the good table, using
the reference's "Create an empty staging table" pattern (`CREATE TABLE … LIKE`). The staging table
becomes the INSERT target in Phase 7.

The **INSERT target** for Phase 7 is therefore the staging table (staging path) or the good table
(direct path). Either way, column classification is always derived from the **good** table's
`INFORMATION_SCHEMA`, so the staging table must mirror the good table's schema (which `LIKE`
guarantees).

---

## Phase 7 — Generate the INSERT (BigQuery builds it itself)

There is **no local script and no `DESCRIBE` paste**. Instead, give the user the reference's
**INSERT-generator query** with the placeholders filled in from everything gathered so far:

- good and failed table identifiers (project.dataset.table),
- the **INSERT target** chosen in Phase 6 (good or staging table),
- the **recovery column** name,
- the validated **recovery expression** (with single quotes doubled `''` so it survives being
  embedded as a string literal),
- the **failing schema** Iglu URI, **app_id**, and the **time window**.

The generator query joins the good and failed tables' `INFORMATION_SCHEMA.COLUMNS` and, per column
in good-table ordinal order, decides:

- the **failure column** → `CAST(NULL AS <good data_type>)` (kept in position, always null in
  recovered rows),
- **`load_tstamp`** → `CURRENT_TIMESTAMP()`,
- the **recovery column** → the embedded recovery expression,
- **columns present in the failed table** → pass through by name,
- **columns only in the good table** → `CAST(NULL AS <target_type>)`.

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

**Direct path:** that's it — the rows are in the good table. Optionally use time travel to compare
before/after: `SELECT COUNT(*) FROM <good_table> FOR SYSTEM_TIME AS OF TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 1 HOUR)`.

**Staging path:** once the staging table validates,

2. **Promote** — give the `INSERT INTO <good> SELECT * FROM <staging>` query (reference "Promote"
   pattern; column order matches because staging was created `LIKE` good).
3. **Re-validate** the good table (row count + spot-check).
4. **Offer to clean up** — the optional `DROP TABLE <staging>` once they're satisfied.

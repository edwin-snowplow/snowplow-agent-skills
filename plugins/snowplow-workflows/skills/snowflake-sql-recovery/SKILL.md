---
name: snowflake-sql-recovery
description: Recover Snowplow failed events from a Snowflake failed events table back into the good atomic events table by generating the complete SQL the user runs — identification, payload inspection, a validation-gated test SELECT, the full INSERT, and post-checks. Use to reprocess, reinsert, or repair schema-validation failed events in Snowflake, especially events that failed on a single custom entity/context. Needs no local script: it builds the wide INSERT itself with a metadata query over INFORMATION_SCHEMA and reconstructs entities as VARIANT/ARRAY (no strict type conversion or reserved-word quoting).
---

# Snowflake failed-events recovery

Generate the **complete set of SQL** a user needs to recover Snowplow events that failed schema
validation on a custom entity, moving them from a failed events table back into the good atomic
events table. Both are Snowflake tables; the failed events table stores the failure context as a
semi-structured `ARRAY`/`VARIANT` column.

Your job is to be the whole recovery system, from first diagnosis to a ready-to-run INSERT. Start
by offering to help the user *find and identify* recent failures — don't assume they already know
what's failing. Then walk them from a chosen failure to the complete SQL runbook that recovers it.

You cannot run Snowflake yourself, so this is human-in-the-loop: you generate queries, the user runs
them and pastes results back, and you use those to generate the next step. **Never ask the user to
confirm a fact a query could establish** — the column name, the field names, the values, the schema
version, whether rows already exist. If it lives in the warehouse, generate the query and have them
run it. The correct shape and types of the repaired entity are **defined by the Iglu schema**, not
chosen by you — so don't guess them either: read them from a validated event in the good table, or
from the schema JSON (see Phase 4). Only ask the user for genuine decisions: which schema to
recover, and the write path. Three facts can only come from running a query, so you always need
results fed back before you can finalise downstream:

- **what's failing** (revealed by the identification query),
- the actual **`data` field values/names** (revealed by the payload inspection query), and
- the two tables' **column sets** — but in Snowflake you do **not** collect these by hand. The
  INSERT-generator query in Phase 7 reads both column sets from `INFORMATION_SCHEMA.COLUMNS` and
  classifies every column itself. No `DESCRIBE` paste, no local script.

At any SQL-generation step, **read `reference/sql-reference.md`** (in this skill's folder) and apply
its column-classification, VARIANT-navigation, and template rules exactly — they encode edge cases
confirmed against real Snowplow tables. Do not reconstruct them from memory.

One run recovers **one failing schema** for **one app_id**. If the user has several, do them one at
a time.

---

## Why there's no script (vs the Athena flow)

Snowflake has fewer limitations than Athena/Trino here, so the whole thing is pure SQL:

- **The INSERT builds itself.** A single metadata query joins the good and failed tables'
  `INFORMATION_SCHEMA.COLUMNS`, classifies each column (pass-through / null-cast / recovery /
  load_tstamp / failure), and emits the full INSERT statement as text. This replaces the Athena
  skill's `generate_insert.py` entirely.
- **Entities are semi-structured.** You reconstruct the repaired entity with
  `ARRAY_CONSTRUCT(OBJECT_CONSTRUCT(...))` and cast fields with `::TYPE`. There is **no**
  Hive→engine type conversion and **no** reserved-word quoting of struct field names — the keys are
  just string literals.
- **No code-execution prerequisite.** Nothing runs locally; every artefact is SQL the user runs in
  Snowflake.

---

## Phase 1 — Start: gather tables, then offer to help identify the failures

**Ask for both tables up front, in the opening message**, so every later query and prompt already
has the real names — never leave `<good_database>.<good_schema>.<good_table>` placeholders to
resolve mid-flow. In Snowflake a table is `DATABASE.SCHEMA.TABLE`, so collect all three parts:

- **Failed events** database + schema + table (recover *from*; table usually `events`)
- **Good events** database + schema + table (recover *into*; table usually `events`)
- **When the failures occurred** — a date or rough range. Bounding `load_tstamp` scans far less. If
  they don't know, fall back to a lookback window (default 7 days) and start narrow.

In the same message, ask whether they want help finding the failures or already know the target:

> "First, what are your failed and good events tables (database.schema.table each), and roughly when
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
  (`contexts_com_snowplowanalytics_snowplow_failure_1[0]:schema`) — the original, possibly-wrong URI
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

Generate the **payload inspection query** (`LATERAL FLATTEN` + `:` navigation) so the user sees the
real values behind each field for the chosen schema. From the results, confirm the exact field
names, their values, and the intended target types — i.e. exactly how each failed field maps into
the repaired entity. This mapping drives the recovery expression in Phase 4.

---

## Phase 3.5 — Profile & confirm coercibility (gate — do not skip)

Before building any recovery expression, give the user the reference's **field profiling query** — it
can't error (`TYPEOF`, `TRY_CAST`) — to learn, per field across *all* failed rows, the VARIANT types
present and whether every value casts to its target type. Use it to confirm the **whole population
shares one recovery** before you suggest anything:

- **any field `_uncoercible > 0`** → the failed rows don't share one fix (some values can't cast to the
  target type) → **stop, list the offending rows, and partition/scope** with the user;
- **any field `_absent > 0`** → the field is missing from some rows → if the schema requires it,
  partition those off; if optional, the cast yields NULL for them;
- **otherwise** → the population is uniform; build the recovery expression (Snowflake's `::TYPE` cast
  coerces both native and string-encoded values, and profiling has confirmed none will error).

---

## Phase 4 — Build and emit the test SELECT

You do **not** need a `DESCRIBE` here — Snowflake entity columns are semi-structured. You need the
target entity's exact **column name**, and you get it with a query, not a guess: give the user the
reference's **"Finding the recovery column name"** query (an `INFORMATION_SCHEMA.COLUMNS` lookup
`LIKE '%<schema_name>%'`) and use the `column_name` it returns as `<recovery_column>`. Never ask the
user to confirm a column name you derived from the Iglu URI — if a query can establish the fact, run
the query.

The field names come from the Phase 3 payload inspection (also a query), so both sides of the
mapping are grounded in real output. Then build the **recovery expression**: the
`ARRAY_CONSTRUCT(OBJECT_CONSTRUCT('_schema_version', '<ver>', '<field>', <path>::<TYPE>, ...))` for
the target column, mapping each `contexts_..._failure_1[0]:data:<field>` to its target field/type.

**The correct field types are defined by the Iglu schema — they are not yours to decide or guess.**
If there is any doubt about the intended shape (field names, types, which are required, the version
to stamp), resolve it from ground truth before building the expression, don't assume:

- **Query a validated event** — give the user the reference's **"Inspect a validated event"** query
  to pull a recent good row for this entity column from the good table, so you can see the exact
  keys and value types a passing event has. This is the fastest path and usually enough.
- **Or ask for the schema JSON** — if no validated events exist yet (e.g. a brand-new schema) or the
  good sample is ambiguous, ask the user for the Iglu schema JSON (or its URL) and read the field
  types and `required` list directly from it.

Only once the intended shape is grounded this way do you write the `::TYPE` casts.

- **`_schema_version` is mandatory** for the Snowflake Streaming Loader — always include it as the
  first key, set to the corrected major-compatible version.
- Fields are cast with `::TYPE` (e.g. `::INTEGER`, `::STRING`, `::BOOLEAN`). A bad cast raises an
  error when the query runs, so a test SELECT that runs clean already proves the casts hold.

Emit a **test SELECT** (per the reference's test-SELECT pattern) that returns, for each row:
`event_id`, the reconstructed column, and `TYPEOF(...)` of the reconstructed field(s). **Do not
generate the INSERT yet.** Ask the user to run the test SELECT and **paste the full output back**,
so you can validate before anything touches the good table.

---

## Phase 5 — Validate the test output (gate — do not skip)

When the user pastes the test output, validate that the reconstructed column is correct, per the
reference's validation checklist. In short, confirm:

- **The query ran clean** — every `::TYPE` cast succeeded (Snowflake errors on a bad cast rather
  than silently continuing), so a successful run is strong evidence the types are right.
- **Field types** — `TYPEOF()` on each reconstructed field returns the intended type
  (e.g. `INTEGER`, `VARCHAR`, `BOOLEAN`), and `_schema_version` is present.
- **Row count** — matches the number of failed events expected for this schema + app_id.
- **No unexpected nulls** — a null where a value was expected usually means a wrong `:` path;
  investigate before proceeding.
- **Values look right** — the repaired field(s) hold the corrected values (e.g. the string is now a
  proper integer).

State the verdict clearly. If anything fails, **fix the recovery expression and re-emit the test
SELECT (return to Phase 4)** — loop until it validates. Only advance to Phase 6 once the casts hold
and the values are correct.

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

## Phase 7 — Generate the INSERT (Snowflake builds it itself)

There is **no local script and no `DESCRIBE` paste**. Instead, give the user the reference's
**INSERT-generator query** with the placeholders filled in from everything gathered so far:

- good and failed table identifiers (database.schema.table),
- the **INSERT target** chosen in Phase 6 (good or staging table),
- the **recovery column** name,
- the validated **recovery expression** (with single quotes doubled `''` so it survives being
  embedded as a string literal),
- the **failing schema** Iglu URI, **app_id**, and the **time window**.

The generator query joins the good and failed tables' `INFORMATION_SCHEMA.COLUMNS` and, per column
in good-table ordinal order, decides:

- the **failure column** → `CAST(NULL AS ...)` (kept in position, always null in recovered rows),
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

**Direct path:** that's it — the rows are in the good table. Optionally use Time Travel to compare
before/after: `SELECT COUNT(*) FROM <good_table> AT (TIMESTAMP => '2024-01-15 12:00:00'::TIMESTAMP)`.

**Staging path:** once the staging table validates,

2. **Promote** — give the `INSERT INTO <good> SELECT * FROM <staging>` query (reference "Promote"
   pattern; column order matches because staging was created `LIKE` good).
3. **Re-validate** the good table (row count + spot-check).
4. **Offer to clean up** — the optional `DROP TABLE <staging>` once they're satisfied.

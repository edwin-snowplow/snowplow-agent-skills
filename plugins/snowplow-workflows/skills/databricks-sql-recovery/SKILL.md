---
name: databricks-sql-recovery
description: Recover Snowplow failed events from a Databricks failed events table back into the good atomic events table by generating the complete SQL the user runs — identification, payload inspection, a validation-gated test SELECT, the full INSERT, and post-checks. Use to reprocess, reinsert, or repair schema-validation failed events in Databricks / Delta Lake, especially events that failed on a single custom entity/context. Assembles the wide INSERT from INFORMATION_SCHEMA column metadata (via a bundled local script) and reconstructs entities as native ARRAY-of-STRUCT columns via ARRAY(NAMED_STRUCT(...)), matching the target column field names, types, and order exactly (read from ground truth, never guessed). Handles Databricks specifics — GET_JSON_OBJECT string extraction, CAST typing, and ANSI-mode-dependent cast behaviour.
---

# Databricks failed-events recovery

Recover Snowplow events that failed schema validation on a custom entity — moving them from a failed
events table back into the good atomic events table — by generating the **complete SQL the user
runs**. Both are Databricks (Delta Lake) tables; the failed events table stores the failure context
as a native `ARRAY<STRUCT<...>>` column whose `data` field is a **JSON-serialised STRING**.

You are the whole recovery system, from first diagnosis to a ready-to-run INSERT. This is
**human-in-the-loop**: you generate queries, the user runs them and pastes results back, and you use
those to generate the next step. You cannot run Databricks yourself.

**Three rules that shape everything:**
- **Never ask the user to confirm a fact a query could establish** — the column name, field names,
  values, schema version, whether rows already exist. If it lives in the warehouse, generate the
  query.
- **The repaired entity's shape and types are defined by the Iglu schema, not chosen by you.** Read
  them from ground truth (the recovery column's `full_data_type`, a validated good-table row, or the
  schema JSON) — never guess.
- **One step at a time — never hand over a query you'd expect the user to edit.** Emit a single query
  (or one tightly-scoped step), wait for the result, then use it to build the next step. When a query
  needs a value another query establishes — the recovery column name, a field's type — run that query
  first and substitute the real value **yourself**. Never emit a query containing a guessed or
  placeholder value with a "swap this in if it differs" note, never batch a query together with the
  one whose output it depends on, and never ask the user to edit the SQL you provide.

One run recovers **one failing schema** for **one app_id**. If the user has several, do them one at a
time.

## Where the knowledge lives (read the form you need, when you need it)

- **`templates/recovery-sql.md`** — every SQL statement, with placeholders. The thing you actually
  hand the user at each phase.
- **`reference/failure-context.md`** — the failure column's structure and how to navigate/extract it,
  the Databricks **silent-NULL trap**, and how to ground the target shape.
- **`reference/strict-typing-and-classification.md`** — why the reconstructed struct must match the
  target type exactly (Delta resolves struct fields by name), how the generator classifies every
  column, the Phase 3.5 profiling decision, and the Phase 5 validation gate.
- **`examples/worked-recovery.md`** — a complete run end to end (string→BIGINT), to pattern-match against.
- **`troubleshooting/recovery-issues.md`** — symptom → cause → verify → fix for when a step goes wrong.
- **`scripts/generate_insert.py`** — assembles the wide INSERT locally (Phase 7) from plain-text
  column pastes, so no quote-laden SQL is round-tripped through the Databricks result grid.

Read the relevant form at each phase rather than working from memory — they encode edge cases
confirmed against real Snowplow tables.

## What makes Databricks specific (a hybrid of the Snowflake and Athena flows)

- **The wide INSERT is assembled by a local script (like Athena).** The good table has ~130 columns
  and the INSERT is positional, so the column list and order come from metadata, not memory. The user
  pastes the good and failed tables' `information_schema.columns` (plain `column_name, full_data_type`
  — copy-safe), and `scripts/generate_insert.py` classifies each column (pass-through / null-cast /
  recovery / load_tstamp / failure) and writes the full INSERT to a `.sql` file locally.
  **Why a local script and not an in-warehouse generator:** the equivalent one-query generator builds
  the whole INSERT as a string in a result cell, and Databricks' result-grid "copy as plain text"
  **strips single-quote characters** — so the pasted statement won't parse (unquoted string literals).
  Assembling locally keeps every quote intact. The in-warehouse generator is kept only as a fallback
  for when local Python isn't available (`templates/recovery-sql.md`).
- **Strict struct typing (like Athena/BigQuery).** Entity columns are native `ARRAY<STRUCT<...>>`.
  Delta resolves struct fields **by name**, so the reconstruction's field names must match exactly;
  keep types and order matching too. The shape is always read from ground truth
  (`reference/strict-typing-and-classification.md`).
- **JSON payload is a STRING.** `data` is a JSON-serialised STRING, not a native JSON/VARIANT type.
  Read scalars with `GET_JSON_OBJECT(data, '$.field')` (returns STRING), then `CAST(... AS <type>)`.
- **Casts and paths can fail silently — the biggest trap.** Unlike Snowflake and BigQuery (which error
  on a bad cast, so a clean run proves the types), `GET_JSON_OBJECT` returns **NULL** on a bad path
  (never errors), and `CAST` returns **NULL** on a bad value in legacy mode (it only errors under ANSI
  mode, the default on Databricks SQL warehouses but not on every cluster). So **a clean run is not
  sufficient evidence** — Phase 3.5 profiling pre-empts it, and Phase 5 must actively check for
  unexpected NULLs (`reference/failure-context.md`).
- **Zero-based array access.** `contexts_..._failure_1[0].data` (`element_at(col, 1)` is the 1-based
  equivalent; `try_element_at(col, 1)` returns NULL instead of erroring).

---

## The 8-phase flow (the decision tree)

Carry one **time window** through every query so the identified, validated, and recovered sets match
(see "Time window" in `templates/recovery-sql.md`). All identifiers are Unity Catalog three-part
`catalog.schema.table`.

### Phase 1 — Gather tables, offer to help identify
Ask for both tables up front (Databricks Unity Catalog `catalog.schema.table`, all three parts each)
and when the failures happened, in the opening message, so no placeholders linger:

> "First, what are your failed and good events tables (catalog.schema.table each), and roughly when
> did the failures happen (a date or range — helps me scan less)? If you're not sure, I'll use a
> lookback window. And do you want help finding and identifying recent failed events, or do you
> already know the failing schema and app_id you want to recover?"

Then: help identifying → Phase 2. Already know the target → collect the failing schema Iglu URI,
app_id, and target entity column → Phase 3.

### Phase 2 — Identify the failures
Lead with the **summary identification query**; read off which schemas fail, at what volume, which
`failure_type` (e.g. `SchemaViolation`), which app_ids. Have the user pick **one** schema; run the
**row-level drill-in** to form a fix hypothesis — including whether it's a schema **resolution**
failure rather than a validation one (see "Recovering a ResolutionError" below). The drill-in also
surfaces **`source`** via `GET_JSON_OBJECT(errors[0], '$.source')` (`errors` is `ARRAY<STRING>`, so
JSON-extract — don't dot into it, and never read the top-level `source`, which is null): a mix of
`contexts` and `unstruct` means the schema failed both as a context entity
**and** as the self-describing event, which recover into **different columns** and must be **separate
runs** (see `reference/failure-context.md`).

**Offer the duplicate pre-check now (optional).** As soon as the schema is chosen — and before
building any recovery expression — **explicitly ask** whether they want to confirm these events
haven't already been recovered; don't silently skip it:

> "Want me to check whether these events have already been recovered before? You can skip this if
> you know this schema has never been recovered."

If they want it, give the **duplicate pre-check** query (`templates/recovery-sql.md`). Expect **0** —
if > 0, list the overlapping `event_id`s and decide with the user (stop, narrow scope, or proceed
knowingly). If they skip, note it and move on.

Then, if the failing schema is a **custom schema** (your own vendor, not a Snowplow / Iglu Central
one), **ask the user to paste the Iglu JSON schema** for it (or its registry URL). Not strictly
required — the recovery column's `full_data_type` and Phase 3.5 profiling ground the recovery, and this
replaces none of them — but it's authoritative context the user can reasonably provide, letting you
sanity-check field names/types/`required`/enums, catch a renamed/misspelled field, and tell a
genuinely-bad value from a wrong target type. Extra confidence, not a substitute. (Iglu Central schemas
you can read directly.)

### Phase 3 — Inspect the payload
Run the **payload inspection query** (`LATERAL VIEW explode` + `GET_JSON_OBJECT`) so the user sees the
real value behind each field. Confirm exact field names and values, and note how each maps into the
repaired entity. Expect failures to be **compound**: one schema's failed events can combine a
renamed/missing field *and* a wrong type. Remember `GET_JSON_OBJECT` renders every scalar as a string
and can silently NULL a bad path — so this sample orients you but proves nothing.

### Phase 3.5 — Profile & confirm coercibility (gate — do not skip)
Before building any recovery expression, run the **field profiling query** (`templates/recovery-sql.md`)
— it can't error (`get_json_object`, `try_cast`, `count_if`) — to learn, per field across *all* failed
rows, whether every value casts to its target type and whether the field is always present. This is how
you pre-empt Databricks' **silent-NULL trap**: confirm coercibility up front rather than discovering
NULLs after the write. Decide, per field: any `_uncoercible > 0` → **stop and partition/scope**; any
`_absent > 0` → required→partition, optional→NULL; otherwise the population is uniform and
`CAST(GET_JSON_OBJECT(...) AS <type>)` will produce a value for every row.
Decision table: `reference/strict-typing-and-classification.md`.

### Phase 4 — Build & emit the test SELECT
Do these **in order, one query at a time** — never batch them, and never write a query with a guessed
value:
1. Emit **only** the "Finding the recovery column name" query. Wait for the result and read back the
   exact `column_name` and its `full_data_type` (which names every struct field, type, and order).
   Don't write any query that references the recovery column until you have this — no guessing it from
   the Iglu URI.
2. If the `full_data_type` plus the Phase-3.5 profiling leaves the shape ambiguous, then emit the
   "Inspect a validated event" query (written with that **real** column name) or ask for the Iglu
   schema JSON.

Only now build the recovery expression to match `full_data_type` field-for-field (`_schema_version`
first, mandatory), reconstructing as `ARRAY(NAMED_STRUCT(...))` with each field
`CAST(GET_JSON_OBJECT(...) AS <type>)` (`reference/failure-context.md`). Emit the **test SELECT**
(event_id + reconstructed column). **Do not generate the INSERT yet** — have the user run it and paste
output back. Because a clean run doesn't prove correctness here, also offer the **optional per-field
null-count check**.

### Phase 5 — Validate (gate — do not skip)
Check the pasted output against the validation gate in `reference/strict-typing-and-classification.md`:
**no unexpected NULLs** (decisive — Databricks NULLs silently, so this, not "the query ran", is the
primary signal), values corrected, struct shape matches `full_data_type` (`_schema_version` first),
row count matches. If anything fails, fix the expression and re-emit the test SELECT (back to Phase 4).
Diagnostics: `troubleshooting/recovery-issues.md`.

### Phase 6 — Choose the write path
Ask the user which write path they want, **spelling out both options and the recommendation** — don't
ask a bare "how do you want to write these back?". Use words like:

> "Validation passed. How would you like to write the recovered rows?
> **1. Via a staging table (recommended)** — I insert into a temporary copy of your good table first,
> you inspect the recovered rows there, then we promote them into the good atomic events table in one
> step. Safest, and easy to abandon if anything looks off.
> **2. Directly into the good table** — insert straight into `<good table>`. Faster, but the rows are
> live immediately (Delta time travel is still available to compare before/after)."

If they pick staging, create it with `CREATE TABLE ... AS SELECT * ... LIMIT 0` (CTAS mirrors the good
table's columns/types/order exactly; `CREATE TABLE ... LIKE` is equivalent). The chosen target becomes
the INSERT target; column classification always comes from the **good** table.

### Phase 7 — Generate the INSERT (local script)
Assemble the wide INSERT with **`scripts/generate_insert.py`** so no quote-laden SQL is copied out of
a result cell. Do these one query at a time (see `templates/recovery-sql.md` for exact SQL and the
full script invocation):
1. Have the user run the two **column-metadata queries** (good and failed tables'
   `information_schema.columns`, `ORDER BY ordinal_position`) and paste each result — plain
   `column_name, full_data_type`, nothing quote-sensitive, so the copy is lossless.
2. Write those pastes, a `config.json` (table identifiers, INSERT target, recovery column, failing
   schema URI, app_id, window), and the **validated Phase-4 recovery expression** (single quotes stay
   **single** — no doubling; it isn't embedded in a string literal) to files, and run the script. It
   prints a sanity summary (column counts, pass-through, nulled) and writes the full INSERT to a
   `.sql` file.

Review the generated INSERT with the user (recovery column carries the expression, failure column
nulled, `load_tstamp` refreshed, positional column order matches the good table). If the script warns
the recovery column wasn't matched, or the null-cast on the failure column is rejected, see
`troubleshooting/recovery-issues.md`.

**Fallback (no local Python):** the **in-warehouse INSERT-generator query** (`templates/recovery-sql.md`)
builds the same INSERT as a single result cell — but warn the user that Databricks' plain-text copy can
strip its single quotes; they must copy from the expanded/raw cell and confirm the quotes survived
before running.

### Phase 8 — Run, validate, promote
If the duplicate pre-check was skipped in Phase 2 and there's any doubt, offer it once more. Run the
INSERT into the chosen target; validate what landed (row count + spot-check).
- **Direct path:** done. Optionally compare before/after with Delta time travel (`TIMESTAMP AS OF` /
  `VERSION AS OF`, in `templates/recovery-sql.md`).
- **Staging path:** promote (`INSERT INTO <good> SELECT * FROM <staging>`), re-validate, then offer
  to `DROP TABLE` the staging table.

---

## Recovering a ResolutionError (schema couldn't be resolved)

This flow is **not** limited to `ValidationError` cases. A **ResolutionError** means Iglu couldn't
*resolve* (fetch) the referenced schema at all; a **ValidationError** means the schema resolved but
the data didn't conform. Tell them apart in Phase 2/3 from the `failure_type` and the `errors`
content — and do **not** treat a ResolutionError as automatically unrecoverable.

A ResolutionError **is** recoverable in-warehouse in these cases:

- **Transient / misconfigured registry** — the schema was valid; Iglu was just unreachable. The
  payload already conforms; reconstruct it into the correct column unchanged.
- **Wrong URI** — a typo in the vendor/name, or a version that doesn't exist. Re-point to the
  corrected URI, reconstruct into *that* schema's column, and adjust any fields the correct schema
  requires (exactly as in a ValidationError recovery).
- **Never published** — register the schema in Iglu first (outside the warehouse), then recover.

Two things must be true before the INSERT can land — confirm both:

1. **The correct schema now resolves** — same URI if transient, the corrected URI if it was wrong,
   or a newly registered one.
2. **A target column exists** in the good table for the correct schema. Loaders materialise an
   entity's column only after a valid event of that schema has loaded, so a schema that never
   resolved may have **no column yet**. Confirm with the "finding the recovery column name" query
   (`templates/recovery-sql.md`) run against the **corrected** schema name — a returned row is your
   confirmation. If none comes back, the schema must load once (or the column be created) first.

**Key subtlety — filter on the recorded URI, build into the corrected one:**

- **Filter** the failed rows by the schema URI **as stored in the failure context**
  (`contexts_com_snowplowanalytics_snowplow_failure_1[0].schema`) — the original, possibly-wrong URI
  the event carried. Every WHERE predicate (drill-in, payload, test `prep`, INSERT `prep`, duplicate
  pre-check) must match on that recorded value.
- **Derive** the target column and `_schema_version` from the **corrected** URI.
- Same URI (transient) → nothing else changes. Different (typo / version fix) → keep the predicates
  on the recorded URI but reconstruct into the corrected column/version.

**Only bail out** when the schema genuinely can't be made to resolve or no target column can exist.
Diagnostics: `troubleshooting/recovery-issues.md`.

---

## When this skill is *not* the right answer

- **The event failed for a reason a data fix can't touch** — enrichment failures, adapter failures,
  or a genuinely malformed event are out of scope; diagnose the `failure_type` first. (A
  **ResolutionError** is *not* automatically excluded — see "Recovering a ResolutionError" above.)
- **The underlying values are genuinely unrecoverable** (not just mis-typed) — if profiling shows
  `_uncoercible > 0` and the values can't cast to the target type, they may need fixing at the
  tracker/source, not backfilled.
- **The schema itself is wrong** and should be evolved — recovering into the old shape may be the
  wrong move; consider a schema version bump instead.
- **A different warehouse** — use the sibling recovery skill for Snowflake, Athena/Iceberg, or
  BigQuery; the dialect and load mechanics differ.

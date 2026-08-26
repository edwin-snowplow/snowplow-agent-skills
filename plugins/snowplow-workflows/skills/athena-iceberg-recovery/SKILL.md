---
name: athena-iceberg-recovery
description: Recover Snowplow failed events from an Athena Iceberg / Glue failed events table back into the good atomic events table by generating the complete SQL the user runs — identification, payload inspection, a validation-gated test SELECT, the full INSERT, and post-checks. Use to reprocess, reinsert, or repair schema-validation failed events in Athena, especially events that failed on a single custom entity/context. Handles atomic vs custom columns, Hive to Athena type conversion, and reserved-word quoting, and builds the wide INSERT deterministically with a bundled Python generator. Triggers: recover failed events in Athena, reprocess Athena bad rows, reinsert Iceberg schema-validation failures, recover failed events into the Athena atomic events table, Athena Iceberg failed event recovery, recover Glue failed events with SQL.
---

# Athena Iceberg failed-events recovery

Recover Snowplow events that failed schema validation on a custom entity — moving them from a failed
events table back into the good atomic events table — by generating the **complete SQL the user
runs**. Both tables are Iceberg tables in AWS Glue, queried via Athena (Presto/Trino); the failed
events table stores the failure context as a native `ARRAY(ROW(...))` whose `data` field is a JSON
string (`VARCHAR`).

You are the whole recovery system, from first diagnosis to a ready-to-run INSERT. This is
**human-in-the-loop**: you generate queries, the user runs them and pastes results back, and you use
those to generate the next step. You cannot run Athena yourself. Three facts can only come from
running a query, so you always need results fed back before finalising downstream:

- **what's failing** (the identification query),
- the actual **`data` field values/names** (the payload inspection query), and
- the two tables' **column sets** (`DESCRIBE`), needed to build the INSERT column list.

**Three rules that shape everything:**
- **Never ask the user to confirm a fact a query could establish** — the column name, field names,
  values, schema version, whether rows already exist. If it lives in the warehouse, generate the query.
- **The repaired entity's shape and types are defined by the Iglu schema, not chosen by you.** Read
  them from ground truth (the good-table `DESCRIBE`, a validated good-table row, or the schema JSON) —
  never guess.
- **One step at a time — never hand over a query (or `DESCRIBE`) you'd expect the user to edit.** Emit
  a single query (or one tightly-scoped step), wait for the result, then use it to build the next
  step. When a step needs a value another step establishes — the recovery column name, a field's type
  from the `DESCRIBE` — get that first and substitute the real value **yourself**. Never emit SQL
  containing a guessed or placeholder value with a "swap this in if it differs" note, never batch a
  query together with the one whose output it depends on, and never ask the user to edit the SQL you
  provide.

One run recovers **one failing schema** for **one app_id**. If the user has several, do them one at a
time.

## Where the knowledge lives (read the form you need, when you need it)

- **Plain-language overview (help centre):** a customer-facing summary of what this skill does — the
  process, the safety gates, what it can and can't recover — is published at
  https://support.snowplow.io/articles/5225708172-Recovering-failed-events-in-the-warehouse-with-Claude.
  Share that link if the user asks "what does this do?".

- **`templates/recovery-sql.md`** — every SQL statement, with placeholders. The thing you actually
  hand the user at each phase.
- **`reference/failure-context.md`** — the failure column's structure and how to navigate/extract it
  with `UNNEST` + `json_extract_scalar` (and *why* a clean run proves the casts), plus grounding the
  target shape in the good-table `DESCRIBE`.
- **`reference/column-classification-and-validation.md`** — how the generator classifies every column,
  Hive→Athena type conversion, reserved-word quoting, the Phase-3.5 profiling decision, and the
  `typeof()` validation gate.
- **`examples/worked-recovery.md`** — a complete run end to end (string→BIGINT), to pattern-match against.
- **`troubleshooting/recovery-issues.md`** — symptom → cause → verify → fix for when a step goes wrong.
- **`scripts/generate_insert.py`** — the bundled generator that builds the wide INSERT deterministically (Phase 7).

Read the relevant form at each phase rather than working from memory — they encode edge cases
confirmed against real Snowplow tables.

## What makes Athena specific

- **The INSERT is built by a bundled script.** There is no INFORMATION_SCHEMA path here — columns are
  classified from the two tables' `DESCRIBE` output, and `scripts/generate_insert.py` implements the
  classification, Hive→Athena type conversion, and reserved-word quoting deterministically (Phase 7).
- **Native `ARRAY(ROW(...))`, reconstructed with `CAST(ARRAY[ROW(...)] AS ARRAY(ROW(...)))`** — never
  the DDL `ARRAY<STRUCT<...>>` form that DESCRIBE prints. Field names, types, and order must match the
  good-table column exactly (`reference/column-classification-and-validation.md`).
- **JSON string payload — `json_extract_scalar` + `CAST`.** `data` is a JSON string, and Athena can't
  dot-navigate the array in a SELECT, so you `UNNEST` first then `json_extract_scalar(failure_data,
  '$.field')`, wrapping in `CAST(... AS <type>)` to land a typed value. Profiling (Phase 3.5) confirms
  every value coerces (`reference/failure-context.md`).
- **1-indexed array access.** `contexts_..._failure_1[1].schema` (Trino/Presto arrays are 1-based).

---

## Prerequisites

- **Python 3.6+ available locally.** Phase 7 runs the bundled `scripts/generate_insert.py` to build
  the INSERT deterministically. It is standard-library only — no packages to install.
- **In Claude Desktop:** the user must enable **"Code execution and file creation"** (Settings →
  Capabilities) so the bundled script can run. It runs locally, off by default. In Claude Code this is
  available already.
- If code execution is unavailable, fall back to generating the INSERT inline per the reference rules
  and the Full INSERT template (workable, but less reliable on wide tables — prefer the script).

---

## The 8-phase flow (the decision tree)

Carry one **time window** through every query so the identified, validated, and recovered sets match
(see "Time window" in `templates/recovery-sql.md`). The tables are partitioned by `load_tstamp_day`, so
prefer a bounded `load_tstamp` range; the generator takes `LOAD_TSTAMP_START`/`LOAD_TSTAMP_END` (or
`DAYS_LOOKBACK`).

### Phase 1 — Gather tables, offer to help identify
Ask for both tables up front (`database.table` each) and when the failures happened, in the opening
message, so no placeholders linger:

> "First, what are your failed and good events tables (database.table each), and roughly when did the
> failures happen (a date or range — helps me scan less)? If you're not sure, I'll use a lookback
> window. And do you want help finding and identifying recent failed events, or do you already know
> the failing schema and app_id you want to recover?"

Then: help identifying → Phase 2. Already know the target → collect the failing schema Iglu URI,
app_id, and target entity column → Phase 3. From here on, refer to the tables by the names given —
never re-ask.

### Phase 2 — Identify the failures
Lead with the **summary identification query**; read off which schemas fail, at what volume, which
`failure_type`, which app_ids. Have the user pick **one** schema; run the **row-level drill-in** to
form a fix hypothesis — including whether it's a schema **resolution** failure rather than a validation
one (see "Recovering a ResolutionError" below). The drill-in also surfaces **`source`** — read it from
the nested `errors[…].source` path (never the top-level `source`, which doesn't exist): a mix of
`contexts` and `unstruct` means the schema failed both as a context entity **and** as the
self-describing event, which recover into **different columns** and must be **separate runs** (see
`reference/failure-context.md`).

**Offer the duplicate pre-check now (optional).** As soon as the schema is chosen — and before building
any recovery expression — **explicitly ask** whether they want to confirm these events haven't already
been recovered; don't silently skip it:

> "Want me to check whether these events have already been recovered before? You can skip this if you
> know this schema has never been recovered."

If they want it, give the **duplicate pre-check** query (`templates/recovery-sql.md`). Expect **0** — if
> 0, list the overlapping `event_id`s and decide with the user (stop, narrow scope, or proceed
knowingly). If they skip, note it and move on.

Then, if the failing schema is a **custom schema** (your own vendor, not a Snowplow / Iglu Central
one), **ask the user to paste the Iglu JSON schema** for it (or its registry URL). Not strictly
required — the good-table `DESCRIBE` and Phase 3.5 profiling ground the recovery, and this replaces
none of them — but it's authoritative context the user can reasonably provide, letting you sanity-check
field names/types/`required`/enums, catch a renamed field, and tell a genuinely-bad value from a wrong
target type. Extra confidence, not a substitute. (Iglu Central / Snowplow schemas you can reason from
directly.)

### Phase 3 — Inspect the payload
Run the **payload inspection query** (`UNNEST` + `json_extract_scalar`) so the user sees the real value
behind each field. Confirm exact field names and values, and note **how each value is encoded** (a
quoted number is a string-encoded int). Expect failures to be **compound**: one schema's failed events
can combine a renamed/missing field *and* a wrong type. The real target types come from the good-table
`DESCRIBE`.

### Phase 3.5 — Profile & confirm coercibility (gate — do not skip)
Before building any recovery expression, run the **field profiling query** (`templates/recovery-sql.md`)
— it can't error (`json_extract_scalar`, `TRY_CAST`, `count_if`) — to learn, per field across *all*
failed rows, whether every value casts to its target type and whether it's always present. Use it to
(a) confirm the whole population shares **one** recovery, and (b) know the extractor is safe:

- **any field `_uncoercible > 0`** → the failed rows don't share one recovery — **stop**, surface the
  offending rows, and partition/scope with the user;
- **any field `_absent > 0`** → missing from some rows — required→partition, optional→NULL;
- **otherwise** → build the expression with `CAST(json_extract_scalar(...) AS <type>)`, which coerces
  native and string-encoded values and errors (never silent-NULLs) on a bad one.

Never use `TRY_CAST` in the recovery expression itself. Decision table:
`reference/column-classification-and-validation.md`.

### Phase 4 — Build & emit the test SELECT
Do these **in order, one step at a time** — never build the expression from a guessed column name or
field type:
1. Ask the user to paste the **good-table `DESCRIBE`** (`DESCRIBE <good_database>.<good_table>` —
   don't re-ask for the name). From it, read the exact recovery column and its `ROW(...)` field names,
   types, and **order**. If the entity isn't in the DESCRIBE yet (brand-new schema), fall back to the
   Iglu schema JSON.
2. Only now build the **recovery expression**, mapping each `json_extract_scalar(failure_data, '$.field')`
   to its target field/type in the good-table struct's order. The shape depends on the Phase-2 **source**:
   - **contexts** → `CAST(ARRAY[ROW(...)] AS ARRAY(ROW(...)))`, `_schema_version` first (the `contexts_*`
     struct lists it).
   - **unstruct** → a single `CAST(ROW(...) AS ROW(...))` with **no outer `ARRAY[...]`/`ARRAY(...)`**,
     into the `unstruct_event_*` column. Its DESCRIBE struct usually has **no `_schema_version`** field —
     omit it unless DESCRIBE lists it. (Never the DDL `ARRAY<STRUCT<...>>`/`STRUCT<...>` form — that's
     invalid in a query.) Both forms in `templates/recovery-sql.md`.

Emit the **test SELECT** (event_id + reconstructed column + `typeof(...)`). **Do not generate the
INSERT yet** — have the user run it and paste output back.

### Phase 5 — Validate (gate — do not skip)
Check the pasted output against the validation gate in
`reference/column-classification-and-validation.md`: **`typeof()` equals the good-table column's type
field-for-field** (the decisive check — if it matches, the INSERT type-checks), row count matches, no
unexpected nulls, values corrected. If anything fails, fix the expression and re-emit the test SELECT
(back to Phase 4). Diagnostics: `troubleshooting/recovery-issues.md`.

### Phase 6 — Choose the write path
Ask the user which write path they want, **spelling out both options and the recommendation** — don't
ask a bare "how do you want to write these back?". Use words like:

> "Validation passed. How would you like to write the recovered rows?
> **1. Via a staging table (recommended)** — I insert into a temporary copy of your good table first,
> you inspect the recovered rows there, then we promote them into the good atomic events table in one
> step. Safest, and easy to abandon if anything looks off.
> **2. Directly into the good table** — insert straight into `<good table>`. Faster, but the rows are
> live immediately."

If they pick staging, create it with the `CREATE TABLE ... AS SELECT * FROM <good> WHERE 1 = 0`
pattern (mirrors columns/order exactly; ask for an S3 location and a staging table name). The chosen
target becomes the INSERT target; column classification always comes from the **good-table** DESCRIBE.

### Phase 7 — Generate the INSERT (bundled script builds it)
You need **both `DESCRIBE` outputs** to classify columns — request the failed-table one now if you
don't have it. **Generate with `scripts/generate_insert.py`** (under this skill's directory) — it
implements the column-classification and type-conversion rules exactly, so the wide column list is
deterministic:

1. Write four input files (in a temp/working dir):
   - `config.json` — keys `GOOD_DATABASE`, `GOOD_TABLE`, `FAILED_DATABASE`, `FAILED_TABLE`,
     `RECOVERY_COLUMN` (the `contexts_*` or `unstruct_event_*` column), `FAILED_SCHEMA`, `APP_ID`,
     `DAYS_LOOKBACK`. Set **`FAILURE_SOURCE`** (`contexts`/`unstruct`) when the schema failed both ways —
     it scopes `prep` to that source, so each source recovers into its own column in a separate run;
     omit for a single-source schema. **For the staging path**, also set `TARGET_DATABASE`/`TARGET_TABLE`
     to the staging table (defaults to the good table if omitted).
   - `good_describe.txt` / `failed_describe.txt` — the raw pasted DESCRIBE outputs
   - `recovery_expression.sql` — the validated recovery expression (no trailing `AS <column>`)
2. Run:
   ```
   python3 <skill_dir>/scripts/generate_insert.py \
       --config config.json --good-describe good_describe.txt \
       --failed-describe failed_describe.txt --recovery-expr recovery_expression.sql
   ```
   It prints the INSERT to stdout and a sanity summary (column counts, pass-through, nulled) to stderr.
   Add `--out recovery.sql` to write a file instead of printing.

If code execution is unavailable, build the statement inline from the Full INSERT template and the
reference rules. **Show the statement inline by default** (in a code block) — offer to write
`recovery.sql`, leaning toward it only when the statement is large or the user wants to re-run/diff it.
Relay the sanity summary; warn if the target/recovery column wasn't found in the good DESCRIBE.
Classification details and failure modes: `reference/column-classification-and-validation.md`,
`troubleshooting/recovery-issues.md`.

### Phase 8 — Run, validate, promote
If the duplicate pre-check was skipped in Phase 2 and there's any doubt, offer it once more. Run the
INSERT into the chosen target; validate what landed (row count + spot-check the recovered column).
- **Direct path:** done. Optionally compare before/after with Iceberg `FOR TIMESTAMP AS OF` time travel
  (Athena engine v3 — `templates/recovery-sql.md`).
- **Staging path:** promote (`INSERT INTO <good> SELECT * FROM <staging>`), re-validate, then offer to
  `DROP TABLE` the staging table.

---

## Recovering a ResolutionError (schema couldn't be resolved)

This flow is **not** limited to `ValidationError` cases. A **ResolutionError** means Iglu couldn't
*resolve* (fetch) the referenced schema at all; a **ValidationError** means the schema resolved but the
data didn't conform. Tell them apart in Phase 2/3 from the `failure_type` and the `errors` content —
and do **not** treat a ResolutionError as automatically unrecoverable.

A ResolutionError **is** recoverable in-warehouse in these cases:

- **Transient / misconfigured registry** — the schema was valid; Iglu was just unreachable at
  enrichment time. The payload already conforms; reconstruct it into the correct column unchanged.
- **Wrong URI** — a typo in the vendor/name, or a version that doesn't exist. Re-point to the corrected
  URI, reconstruct into *that* schema's column, and adjust any fields the correct schema requires
  (exactly as in a ValidationError recovery).
- **Never published** — register the schema in Iglu first (outside the warehouse), then recover.

Two things must be true before the INSERT can land — confirm both:

1. **The correct schema now resolves** — same URI if transient, the corrected URI if it was wrong, or a
   newly registered one.
2. **A target column exists** in the good table for the correct schema. Loaders materialise an entity's
   column only after a valid event of that schema has loaded, so a schema that never resolved may have
   **no column yet**. Confirm the **corrected** entity column appears in the good-table `DESCRIBE` — its
   presence is your confirmation. If it's absent, the schema must load once (or the column be created)
   first.

**Key subtlety — filter on the recorded URI, build into the corrected one:**

- **Filter** the failed rows by the schema URI **as stored in the failure context**
  (`contexts_com_snowplowanalytics_snowplow_failure_1[1].schema`) — the original, possibly-wrong URI
  the event carried. Every WHERE predicate (drill-in, payload, test `prep`, INSERT `prep`, duplicate
  pre-check) must match on that recorded value.
- **Derive** the target column and `_schema_version` from the **corrected** URI (and pass the corrected
  `FAILED_SCHEMA`/recovery column to the generator).
- Same URI (transient) → nothing else changes. Different (typo / version fix) → keep the predicates on
  the recorded URI but reconstruct into the corrected column/version.

**Only bail out** when the schema genuinely can't be made to resolve or no target column can exist.
Diagnostics: `troubleshooting/recovery-issues.md`.

---

## Recovering an unstruct (self-describing event) failure

Same 8-phase flow, targeting the **self-describing event** instead of a context entity. The drill-in
`source = unstruct` means the schema failed as the event body, recovering into the
`unstruct_event_<vendor>_<name>_<major>` column. Four things differ from the contexts path — everything
else (identification, profiling gate, the script, pass-through, validation) is identical:

1. **Target column is a single `ROW`, not `ARRAY(ROW(...))`.** Read its field names/types/order from the
   good-table `DESCRIBE`.
2. **Reconstruct with a single `CAST(ROW(...) AS ROW(...))`** — no outer `ARRAY[...]`/`ARRAY(...)`.
3. **No `_schema_version`.** The `unstruct_event_*` DESCRIBE struct usually omits it; match the struct.
4. **Inspection/validation read the row directly** — `<recovery_column>` / `<recovery_column>.field`,
   no `[1]` and no `cardinality()`.

For Phase 7, set the script's `FAILURE_SOURCE = unstruct` and `RECOVERY_COLUMN` to the `unstruct_event_*`
column. A schema that failed **both** ways is two separate runs, one per source. A valid entity the event
also carried (a passing `contexts_*`) is preserved automatically via pass-through.

---

## When this skill is *not* the right answer

- **The event failed for a reason a data fix can't touch** — enrichment failures, adapter failures, or
  a genuinely malformed event are out of scope; diagnose the `failure_type` first with the `debug-failed-events` skill. (A **ResolutionError**
  is *not* automatically excluded — see "Recovering a ResolutionError" above.)
- **The underlying values are genuinely unrecoverable** (not just mis-typed) — if the cast can't coerce
  the values (`_uncoercible > 0`), they may need fixing at the tracker/source, not backfilled.
- **The schema itself is wrong** and should be evolved — recovering into the old shape may be the wrong
  move; consider a schema version bump instead.
- **A different warehouse** — use the sibling recovery skill for BigQuery, Snowflake, or Databricks;
  the dialect and load mechanics differ.

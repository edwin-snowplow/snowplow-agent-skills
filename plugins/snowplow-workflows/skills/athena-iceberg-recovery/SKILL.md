---
name: athena-iceberg-recovery
description: Recover Snowplow failed events from an Athena Iceberg / Glue failed events table back into the good atomic events table by generating the complete SQL the user runs — identification, payload inspection, a validation-gated test SELECT, the full INSERT, and post-checks. Use to reprocess, reinsert, or repair schema-validation failed events in Athena, especially events that failed on a single custom entity/context. Handles atomic vs custom columns, Hive→Athena type conversion, and reserved-word quoting, and builds the wide INSERT deterministically with a bundled Python generator.
---

# Athena Iceberg failed-events recovery

Generate the **complete set of SQL** a user needs to recover Snowplow events that failed schema
validation on a custom entity, moving them from a failed events table back into the good atomic
events table. Both tables are Iceberg tables in AWS Glue, queried via Athena (Presto/Trino).

Your job is to be the whole recovery system, from first diagnosis to a ready-to-run INSERT. Start
by offering to help the user *find and identify* recent failures — don't assume they already know
what's failing. Then walk them from a chosen failure to the complete SQL runbook that recovers it.

You cannot run Athena yourself, so this is human-in-the-loop: you generate queries, the user runs
them and pastes results back, and you use those to generate the next step. Three facts can only
come from running a query, so you always need results fed back before you can finalise downstream:

- **what's failing** (revealed by the identification query),
- the actual **`data` field values/names** (revealed by the payload inspection query), and
- the two tables' **column sets** (revealed by `DESCRIBE`), needed to build the INSERT column list.

At any SQL-generation step, **read `reference/sql-reference.md`** (in this skill's folder) and apply
its column-classification, type-conversion, reserved-word, and template rules exactly — they encode
edge cases confirmed against real Snowplow tables. Do not reconstruct them from memory.

One run recovers **one failing schema** for **one app_id**. If the user has several, do them one at
a time.

---

## Prerequisites

- **Python 3.6+ available locally.** Phase 6 runs the bundled `scripts/generate_insert.py` to build
  the INSERT deterministically. It is standard-library only — no packages to install.
- **In Claude Desktop:** the user must enable **"Code execution and file creation"** (Settings →
  Capabilities) so the bundled script can run. It runs locally, off by default. In Claude Code this
  is available already.
- If code execution is unavailable, fall back to generating the INSERT inline per the reference
  rules (workable, but less reliable on wide tables — prefer the script).

---

## Phase 1 — Start: gather tables, then offer to help identify the failures

**Ask for both tables up front, in the opening message**, so every later query and prompt already
has the real names — never leave `<good_database>.<good_table>` placeholders to resolve mid-flow:

- **Failed events** database + table (recover *from*; table usually `events`)
- **Good events** database + table (recover *into*; table usually `events`)
- **When the failures occurred** — a date or rough range. The tables are partitioned by
  `load_tstamp_day`, so a bounded range scans far less than a broad lookback. If they don't know,
  fall back to a lookback window (default 7 days) and start narrow.

In the same message, ask whether they want help finding the failures or already know the target:

> "First, what are your failed and good events tables (database.table each), and roughly when did
> the failures happen (a date or range — helps me scan less)? If you're not sure, I'll use a
> lookback window. And do you want help finding and identifying recent failed events, or do you
> already know the failing schema and app_id you want to recover?"

Carry this time window through **every** query (identification, inspection, test, INSERT, duplicate
pre-check) so the identified, validated, and recovered sets match — see the reference's "Time
window" note. Prefer a bounded `load_tstamp >= TIMESTAMP '…' AND load_tstamp < TIMESTAMP '…'` when
the timeframe is known; the bundled generator takes `LOAD_TSTAMP_START`/`LOAD_TSTAMP_END` for this
(or `DAYS_LOOKBACK` for the lookback fallback).

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
schema** for it (or its registry URL). This isn't strictly required (the good-table `DESCRIBE` and the
Phase 3.5 profiling already ground the recovery) and it doesn't replace any of them; it's authoritative
context the user can reasonably provide that lets you sanity-check field names, types, the `required`
list, and enums/constraints, catch a renamed/misspelled field, and judge whether an un-coercible value
is genuinely bad or a sign the target type is wrong. Treat it as extra confidence. (For Iglu Central /
Snowplow schemas you can reason from the public schema directly.)

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
   resolved may have **no column yet**. Confirm that the **corrected** entity column appears in the
   good-table `DESCRIBE` — its presence is your confirmation. If it's absent, the schema must load
   once (or the column be created) before recovery can land.

**Key subtlety — filter on the recorded URI, build into the corrected one:**

- **Filter** the failed rows by the schema URI **as stored in the failure context**
  (`contexts_com_snowplowanalytics_snowplow_failure_1[1].schema`) — the original, possibly-wrong URI
  the event actually carried. Every WHERE predicate (identification drill-in, payload, test `prep`,
  INSERT `prep`, duplicate pre-check) must match on that recorded value.
- **Derive** the target column and `_schema_version` from the **corrected** URI (and pass the
  corrected `FAILED_SCHEMA`/recovery column to the generator).
- When the two are the same (transient), nothing else changes. When they differ (typo / version
  fix), keep the predicates on the recorded URI but reconstruct into the corrected column/version.

**Only bail out** when the schema genuinely can't be made to resolve (doesn't exist and won't be
registered) or no target column can exist — then it isn't a data fix. Enrichment failures and
adapter failures remain out of scope regardless.

## Phase 3 — Inspect the payload and confirm the fix

Generate the **payload inspection query** (UNNEST + `json_extract_scalar`) so the user sees the real
values behind each field for the chosen schema. From the results, confirm the exact field names,
their values, and the intended target types — i.e. exactly how each failed field maps into the
repaired entity. This mapping drives the recovery expression in Phase 4.

---

## Phase 3.5 — Profile & confirm coercibility (gate — do not skip)

Before building any recovery expression, give the user the reference's **field profiling query** — it
can't error (`json_extract_scalar`, `TRY_CAST`) — to learn, per field across *all* failed rows,
whether every value casts to its target type and whether the field is always present. Confirm the
**whole population shares one recovery** before you suggest anything:

- **any field `_uncoercible > 0`** → some values can't cast to the target type → **stop, list the
  offending rows, and partition/scope** with the user;
- **any field `_absent > 0`** → the field is missing from some rows → required→partition, optional→NULL;
- **otherwise** → the population is uniform; build the recovery expression
  (`CAST(json_extract_scalar(...) AS <type>)` coerces native and string-encoded values, and profiling
  has confirmed none will error).

---

## Phase 4 — Build and emit the test SELECT

You need the **good-table `DESCRIBE`** to read the target entity's exact field names, types, and
**order**. Ask the user to paste the output of `DESCRIBE <good_database>.<good_table>` using the
table name they already gave in Phase 1 (don't re-ask for the name). Then build the **recovery
expression**: the
`CAST(ARRAY[ROW(...)] AS ARRAY(ROW(...)))` for the target column, mapping each
`json_extract_scalar(failure_data, '$.field')` to its target field/type in the same order as the
good-table struct. (Always the `CAST(ARRAY[ROW(...)] AS ARRAY(ROW(...)))` form, never the DDL
`ARRAY<STRUCT<...>>`.)

**The target field types are defined by the Iglu schema — not yours to decide or guess.** Here the
good-table `DESCRIBE` already carries that ground truth: the struct's field names, types, and order
are the schema materialised into the table, so cast each field to match the DESCRIBE exactly. Do not
infer types from the failed payload (its values are exactly what failed validation). If the entity
isn't in the good-table `DESCRIBE` yet (e.g. a brand-new schema with no validated events), fall back
to the Iglu schema JSON — ask the user for it or its URL and read the field types and `required` list
from there.

Emit a **test SELECT** (per the reference's test-SELECT + `typeof()` pattern) that wraps the
recovery expression in the `prep` + `unnested` CTEs and returns, for each row: `event_id`, the
reconstructed column, and `typeof(<recovery expression>)`. **Do not generate the INSERT yet.** Ask
the user to run the test SELECT and **paste the full output back**, so you can validate before
anything touches the good table.

## Phase 5 — Validate the test output (gate — do not skip)

When the user pastes the test output, validate that the reconstructed column will insert into the
good table without error, per the reference's validation checklist. In short, confirm:

- **Type match** — the `typeof()` string equals the good-table column's Athena type, field-for-field
  (names, types, and order). This is the decisive check: if it matches, the INSERT type-checks.
- **Row count** — matches the number of failed events expected for this schema + app_id.
- **No unexpected nulls** — a null where a value was expected usually means a wrong JSON path or a
  cast that silently failed; investigate before proceeding.
- **Values look right** — the repaired field(s) hold the corrected values (e.g. the string is now a
  proper integer).

State the verdict clearly. If anything fails, **fix the recovery expression and re-emit the test
SELECT (return to Phase 4)** — loop until it validates. Only advance to Phase 6 once the type
matches and the values are correct.

## Phase 6 — Choose the write path (ask before generating the INSERT)

Once the gate passes and before generating the INSERT, ask the user how they want to write:

> "Do you want to recover into a **staging table first** (recommended — you can inspect the actual
> written rows and promote them when you're happy), or **straight into the good table**?"

- **Staging table (recommended):** safer — the recovered rows land in a throwaway table you can
  query and validate before anything touches production; promote with a single INSERT when ready.
- **Straight into the good table:** faster, but writes to production immediately.

**If staging:** guide the user to create an empty staging table that mirrors the good table, using
the reference's "Create an empty staging table" pattern (ask them for an S3 location and a staging
table name). The staging table becomes the INSERT target in Phase 7.

The **INSERT target** for Phase 7 is therefore the staging table (staging path) or the good table
(direct path). Either way, columns are always classified from the **good-table** DESCRIBE.

## Phase 7 — Generate the INSERT

You need both `DESCRIBE` outputs to classify columns — request the failed-table one now if you
don't have it. **Generate with the bundled script** (`scripts/generate_insert.py`, under this
skill's directory) — it implements the reference's column-classification and type-conversion rules
exactly, so the wide column list is deterministic. Steps:

1. Write four input files (in a temp/working dir):
   - `config.json` — keys `GOOD_DATABASE`, `GOOD_TABLE`, `FAILED_DATABASE`, `FAILED_TABLE`,
     `RECOVERY_COLUMN`, `FAILED_SCHEMA`, `APP_ID`, `DAYS_LOOKBACK`. **For the staging path**, also
     set `TARGET_DATABASE`/`TARGET_TABLE` to the staging table (defaults to the good table if omitted).
   - `good_describe.txt` / `failed_describe.txt` — the raw pasted DESCRIBE outputs
   - `recovery_expression.sql` — the validated recovery expression (no trailing `AS <column>`)
2. Run:
   ```
   python3 <skill_dir>/scripts/generate_insert.py \
       --config config.json --good-describe good_describe.txt \
       --failed-describe failed_describe.txt --recovery-expr recovery_expression.sql
   ```
   It prints the INSERT to stdout and a sanity summary (column counts, pass-through, nulled) to
   stderr. Add `--out recovery.sql` to write a file instead of printing.

If code execution is unavailable, build the statement inline from the reference template and rules.

**Show the statement inline by default** (in a code block) — a file is not required. Offer to write
`recovery.sql` as an option, and lean toward it only when the statement is large (hard to scroll/copy
in chat) or the user wants to re-run or diff it later; don't drop a file into their cwd every run by
default. Relay the sanity summary. Warn if the target/recovery column wasn't found in the good
DESCRIBE.

---

## Phase 8 — Run, validate, and (staging path) promote

The duplicate pre-check is offered earlier (Phase 2). If the user **skipped** it there and is at all
unsure whether this recovery has been run before, offer it once more now (reference: "duplicate
pre-check") before inserting — running again would create duplicates. Otherwise proceed.

Advise running the INSERT into the chosen target, and:

1. **Validate what landed** — row count matches the expected failed-event count, and spot-check the
   recovered column (reference validation queries, run against the target table).

**Direct path:** that's it — the rows are in the good table. Optionally use Iceberg time travel to
compare before/after: `SELECT COUNT(*) FROM <good_db>.<good_table> FOR SYSTEM_TIME AS OF TIMESTAMP
'2024-01-15 12:00:00'`.

**Staging path:** once the staging table validates,

2. **Promote** — give the `INSERT INTO <good> SELECT * FROM <staging>` query (reference "Promote"
   pattern; column order matches because staging was cloned from good).
3. **Re-validate** the good table (row count + spot-check).
4. **Offer to clean up** — the optional `DROP TABLE <staging>` once they're satisfied.

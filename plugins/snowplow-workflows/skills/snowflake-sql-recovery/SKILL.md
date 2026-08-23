---
name: snowflake-sql-recovery
description: Recover Snowplow failed events from a Snowflake failed events table back into the good atomic events table by generating the complete SQL the user runs — identification, payload inspection, a validation-gated test SELECT, the full INSERT, and post-checks. Use to reprocess, reinsert, or repair schema-validation failed events in Snowflake, especially events that failed on a single custom entity/context. Builds the wide INSERT itself with a metadata query over INFORMATION_SCHEMA and reconstructs entities as semi-structured ARRAY/VARIANT with per-field TYPE casts (no strict struct typing, no reserved-word quoting, read from ground truth, never guessed).
---

# Snowflake failed-events recovery

Recover Snowplow events that failed schema validation on a custom entity — moving them from a failed
events table back into the good atomic events table — by generating the **complete SQL the user
runs**. Both are Snowflake tables; the failed events table stores the failure context as a
semi-structured `ARRAY`/`VARIANT` column.

You are the whole recovery system, from first diagnosis to a ready-to-run INSERT. This is
**human-in-the-loop**: you generate queries, the user runs them and pastes results back, and you use
those to generate the next step. You cannot run Snowflake yourself.

**Three rules that shape everything:**
- **Never ask the user to confirm a fact a query could establish** — the column name, field names,
  values, schema version, whether rows already exist. If it lives in the warehouse, generate the
  query.
- **The repaired entity's shape and types are defined by the Iglu schema, not chosen by you.** Read
  them from ground truth (a validated good-table row, or the schema JSON) — never guess.
- **One step at a time — never hand over a query you'd expect the user to edit.** Emit a single query
  (or one tightly-scoped step), wait for the result, then use it to build the next step. When a query
  needs a value another query establishes — the recovery column name, a field's type — run that query
  first and substitute the real value **yourself**. Never emit a query containing a guessed or
  placeholder value with a "swap this in if it differs" note, never batch a query together with the
  one whose output it depends on, and never ask the user to edit the SQL you provide.

One run recovers **one failing schema** for **one app_id**. If the user has several, do them one at a
time.

## Where the knowledge lives (read the form you need, when you need it)

- **`OVERVIEW.md`** — a plain-language summary of what this skill does, for the **user** (not recovery
  instructions). If they want to understand the process, or ask "what does this do?", share or read from it.

- **`templates/recovery-sql.md`** — every SQL statement, with placeholders. The thing you actually
  hand the user at each phase.
- **`reference/failure-context.md`** — the failure column's VARIANT structure and how to
  navigate/extract it with `:` and `::TYPE` (and *why* a clean run proves the casts).
- **`reference/column-classification-and-validation.md`** — how the generator classifies every
  column, the Phase-3.5 profiling decision, and the Phase-5 validation gate.
- **`examples/worked-recovery.md`** — a complete run end to end (string→INTEGER), to pattern-match against.
- **`troubleshooting/recovery-issues.md`** — symptom → cause → verify → fix for when a step goes wrong.

Read the relevant form at each phase rather than working from memory — they encode edge cases
confirmed against real Snowplow tables.

## What makes Snowflake specific

- **The INSERT builds itself — no script.** A single metadata query joins the good and failed tables'
  `INFORMATION_SCHEMA.COLUMNS`, classifies each column, and emits the full INSERT as text. This
  replaces the Athena skill's `generate_insert.py` entirely.
- **No strict struct typing.** Entity columns are semi-structured `ARRAY`/`VARIANT`; you reconstruct
  with `ARRAY_CONSTRUCT(OBJECT_CONSTRUCT(...))` and `::TYPE` casts. There is **no** field-order
  requirement, **no** reserved-word quoting of keys (they're plain string literals), and **no**
  Hive→engine type conversion (`reference/column-classification-and-validation.md`).
- **`::TYPE` coerces and errors — profile, then cast per field.** A `::TYPE` cast coerces both native
  and string-encoded values (an int sent as `"5"` still casts) and **errors** on a genuinely bad value
  rather than silent-NULLing. Profiling (Phase 3.5) confirms per field that the whole population
  coerces, so the built casts are guaranteed to run clean. Never use `TRY_CAST` in the recovery
  expression — it hides bad values (`reference/failure-context.md`).
- **Zero-based array access, `:` navigation.** `contexts_..._failure_1[0]:data:field` (Athena is
  1-indexed; Snowflake is 0-indexed).

---

## The 8-phase flow (the decision tree)

Carry one **time window** through every query so the identified, validated, and recovered sets match
(see "Time window" in `templates/recovery-sql.md`).

### Phase 1 — Gather tables, offer to help identify
Ask for both tables up front (Snowflake `DATABASE.SCHEMA.TABLE`, all three parts each) and when the
failures happened, in the opening message, so no placeholders linger:

> "First, what are your failed and good events tables (database.schema.table each), and roughly when
> did the failures happen (a date or range — helps me scan less)? If you're not sure, I'll use a
> lookback window. And do you want help finding recent failed events, or do you already know the
> failing schema and app_id you want to recover?"

Then: help identifying → Phase 2. Already know the target → collect the failing schema Iglu URI,
app_id, and target entity column → Phase 3.

### Phase 2 — Identify the failures
Lead with the **summary identification query**; read off which schemas fail, at what volume, which
`failure_type`, which app_ids. Have the user pick **one** schema; run the **row-level drill-in** to
form a fix hypothesis — including whether it's a schema **resolution** failure rather than a
validation one (see "Recovering a ResolutionError" below). The drill-in also surfaces **`source`**
(read it via `errors[0]:source`, never the top-level `[0]:source`): if the rows mix `contexts` and
`unstruct`, the schema failed both as a context entity and as the self-describing event — those recover
into different columns and must be **separate runs**, so scope to one `source` (see
`reference/failure-context.md`).

**Offer the duplicate pre-check now (optional).** As soon as the schema is chosen — and before
building any recovery expression — **explicitly ask** whether they want to confirm these events
haven't already been recovered; don't silently skip it:

> "Want me to check whether these events have already been recovered before? You can skip this if
> you know this schema has never been recovered."

If they want it, give the **duplicate pre-check** query (`templates/recovery-sql.md`). Expect **0** —
if > 0, list the overlapping `event_id`s and decide with the user (stop, narrow scope, or proceed
knowingly). If they skip, note it and move on.

**Ask for the schema JSON (custom schemas).** Then, if the failing schema is a **custom schema** (your
own vendor, not a Snowplow / Iglu Central one), **ask the user to paste the Iglu JSON schema** for it
(or its registry URL). Not strictly required — a validated good row and Phase-3.5 profiling ground the
recovery, and this replaces none of them — but it's authoritative context the user can reasonably
provide, letting you sanity-check field names/types/`required`/enums, catch a renamed field, and tell
a genuinely-bad value from a wrong target type. Extra confidence, not a substitute. (Iglu Central
schemas you can read directly.)

### Phase 3 — Inspect the payload
Run the **payload inspection query** (`LATERAL FLATTEN` + `:` navigation) so the user sees the real
value behind each field. Confirm exact field names and values, and note **how each value is encoded**
(a quoted number is a string-encoded int). Expect failures to be **compound**: one schema's failed
events can combine a renamed/missing field *and* a wrong type.

### Phase 3.5 — Profile & confirm coercibility (gate — do not skip)
Before building any recovery expression, run the **field profiling query** (`templates/recovery-sql.md`)
— it can't error (`TYPEOF`, `TRY_CAST`, `COUNT_IF`) — to learn, per field across *all* failed rows, the
VARIANT types present and whether every value casts to its target type. Use it to (a) confirm the whole
population shares **one** recovery, and (b) decide per field: **build the `::TYPE` cast** once
`_uncoercible = 0`, **stop & partition** where anything won't coerce, and treat `_absent` per whether
the schema requires the field. Never use `TRY_CAST` in the recovery expression. Decision table:
`reference/column-classification-and-validation.md`.

### Phase 4 — Build & emit the test SELECT
Do these **in order, one query at a time** — never batch them, and never fill a downstream query with
a guessed value:
1. Emit **only** the "Finding the recovery column name" query. Wait for the result and read back the
   exact `column_name` and its `data_type`. Do not write any query that references the recovery column
   until you have this — no guessing the name from the Iglu URI.
2. Then emit the "Inspect a validated event" query written with that **real** column name (or, if no
   validated row exists yet, fall back to the Iglu schema JSON). This grounds the target field types —
   never the failed payload.

Only now build the **recovery expression** — `ARRAY_CONSTRUCT(OBJECT_CONSTRUCT('_schema_version',
'<ver>', '<field>', <path>::<TYPE>, ...))` — with `_schema_version` first (mandatory for the Streaming
Loader) and one `::TYPE` cast per field. Emit the **test SELECT** using the template's two-CTE form —
build `recovered` in a CTE, then `TYPEOF(recovered[0]:<field>)` in the outer query (`TYPEOF()` needs a
VARIANT, so it can't wrap a `::TYPE` cast directly). **Do not generate the INSERT yet** — have the user
run it and paste output back.

### Phase 5 — Validate (gate — do not skip)
Check the pasted output against the validation gate in
`reference/column-classification-and-validation.md`: the query **ran clean** (decisive — Phase 3.5
proved coercibility and `::TYPE` errors rather than silent-NULL), `TYPEOF()` matches the intended
types, `_schema_version` present, row count matches, no unexpected nulls, values corrected. If
anything fails, fix the expression and re-emit the test SELECT (back to Phase 4). Diagnostics:
`troubleshooting/recovery-issues.md`.

### Phase 6 — Choose the write path
Ask the user which write path they want, **spelling out both options and the recommendation** — don't
ask a bare "how do you want to write these back?". Use words like:

> "Validation passed. How would you like to write the recovered rows?
> **1. Via a staging table (recommended)** — I insert into a temporary copy of your good table first,
> you inspect the recovered rows there, then we promote them into the good atomic events table in one
> step. Safest, and easy to abandon if anything looks off.
> **2. Directly into the good table** — insert straight into `<good table>`. Faster, but the rows are
> live immediately (Time Travel is still available to compare before/after)."

If they pick staging, create it with `CREATE TABLE ... LIKE` the good table (mirrors columns/order
exactly). The chosen target becomes the INSERT target; column classification always comes from the
**good** table.

### Phase 7 — Generate the INSERT (Snowflake builds it)
Give the user the **INSERT-generator query** with placeholders filled: table identifiers, INSERT
target, recovery column, the validated recovery expression (single quotes doubled `''`), failing
schema URI, app_id, window. It returns one cell — the full INSERT. Review it with them (recovery
column carries the expression, failure column nulled, `load_tstamp` refreshed, column count matches).
If the recovery column wasn't matched, see `troubleshooting/recovery-issues.md`.

### Phase 8 — Run, validate, promote
If the duplicate pre-check was skipped in Phase 2 and there's any doubt, offer it once more. Run the
INSERT into the chosen target; validate what landed (row count + spot-check).
- **Direct path:** done. Optionally compare before/after with Time Travel (`AT (TIMESTAMP => ...)`).
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
   resolved may have **no column yet**. Confirm with the "Finding the recovery column name" query
   (`templates/recovery-sql.md`) run against the **corrected** schema name — a returned row is your
   confirmation. If none comes back, the schema must load once (or the column be created) first.

**Key subtlety — filter on the recorded URI, build into the corrected one:**

- **Filter** the failed rows by the schema URI **as stored in the failure context**
  (`contexts_com_snowplowanalytics_snowplow_failure_1[0]:schema`) — the original, possibly-wrong URI
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
- **The underlying values are genuinely unrecoverable** (not just mis-typed) — if a `::TYPE` cast
  can't coerce the values, they may need fixing at the tracker/source, not backfilled.
- **The schema itself is wrong** and should be evolved — recovering into the old shape may be the
  wrong move; consider a schema version bump instead.
- **A different warehouse** — use the sibling recovery skill for BigQuery, Athena/Iceberg, or
  Databricks; the dialect and load mechanics differ.

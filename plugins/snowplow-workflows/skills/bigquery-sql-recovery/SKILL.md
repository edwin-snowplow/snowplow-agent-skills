---
name: bigquery-sql-recovery
description: Recover Snowplow failed events from a BigQuery failed events table back into the good atomic events table by generating the complete SQL the user runs — identification, payload inspection, a validation-gated test SELECT, the full INSERT, and post-checks. Use to reprocess, reinsert, or repair schema-validation failed events in BigQuery, especially events that failed on a single custom entity/context. Builds the wide INSERT itself with an INFORMATION_SCHEMA metadata query and reconstructs entities as native ARRAY-of-STRUCT columns, matching the target column's field names, types, and order exactly (read from ground truth, never guessed).
---

# BigQuery failed-events recovery

Recover Snowplow events that failed schema validation on a custom entity — moving them from a failed
events table back into the good atomic events table — by generating the **complete SQL the user
runs**. Both are BigQuery tables; the failed events table stores the failure context as a native
`ARRAY<STRUCT<...>>` whose `data` field is a `JSON` value.

You are the whole recovery system, from first diagnosis to a ready-to-run INSERT. This is
**human-in-the-loop**: you generate queries, the user runs them and pastes results back, and you use
those to generate the next step. You cannot run BigQuery yourself.

**Two rules that shape everything:**
- **Never ask the user to confirm a fact a query could establish** — the column name, field names,
  values, schema version, whether rows already exist. If it lives in the warehouse, generate the
  query.
- **The repaired entity's shape and types are defined by the Iglu schema, not chosen by you.** Read
  them from ground truth (the recovery column's `data_type`, a validated good-table row, or the
  schema JSON) — never guess.

One run recovers **one failing schema** for **one app_id**. If the user has several, do them one at a
time.

## Where the knowledge lives (read the form you need, when you need it)

- **`templates/recovery-sql.md`** — every SQL statement, with placeholders. The thing you actually
  hand the user at each phase.
- **`reference/failure-context.md`** — the failure column's structure and how to navigate/extract it
  (and *why* a clean run proves the casts).
- **`reference/strict-struct-typing.md`** — why the reconstructed struct must match the target type
  exactly, how the generator classifies every column, and the Phase 5 validation gate.
- **`examples/worked-recovery.md`** — a complete run end to end (string→INT64), to pattern-match against.
- **`troubleshooting/recovery-issues.md`** — symptom → cause → verify → fix for when a step goes wrong.

Read the relevant form at each phase rather than working from memory — they encode edge cases
confirmed against real Snowplow tables.

## What makes BigQuery specific

- **The INSERT builds itself — no script.** A metadata query over both tables'
  `INFORMATION_SCHEMA.COLUMNS` classifies each column and emits the full INSERT as text.
- **Strict struct typing.** Entity columns are native `ARRAY<STRUCT<...>>`; the reconstruction's
  field names, types, and order must match the target column exactly (`reference/strict-struct-typing.md`).
- **JSON payload — profile, then pick the extractor per field.** `data` is `JSON`. A repaired value may
  already be the native type or sent as the wrong one, so the profiling step (Phase 3.5) decides per
  field: **strict** `INT64()/STRING()/...` where the value is already native, `CAST(JSON_VALUE(...) AS
  <TYPE>)` where it's string-encoded or mixed, and *stop & partition* where it won't coerce. Both error
  on a bad value (never silent-NULL), and profiling guarantees they won't (`reference/failure-context.md`).
- **Zero-based array access.** `contexts_..._failure_1[0].data` (use `[SAFE_OFFSET(0)]` for NULL-safe).

---

## The 8-phase flow (the decision tree)

Carry one **time window** through every query so the identified, validated, and recovered sets match
(see "Time window" in `templates/recovery-sql.md`).

### Phase 1 — Gather tables, offer to help identify
Ask for both tables up front (BigQuery `PROJECT.DATASET.TABLE`, all three parts each) and when the
failures happened, in the opening message, so no placeholders linger:

> "First, what are your failed and good events tables (project.dataset.table each), and roughly when
> did the failures happen (a date or range — helps me scan less)? If you're not sure, I'll use a
> lookback window. And do you want help finding recent failed events, or do you already know the
> failing schema and app_id you want to recover?"

Then: help identifying → Phase 2. Already know the target → collect the failing schema Iglu URI,
app_id, and target entity column → Phase 3.

### Phase 2 — Identify the failures
Lead with the **summary identification query**; read off which schemas fail, at what volume, which
`failure_type`, which app_ids. Have the user pick **one** schema; run the **row-level drill-in** to
form a fix hypothesis — including whether it's a schema **resolution** failure rather than a
validation one (see "Recovering a ResolutionError" below).

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
required — the recovery column's `data_type` and Phase 3.5 profiling ground the recovery, and this
replaces none of them — but it's authoritative context the user can reasonably provide, letting you
sanity-check field names/types/`required`/enums, catch a renamed field, and tell a genuinely-bad value
from a wrong target type. Extra confidence, not a substitute. (Iglu Central schemas you can read directly.)

### Phase 3 — Inspect the payload
Run the **payload inspection query** (`JSON_VALUE`) so the user sees the real value behind each field.
Confirm exact field names and values, and note **how each value is encoded** (a quoted number is a
string-encoded int). Expect failures to be **compound**: one schema's failed events can combine a
renamed/missing field *and* a wrong type.

### Phase 3.5 — Profile & confirm coercibility (gate — do not skip)
Before building any recovery expression, run the **field profiling query** (`templates/recovery-sql.md`)
— it can't error — to learn, per field across *all* failed rows, the JSON types present and whether
every value coerces to its target type. Use it to (a) confirm the whole population shares **one**
recovery, and (b) pick the extractor per field: **strict** where the value is native,
**`CAST(JSON_VALUE(...) AS <TYPE>)`** where string-encoded/mixed, **stop & partition** where anything
won't coerce. Never fall back to `LAX_`. Decision table: `reference/strict-struct-typing.md`.

### Phase 4 — Build & emit the test SELECT
Two facts, both from queries not guesses: (1) the exact **recovery column name** and (2) its exact
**struct shape** — both from the "Finding the recovery column name" query, which returns
`column_name` and `data_type`. Build the recovery expression to match `data_type` field-for-field
(`_schema_version` first, mandatory), using the **per-field extractor chosen in Phase 3.5** and
grounding any doubt per `reference/failure-context.md`. Emit the
**test SELECT** (event_id + reconstructed column). **Do not generate the INSERT yet** — have the user
run it and paste output back.

### Phase 5 — Validate (gate — do not skip)
Check the pasted output against the validation gate in `reference/strict-struct-typing.md`: the query
**ran clean** (decisive — Phase 3.5 already proved coercibility and the strict/`CAST` extractors error
rather than silent-NULL), struct shape matches `data_type`, row count matches, no unexpected nulls,
values corrected. If anything fails, fix the expression and re-emit the test SELECT (back to Phase 4).
Diagnostics:
`troubleshooting/recovery-issues.md`.

### Phase 6 — Choose the write path
Ask: **staging table** (recommended — inspect the written rows, then promote) or **straight into the
good table**? If staging, create it with `CREATE TABLE ... LIKE` the good table (mirrors columns/order
exactly). The chosen target becomes the INSERT target; column classification always comes from the
**good** table.

### Phase 7 — Generate the INSERT (BigQuery builds it)
Give the user the **INSERT-generator query** with placeholders filled: table identifiers, INSERT
target, recovery column, the validated recovery expression (single quotes handled per the template),
failing schema URI, app_id, window. It returns one cell — the full INSERT. Review it with them
(recovery column carries the expression, failure column nulled, `load_tstamp` refreshed, column count
matches). If the recovery column wasn't matched or the null-cast is rejected, see
`troubleshooting/recovery-issues.md`.

### Phase 8 — Run, validate, promote
If the duplicate pre-check was skipped in Phase 2 and there's any doubt, offer it once more. Run the
INSERT into the chosen target; validate what landed (row count + spot-check).
- **Direct path:** done. Optionally compare before/after with `FOR SYSTEM_TIME AS OF`.
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
- **The underlying values are genuinely unrecoverable** (not just mis-typed) — if `INT64()` can't
  coerce the values, they may need fixing at the tracker/source, not backfilled.
- **The schema itself is wrong** and should be evolved — recovering into the old shape may be the
  wrong move; consider a schema version bump instead.
- **A different warehouse** — use the sibling recovery skill for Snowflake, Athena/Iceberg, or
  Databricks; the dialect and load mechanics differ.

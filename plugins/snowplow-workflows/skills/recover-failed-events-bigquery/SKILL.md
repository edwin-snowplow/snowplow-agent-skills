---
name: recover-failed-events-bigquery
description: Recover Snowplow failed events from a BigQuery failed events table back into the good atomic events table by generating the complete SQL the user runs — identification, payload inspection, a validation-gated test SELECT, the full INSERT, and post-checks. Use to reprocess, reinsert, or repair schema-validation failed events in BigQuery, especially events that failed on a single custom entity/context. Builds the wide INSERT itself with an INFORMATION_SCHEMA metadata query and reconstructs entities as native ARRAY-of-STRUCT columns, matching the target column's field names, types, and order exactly (read from ground truth, never guessed). Triggers: recover failed events in BigQuery, reprocess BigQuery bad rows, reinsert BigQuery schema-validation failures, recover failed events into the BigQuery atomic events table, BigQuery failed event recovery, recover BigQuery failed events with SQL.
---

# BigQuery failed-events recovery

Recover Snowplow events that failed schema validation on a custom entity — moving them from a failed
events table back into the good atomic events table — by generating the **complete SQL the user
runs**. Both are BigQuery tables; the failed events table stores the failure context as a native
`ARRAY<STRUCT<...>>` whose `data` field is a `JSON` value.

You are the whole recovery system, from first diagnosis to a ready-to-run INSERT. This is
**human-in-the-loop**: you generate queries, the user runs them and pastes results back, and you use
those to generate the next step. You cannot run BigQuery yourself.

**Three rules that shape everything:**
- **Never ask the user to confirm a fact a query could establish** — the column name, field names,
  values, schema version, whether rows already exist. If it lives in the warehouse, generate the
  query.
- **The repaired entity's shape and types are defined by the Iglu schema, not chosen by you.** Read
  them from ground truth (the recovery column's `data_type`, a validated good-table row, or the
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

- **Plain-language overview (help centre):** a customer-facing summary of what this skill does — the
  process, the safety gates, what it can and can't recover — is published at
  https://support.snowplow.io/articles/5225708172-Recovering-failed-events-in-the-warehouse-with-Claude.
  Share that link if the user asks "what does this do?".

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
- **Array access via `[SAFE_OFFSET(0)]`.** BigQuery arrays need a position accessor; use `SAFE_OFFSET(0)`
  (0-based, NULL — not error — on an empty array; the profiling gate then surfaces it as an absent count).

---

## The 8-phase flow (the decision tree)

Carry one **time window** through every query so the identified, validated, and recovered sets match
(see "Time window" in `templates/recovery-sql.md`).

### Phase 1 — Gather tables, offer to help identify
First, read `.snowplow/agent-context.md` if the project has one (written by `setup-snowplow-context`).
Its `warehouse` key is the fact that routes between these four sibling skills — it should read
`bigquery` here; if it says anything else, flag the mismatch and point the user at the matching
recovery skill instead of continuing. The file doesn't record table names, so you still ask for those
below, but confirm the warehouse rather than re-establishing it. If the file is absent, offer once to
run `setup-snowplow-context` (it removes this question for every later skill too), then continue.

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
`failure_type`, which app_ids. **One event can fail on several entities at once**, so the failure column
is an array with one element per failed entity. The identification query **flattens** it (via `UNNEST`)
so a schema is surfaced wherever it sits in the array — reading only `[SAFE_OFFSET(0)]` would hide, and
silently drop from the counts, every schema that isn't an event's first failure. Recover **one schema
per run** and re-run identification after each to see what remains. Have the user pick **one** schema;
run the **row-level drill-in** to
form a fix hypothesis — including whether it's a schema **resolution** failure rather than a
validation one (see "Recovering a ResolutionError" below). The drill-in also surfaces **`source`** —
read it from the nested `errors[…].source` path (never the top-level `source`, which is null): a mix of
`contexts` and `unstruct` means the schema failed both as a context entity **and** as the
self-describing event, which recover into **different columns** and must be **separate runs** (see
`reference/failure-context.md`).

**Offer the duplicate pre-check now (optional).** As soon as the schema is chosen — and before
building any recovery expression — **explicitly ask** whether they want to confirm these events
haven't already been recovered; don't silently skip it:

> "Want me to check whether these events have already been recovered before? You can skip this if
> you know this schema has never been recovered."

If they want it, give the **duplicate pre-check** query (`templates/recovery-sql.md`). It lists any
`event_id`s already in the good table — expect **none**. If any come back, decide with the user (stop,
narrow scope, or proceed knowingly). If they skip, note it and move on.

Then, if the failing schema is a **custom schema** (your own vendor, not a Snowplow / Iglu Central
one), get its Iglu JSON — **fetch before asking**. Try in order: the Console MCP if the official
`snowplow` plugin is installed (`.snowplow/agent-context.md`'s `mcp-available` records this); else
`snowplow-cli data-structures` with Console credentials; else a plain HTTP fetch of a public registry
or a registry URL the user gives. Only **ask the user to paste** the schema when it lives in a private
authed registry you can't reach. (`design-tracking-plan` follows the same MCP-then-CLI order.) Not
strictly required — the recovery column's `data_type` and Phase 3.5 profiling ground the recovery, and
this replaces none of them — but it's authoritative context, letting you sanity-check
field names/types/`required`/enums, catch a renamed field, and tell a genuinely-bad value from a wrong
target type. It helps most in the ResolutionError flow, where listing the registry finds the correct
schema URI the user often can't recall. Extra confidence, not a substitute. (Iglu Central schemas you
can read directly.)

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
Do these **in order, one query at a time** — never batch them, and never write a query with a guessed
value:
1. Emit **only** the "Finding the recovery column name" query. Wait for the result and read back the
   exact `column_name` and its `data_type` (which names every struct field, type, and order). Don't
   write any query that references the recovery column until you have this — no guessing it from the
   Iglu URI.
2. Then emit the "Inspect a validated event" query (written with that **real** column name) and read the
   shape off a real good row — **every time, not only when the `data_type` looks ambiguous.** It catches
   a type you misread, and whether the unstruct column carries `_schema_version` is settled by looking,
   not by defaulting. **If it returns no rows, widen the lookback; if the good table genuinely holds no validated
   events for this entity, do not recover** — recovering would make you, not the loader, establish the
   column's shape, and the loader can then fail on its next write. Tell the user to land one valid event
   first, then re-run (`reference/failure-context.md`). Ground the target
   field names/types here, never the failed payload.

Only now build the recovery expression to match `data_type` field-for-field, using the **per-field
extractor chosen in Phase 3.5** (`reference/failure-context.md`). The shape depends on the Phase-2
**source**:
- **contexts** → `[STRUCT(...)]` (array of one struct), `_schema_version` first (the `contexts_*`
  `data_type` lists it).
- **unstruct** → a single `STRUCT(...)` with **no array brackets**, into the `unstruct_event_*` column.
  Its `data_type` usually has **no `_schema_version`** field — omit it unless the type *and* the
  validated good row show it (confirm, don't assume). Don't
  apply the contexts "`_schema_version` first" rule; match the actual type. (Both forms in
  `templates/recovery-sql.md`.)

Emit the **test SELECT** (event_id + reconstructed column). **Do not generate the INSERT yet** — have
the user run it and paste output back.

### Phase 5 — Validate (gate — do not skip)
Check the pasted output against the validation gate in `reference/strict-struct-typing.md`: the query
**ran clean** (decisive — Phase 3.5 already proved coercibility and the strict/`CAST` extractors error
rather than silent-NULL), struct shape matches `data_type` exactly (including whether `_schema_version`
is present — yes for `contexts_*`, usually no for `unstruct_event_*`), row count matches, no unexpected
nulls, values corrected. If anything fails, fix the expression and re-emit the test SELECT (back to Phase 4).
Diagnostics:
`troubleshooting/recovery-issues.md`.

### Phase 6 — Choose the write path
Ask the user which write path they want, **spelling out both options and the recommendation** — don't
ask a bare "how do you want to write these back?". Use words like:

> "Validation passed. How would you like to write the recovered rows?
> **1. Via a staging table (recommended)** — I insert into a temporary copy of your good table first,
> you inspect the recovered rows there, then we promote them into the good atomic events table in one
> step. Safest, and easy to abandon if anything looks off.
> **2. Directly into the good table** — insert straight into `<good table>`. Faster, but the rows are
> live immediately."

If they pick staging, create it with `CREATE TABLE ... LIKE` the good table (mirrors columns/order
exactly). The chosen target becomes the INSERT target; column classification always comes from the
**good** table.

### Phase 7 — Generate the INSERT (BigQuery builds it)
Give the user the **INSERT-generator query** with placeholders filled: table identifiers, INSERT
target, recovery column (`contexts_*` or `unstruct_event_*`), the validated recovery expression (single
quotes handled per the template), failing schema URI, app_id, window, and the **`<source>` predicate**
(`contexts` or `unstruct`) scoping `prep` to the source you're recovering — so a schema that failed both
ways is two runs into two columns. It returns one cell — the full INSERT. Review it with them (recovery
column carries the expression, `load_tstamp` refreshed, column count matches; sibling entity columns
pass through, so a valid entity the event also carried is preserved, not nulled). If the recovery column
wasn't matched or the null-cast is rejected, see `troubleshooting/recovery-issues.md`.

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
  (`contexts_com_snowplowanalytics_snowplow_failure_1[SAFE_OFFSET(0)].schema`) — the original, possibly-wrong URI
  the event carried. Every WHERE predicate (drill-in, payload, test `prep`, INSERT `prep`, duplicate
  pre-check) must match on that recorded value.
- **Derive** the target column and `_schema_version` from the **corrected** URI.
- Same URI (transient) → nothing else changes. Different (typo / version fix) → keep the predicates
  on the recorded URI but reconstruct into the corrected column/version.

**Only bail out** when the schema genuinely can't be made to resolve or no target column can exist.
Diagnostics: `troubleshooting/recovery-issues.md`.

---

## Recovering an unstruct (self-describing event) failure

Same 8-phase flow, targeting the **self-describing event** instead of a context entity. The drill-in
`source = unstruct` means the schema failed as the event body, recovering into the
`unstruct_event_<vendor>_<name>_<major>` column. Four things differ from the contexts path — everything
else (identification, profiling gate + per-field extractor choice, the generator, pass-through,
validation) is identical:

1. **Target column is a single `STRUCT`, not `ARRAY<STRUCT>`.** Read the shape from the
   `unstruct_event_*` column's `data_type`.
2. **Reconstruct with a bare `STRUCT(...)`** — no array brackets `[...]`.
3. **No `_schema_version`.** The `unstruct_event_*` `data_type` typically omits it; match the type.
4. **Inspection/validation queries drop `[0]`/`[OFFSET(0)]` and `ARRAY_LENGTH`** (they error on a
   struct) — read the column directly, filter `IS NOT NULL`.

For Phase 7, set the generator's `<source>` predicate to `unstruct` and `<recovery_column>` to the
`unstruct_event_*` column. A schema that failed **both** ways is two separate runs, one per source. A
valid entity the event also carried (a passing `contexts_*`) is preserved automatically via pass-through.

---

## When this skill is *not* the right answer

- **The event failed for a reason a data fix can't touch** — enrichment failures, adapter failures,
  or a genuinely malformed event are out of scope; diagnose the `failure_type` first with the `debug-failed-events` skill. (A
  **ResolutionError** is *not* automatically excluded — see "Recovering a ResolutionError" above.)
- **The underlying values are genuinely unrecoverable** (not just mis-typed) — if `INT64()` can't
  coerce the values, they may need fixing at the tracker/source, not backfilled.
- **The schema itself is wrong** and should be evolved — recovering into the old shape may be the
  wrong move; consider a schema version bump instead.
- **A different warehouse** — use the sibling recovery skill for Snowflake, Athena/Iceberg, or
  Databricks; the dialect and load mechanics differ.

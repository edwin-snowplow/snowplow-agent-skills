# Recovering failed events with Claude — what this skill does

A plain-language overview for the person running a recovery. Ask Claude to walk you through it and it
generates the exact SQL for your warehouse, one step at a time. This doc explains what that process is,
what it touches, and where the safety checks are — so you know what you're agreeing to before anything
is written.

## The problem it solves

Snowplow validates every event against its schema. When an event doesn't match — a number sent as
text, a required field missing, a field name misspelled, or a schema that couldn't be fetched — it
isn't dropped. It's routed to a separate **failed events** table instead of your good **atomic events**
table. The data is still there; it's just been held back.

Recovering it means reshaping those failed rows into what the good table expects and inserting them, so
the events show up in your analytics as if they'd validated the first time.

## What the skill actually does

It's an interactive assistant that writes the recovery SQL and walks you through running it. You run
each query it gives you and paste the result back; it uses that to build the next step. **It never
connects to your warehouse** — every query runs by your hand, in your own console. You see and approve
everything before any data is written.

The end product is one `INSERT` that moves the recovered events from the failed table into the good
atomic events table, correctly typed and shaped.

One run recovers **one failing schema for one app** at a time. If several things are failing, you do
them one after another.

## How a recovery goes, step by step

1. **You point it at your tables** — the failed and good events tables, and roughly when the failures
   happened (so it scans less).
2. **It finds what's failing** and you pick one schema to recover.
3. **It inspects the real failed payloads** so the fix is based on the actual data, not a guess.
4. **It profiles the whole failed population first.** Before writing any recovery SQL, it checks every
   field across *all* the failed rows to confirm they can all be recovered the same way. If some rows
   can't be cleanly recovered, it stops and tells you — it won't quietly drop or corrupt them.
5. **It builds the recovery and a test query.** You run the test and check the output. **Nothing is
   written to your good table until this passes.**
6. **You choose how to write** — into a temporary staging table first (recommended, so you can inspect
   before committing) or straight into the good table.
7. **It generates the full INSERT** and you review it.
8. **You run it, check what landed, and promote** (if you used staging).

## The safety checks built in

- **Nothing is written without your review.** You approve the final INSERT before it runs.
- **Whole-population check before any SQL.** It confirms every failed row is recoverable up front,
  rather than discovering a bad row mid-write.
- **Test-and-validate gate.** A test query proves the recovery is correct before the real write.
- **Duplicate pre-check.** It checks you're not re-inserting events that were already recovered.
- **Shapes come from your warehouse, never guessed.** The target field names and types are read from
  your good table's actual schema (or a validated event, or your Iglu schema).
- **One query at a time, never hand-edited.** It hands you complete, runnable queries — you're never
  asked to paste in a value or fix up SQL yourself.

## What it can recover

- **Schema-validation failures** — a field sent as the wrong type (a number as text), a missing or
  renamed field, or an extra field the schema doesn't allow.
- **Both kinds of failure** — a failed **context entity** (recovered into its `contexts_*` column) and
  a failed **self-describing event** (recovered into its `unstruct_event_*` column). If the same schema
  failed both ways, that's two separate runs, one per target.
- **Some resolution errors** — where a schema couldn't be fetched but the fix is doable in the
  warehouse (a transient registry blip, or a corrected schema URI).
- Events that also carried *valid* data alongside the failure keep that valid data — it's preserved,
  not discarded.

## What it won't do

- **Failures a data fix can't touch** — enrichment failures, adapter failures, or genuinely malformed
  events. Those need diagnosing at the source, not backfilling.
- **Values that are actually wrong**, not just mistyped — if a value can't be converted to the type the
  schema expects, it's flagged rather than forced.
- **Cases where the schema itself should change** — sometimes the right move is to evolve the schema,
  not recover into the old shape. It will say so.

## What you'll be asked for

- Your **good** and **failed** events table names.
- Roughly **when** the failures happened.
- The **failing schema** and the **app** you want to recover.
- For a custom schema, the **Iglu schema JSON** (optional — it adds confidence by letting Claude
  cross-check field names and types).

## Warehouse differences

The flow and the safety checks are the same everywhere. What changes is the SQL dialect and how the
final INSERT is assembled:

| Warehouse | Dialect notes | How the INSERT is built |
|---|---|---|
| **Athena / Iceberg** | Trino SQL, 1-based arrays, `ROW(...)` structs | A small local Python script assembles it from your table metadata |
| **Snowflake** | `VARIANT`, `OBJECT_CONSTRUCT` / `ARRAY_CONSTRUCT` | A query in Snowflake builds the INSERT for you |
| **BigQuery** | Native `ARRAY<STRUCT>`, JSON extraction | A query in BigQuery builds the INSERT for you |
| **Databricks** | Native `ARRAY<STRUCT>`, `GET_JSON_OBJECT` + `CAST` | A small local Python script assembles it from your table metadata |

## The bottom line

You end up with the previously-failed events sitting in your good atomic events table, correctly typed
and complete, with a clear record of exactly what was run to get them there — and no query ran that you
didn't see and approve first.

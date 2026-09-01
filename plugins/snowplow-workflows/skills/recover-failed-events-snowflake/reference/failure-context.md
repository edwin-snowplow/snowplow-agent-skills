# Reference — the failure context column & how to navigate it

**The decision this doc helps you make:** how to read the failed payload out of Snowflake correctly,
so the values you extract for the repaired entity are the real ones and the extractions are typed
right. The SQL that applies these rules lives in `templates/recovery-sql.md`.

## Where the failure lives

Snowplow writes the failure into a column always named
`contexts_com_snowplowanalytics_snowplow_failure_1`. In Snowflake it is a semi-structured `ARRAY` of
`OBJECT`s (VARIANT), **not** a native struct. That semi-structured typing is *why* you access it with
VARIANT syntax (`:` navigation, `::TYPE` casts) rather than SQL dot notation.

Fields of each failure object:

| Key | Meaning |
|---|---|
| `_schema_version` | Version of the failure context schema |
| `component_name` | Pipeline component that rejected the event |
| `component_version` | |
| `data` | Original event payload as VARIANT — read fields with `:field::TYPE` |
| `errors` | **Array** of error objects; each element carries `source`, `path`, `keyword`, `message`, … — `source` is the key one (see below) |
| `failure_type` | e.g. `SchemaViolation` |
| `schema` | Iglu URI of the schema that failed validation |
| `timestamp` | When the failure occurred |

## The `source` field — contexts vs unstruct (it's nested under `errors`, not top-level)

Whether the event failed as a **context entity** or as the **self-describing (unstructured) event** is
recorded on each error, at **`errors[0]:source`** — it is **not** a top-level failure field. Reading
`contexts_..._failure_1[0]:source` returns NULL; the correct path is
`contexts_com_snowplowanalytics_snowplow_failure_1[0]:errors[0]:source::STRING`. Values:

- **`contexts`** — the schema failed as a context entity → recovers into the
  `contexts_<vendor>_<name>_<major>` **ARRAY** column (this skill's main path,
  `ARRAY_CONSTRUCT(OBJECT_CONSTRUCT(...))`).
- **`unstruct`** — the *same* schema failed as the self-describing event → recovers into the
  `unstruct_event_<vendor>_<name>_<major>` column, which is a **single OBJECT**, not an array — a
  different target column **and** a different expression shape (`OBJECT_CONSTRUCT(...)`, no
  `ARRAY_CONSTRUCT`). Read/validate it directly (`<recovery_column>:field`) — no `[0]`, no `ARRAY_SIZE`.
  The event object carries **no `_schema_version`** key (only contexts objects do), so omit it unless a
  validated unstruct event shows it. `unstruct_event_*` is a single `OBJECT` (`OBJECT_KEYS`/`TYPEOF`),
  and `errors[0]:source` returns `contexts`/`unstruct`.

**One schema can fail both ways in the same window.** If the drill-in shows a mix of `contexts` and
`unstruct` sources, they can't go through one recovery expression: scope the run to a single `source`
(`...errors[0]:source::STRING = 'contexts'`), recover it, then handle the other `source` as a separate
run into its own column. Always read `source` via the `errors[0]:source` path — never the top-level
guess.

**Valid entities the failed event also carried are preserved.** The failed-events table keeps a passing
attached entity in its own column, so the recovery's pass-through carries it into the good row
untouched. Recover only what failed; do **not** null the sibling entity columns.

## Navigation rules (and why)

- **`[0]` — arrays are 0-indexed** (Athena is 1-indexed). The failure context is normally a
  single-element array, so `contexts_..._failure_1[0]` is the failure object.
- **`:`** — navigate into a VARIANT/OBJECT key, e.g. `...failure_1[0]:data`, `...failure_1[0]:schema`.
- **`::TYPE`** — cast a VARIANT value to a SQL type, e.g. `...:data:user_id::INTEGER`,
  `...:schema::STRING`. A `::TYPE` cast **coerces both native and string-encoded values** (an int sent
  as `"5"` still casts to `INTEGER`) and **raises an error** on a value it genuinely can't convert — so
  it never silently returns NULL for a bad value. That erroring behaviour is what makes a clean test
  SELECT proof that the casts hold.

**Don't guess whether a value is native or string-encoded — profile first.** The value you're
repairing was often sent as the wrong VARIANT type. Run the **field profiling query**
(`templates/recovery-sql.md`, Phase 3.5) to see, per field across *all* failed rows, which VARIANT
types are present (`TYPEOF`) and whether every value casts to the target type (`TRY_CAST`); then build
the `::TYPE` casts knowing the whole population coerces (decision table in
`reference/column-classification-and-validation.md`). **Avoid `TRY_CAST` in the *recovery* expression
itself:** it swallows a bad value into NULL silently, hiding an incomplete recovery — it belongs only
in the non-erroring profiling query.

## Expanding the array vs. indexing it

To expand a multi-element failure array (or to read `errors` row-by-row), use `LATERAL FLATTEN`:
`FROM <table>, LATERAL FLATTEN(input => contexts_com_snowplowanalytics_snowplow_failure_1) f`, then
read `f.value:data`, `f.value:errors`, `f.value:schema`, etc. This is what the payload-inspection
query does so each field renders on its own. **One event can fail on several entities at once**, so
every query flattens and matches the target schema on the flattened element (`f.value:schema`), not
`[0]` — an event whose target failure isn't the first array element would be missed. The recovery
expression reads its payload from the matched element (`failure_entry:data`), which the test SELECT and
generator `prep` expose via `LATERAL FLATTEN`.

## Grounding the target shape (never guess it)

The values you extract come from this failed payload, but the **types and field names you extract
into are not defined by the payload** — the payload is exactly what failed validation. They are
defined by the Iglu schema. Resolve the intended shape from ground truth, in this order of preference:

1. A validated good-table row for the same entity (the "Inspect a validated event" query) — **always
   query one; on Snowflake it is the only grounding there is.** Entity columns are VARIANT, so there is
   no DDL type to read: the `TYPEOF()` of each field on a known-good row *is* the target type your
   `::TYPE` casts must produce, and a good row is the only thing that tells you whether an unstruct
   value carries `_schema_version`. Never assume that shape.

The recovery column's **name** (not its full shape — Snowflake entity columns are semi-structured VARIANT)
still comes from a query: the "Finding the recovery column name" lookup. See
`reference/column-classification-and-validation.md` for how the generator uses that name to classify
columns, and `templates/recovery-sql.md` for the queries.


### No validated events in the good table → stop, don't recover

If the good table holds **no validated events for this entity** — the recovery column is absent, or it
exists but every row is NULL — **do not build a recovery.** Say so plainly and stop.

The reason is the loader, not the SQL. Inserting into a column the loader has never populated means
*you* are establishing that column's shape: adding the column outright, or fixing a struct contract the
loader hasn't set yet. If that differs from what the loader later creates — a field order, a type width,
a nullable flag — the loader can fail on its next write, turning a failed-events problem into a broken
pipeline.

Tell the user to send one valid event of this schema through the pipeline first and let the loader
create and populate the column, then re-run the recovery. The failed events aren't going anywhere.

Widen the lookback before concluding the table is empty — a validated example may simply predate the
default window. "Zero rows in the last 7 days" is not "no validated events".

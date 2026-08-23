# Reference — the failure context column & how to navigate it

**The decision this doc helps you make:** how to read the failed payload out of BigQuery correctly,
so the values you extract for the repaired entity are the real ones and the extractions are typed
right. The SQL that applies these rules lives in `templates/recovery-sql.md`.

## Where the failure lives

Snowplow writes the failure into a column always named
`contexts_com_snowplowanalytics_snowplow_failure_1`. In BigQuery it is a **native**
`ARRAY<STRUCT<...>>` (`REPEATED RECORD`), and its `data` field is a **`JSON`** value. That native
typing is *why* you navigate it with array/struct/JSON syntax rather than string functions.

Fields of each failure struct:

| Field | Meaning |
|---|---|
| `_schema_version` | Version of the failure context schema |
| `component_name` | Pipeline component that rejected the event |
| `component_version` | |
| `data` | Original event payload as **`JSON`** — read fields with `INT64/STRING/...(data.field)` |
| `errors` | **Array** of error objects; each carries `source`, `path`, `keyword`, `message`, … — `source` is the key one (see below) |
| `failure_type` | e.g. `SchemaViolation` |
| `schema` | Iglu URI of the schema that failed validation |
| `timestamp` | When the failure occurred |

## The `source` field — contexts vs unstruct (nested under `errors`, not top-level)

Whether the event failed as a **context entity** or the **self-describing (unstructured) event** is
recorded on each error, at **`errors[SAFE_OFFSET(0)].source`** — *not* a top-level failure field
(reading `...failure_1[SAFE_OFFSET(0)].source` returns NULL). Read it with
`JSON_VALUE(contexts_com_snowplowanalytics_snowplow_failure_1[SAFE_OFFSET(0)].errors[SAFE_OFFSET(0)].source)`.
(`errors` is `ARRAY<JSON>`, so index the array with `SAFE_OFFSET(0)` and then JSON-navigate `.source`.)
Values:

- **`contexts`** — failed as a context entity → recovers into the `contexts_<vendor>_<name>_<major>`
  **ARRAY** column (this skill's main path).
- **`unstruct`** — the *same* schema failed as the self-describing event → recovers into the
  `unstruct_event_<vendor>_<name>_<major>` column, a **single STRUCT** (not an array) — a different
  target column **and** a different reconstruction: a bare `STRUCT(...)`, no array brackets. The
  `unstruct_event_*` `data_type` usually has **no `_schema_version`** field (the `contexts_*` struct
  does) — match it, don't add one. Inspection/validation read the column directly — no `[0]`,
  `[OFFSET(0)]`, or `ARRAY_LENGTH` (all error on a struct); filter `IS NOT NULL`.

**One schema can fail both ways in the same window.** A mix of `contexts` and `unstruct` can't go
through one recovery expression: scope the run to one `source` (the generator's `<source>` predicate),
recover it, then handle the other as a separate run into its own column.

**Valid entities the failed event also carried are preserved.** The failed-events table materialises a
passing attached entity in its own column, so the recovery's pass-through carries it into the good row
untouched. Recover only what failed; do **not** null the sibling entity columns.

## Navigation rules (and why)

- **`[SAFE_OFFSET(0)]` — 0-based array access, NULL-safe (the recommended accessor here).** BigQuery
  arrays need a position accessor — `OFFSET`/`ORDINAL` (error out of range), `SAFE_OFFSET`/`SAFE_ORDINAL`
  (NULL out of range), or a bare `[0]` (behaves like `OFFSET`). Prefer `[SAFE_OFFSET(0)]`: 0-based (same
  as the other warehouses), and NULL rather than an error on an unexpectedly empty array — the Phase-3.5
  profiling gate then surfaces that as an absent count instead of a mid-run failure. The failure context
  is normally a single-element array, so `contexts_..._failure_1[SAFE_OFFSET(0)]` is the failure struct.
- **`.field`** — navigate into a struct field, e.g. `...failure_1[SAFE_OFFSET(0)].data`,
  `...failure_1[SAFE_OFFSET(0)].schema`.
- **JSON extraction — the extractor is chosen *per field from profiling*, not fixed.** `data` is
  `JSON`. Two ways to read a scalar:
  - **strict accessor** — `INT64(...)`, `STRING(...)`, `FLOAT64(...)`, `BOOL(...)` — reads the native
    JSON type and **errors** if the value isn't that type;
  - **coerce** — `CAST(JSON_VALUE(...) AS <TYPE>)` — pulls the scalar as a string and casts it, so it
    handles a value sent as the *wrong* JSON type (e.g. an int sent as `"5"`), still erroring on a
    genuinely un-castable value;
  - `JSON_VALUE(...)` alone returns any scalar as STRING (use for read-only inspection);
    `JSON_QUERY(...)` / `JSON_QUERY_ARRAY(...)` for JSON subtrees / arrays.

**Don't guess which — profile first.** The value you're repairing was often sent as the wrong JSON
type, so `INT64()` on a string-encoded `"5"` errors while `CAST(JSON_VALUE(...) AS INT64)` coerces it.
Run the **field profiling query** (`templates/recovery-sql.md`, Phase 3.5) to see, per field across
*all* failed rows, which JSON types are present and whether every value coerces to the target type;
then pick the strictest extractor the data supports (decision table in
`reference/strict-struct-typing.md`). Because both the strict accessors and `CAST` error on a bad
value — and profiling has confirmed none exist — a clean test SELECT genuinely proves the casts hold.
**Avoid the lenient `LAX_` accessors:** they swallow bad values into NULL silently, hiding an
incomplete recovery.

## Expanding the array vs. indexing it

To expand a multi-element failure array (or to read `errors` row-by-row), use `UNNEST`:
`FROM <table>, UNNEST(contexts_com_snowplowanalytics_snowplow_failure_1) AS f`, then read `f.data`,
`f.errors`, `f.schema`, etc. This is what the payload-inspection query does so each field renders on
its own. For **reconstructing** the entity, direct `[SAFE_OFFSET(0)]` indexing is simpler and is what
the recovery expression uses.

## Grounding the target shape (never guess it)

The values you extract come from this failed payload, but the **types and field names you extract
into are not defined by the payload** — the payload is exactly what failed validation. They are
defined by the Iglu schema, materialised in the recovery column's `data_type`. Resolve the intended
shape from ground truth, in this order of preference:

1. The recovery column's `data_type` (from the "Finding the recovery column name" query) — usually
   enough; it names every field, its type, and its order.
2. A validated good-table row for the same entity (the "Inspect a validated event" query) — a
   cross-check.
3. The Iglu schema JSON itself — the fallback when no validated events exist yet (brand-new schema)
   and the `data_type` is ambiguous. Ask the user for it or its URL.

See `reference/strict-struct-typing.md` for why the shape has to match *exactly*, and
`templates/recovery-sql.md` for the queries.

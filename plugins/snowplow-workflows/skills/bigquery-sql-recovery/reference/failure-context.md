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
| `errors` | Structured validation errors |
| `failure_type` | e.g. `SchemaViolation` |
| `schema` | Iglu URI of the schema that failed validation |
| `timestamp` | When the failure occurred |

## Navigation rules (and why)

- **`[0]` — arrays are zero-based** (direct index; errors if out of range). The failure context is
  normally a single-element array, so `contexts_..._failure_1[0]` is the failure struct. Use
  `[SAFE_OFFSET(0)]` to get NULL instead of an error on an empty array.
- **`.field`** — navigate into a struct field, e.g. `...failure_1[0].data`, `...failure_1[0].schema`.
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
its own. For **reconstructing** the entity, direct `[0]` indexing is simpler and is what the recovery
expression uses.

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

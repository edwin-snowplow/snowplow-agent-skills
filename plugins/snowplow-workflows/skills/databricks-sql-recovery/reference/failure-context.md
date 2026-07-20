# Reference — the failure context column & how to navigate it

**The decision this doc helps you make:** how to read the failed payload out of Databricks correctly,
so the values you extract for the repaired entity are the real ones and the extractions are typed
right — while avoiding Databricks' silent-NULL trap. The SQL that applies these rules lives in
`templates/recovery-sql.md`.

All identifiers are Unity Catalog three-part names — `catalog.schema.table`. (Two-part
`schema.table` works if a default catalog is set, but prefer three-part so nothing is ambiguous.)

---

## Silent NULLs — the Databricks trap (read this first)

Snowflake and BigQuery **error** on a bad cast, so a test SELECT that runs at all proves the casts
held. **Databricks does not give you that guarantee**, in two ways:

1. **`GET_JSON_OBJECT(data, '$.path')` returns NULL for a missing or malformed path** — it never
   errors. A typo in the JSON path silently yields NULL.
2. **`CAST(x AS <type>)` returns NULL on an un-castable value in legacy mode.** It only *errors* under
   ANSI mode (`spark.sql.ansi.enabled = true`), which is the default on Databricks SQL warehouses and
   recent DBR, but not on every cluster. `TRY_CAST` always returns NULL rather than erroring.

Consequence: **never treat "the query ran" as proof the recovery is correct.** Validation (Phase 5)
must actively look for unexpected NULLs and eyeball the values. The Phase 3.5 profiling query
pre-empts this by confirming coercibility up front, and the optional per-field null-count query
(`templates/recovery-sql.md`) makes the post-hoc check mechanical.

---

## Where the failure lives

Snowplow writes the failure into a column always named
`contexts_com_snowplowanalytics_snowplow_failure_1`. In Databricks it is a **native**
`ARRAY<STRUCT<...>>`, and its `data` field is a **JSON-serialised STRING** (not a native JSON/VARIANT
type). Access it with native array/struct syntax, and read `data` with `GET_JSON_OBJECT`.

Fields of each failure struct:

| Field | Meaning |
|---|---|
| `_schema_version` | Version of the failure context schema |
| `component_name` | Pipeline component that rejected the event |
| `component_version` | |
| `data` | Original event payload as a **JSON STRING** — read fields with `GET_JSON_OBJECT(data, '$.field')` |
| `errors` | Structured validation errors |
| `failure_type` | e.g. `SchemaViolation` |
| `schema` | Iglu URI of the schema that failed validation |
| `timestamp` | When the failure occurred |

## Navigation rules (and why)

- **`[0]` — arrays are zero-based** (direct index). The failure context is normally a single-element
  array, so `contexts_..._failure_1[0]` is the failure struct. `element_at(col, 1)` is the 1-based
  equivalent; `try_element_at(col, 1)` returns NULL instead of erroring on an empty array.
- **`.field`** — navigate into a struct field, e.g. `...failure_1[0].data`, `...failure_1[0].schema`.
- **JSON extraction** — `data` is a STRING. Pull a scalar out with `GET_JSON_OBJECT(data, '$.field')`,
  which returns a **STRING**, then wrap in `CAST(... AS <type>)` for the target type:
  - `CAST(GET_JSON_OBJECT(...failure_1[0].data, '$.user_id') AS BIGINT)`
  - `CAST(GET_JSON_OBJECT(...failure_1[0].data, '$.user_name') AS STRING)`
  - `CAST(... AS DOUBLE)`, `CAST(... AS BOOLEAN)`, `CAST(... AS TIMESTAMP)` for other scalar types.
  - For a nested JSON object/array field, `GET_JSON_OBJECT(data, '$.obj')` returns the JSON substring;
    use `FROM_JSON(..., '<schema>')` if you need it as a typed struct/array.

  Remember the silent-NULL trap above: a wrong path or un-castable value can yield NULL, not an error.
  Don't guess whether a value will coerce — run the **field profiling query** (Phase 3.5) first, which
  quantifies coercibility per field across *all* failed rows so the built expression is guaranteed not
  to silently NULL.

## Expanding the array vs. indexing it

To expand a multi-element failure array (or to read `errors` row-by-row), use
`LATERAL VIEW explode(contexts_com_snowplowanalytics_snowplow_failure_1) t AS f`, then read `f.data`,
`f.errors`, `f.schema`, etc. This is what the payload-inspection query does so each field renders on
its own. For **reconstructing** the entity, direct `[0]` indexing is simpler and is what the recovery
expression uses.

## Grounding the target shape (never guess it)

The values you extract come from this failed payload, but the **types and field names you extract
into are not defined by the payload** — the payload is exactly what failed validation. They are
defined by the Iglu schema, materialised in the recovery column's `full_data_type`. Resolve the
intended shape from ground truth, in this order of preference:

1. The recovery column's `full_data_type` (from the "Finding the recovery column name" query) —
   usually enough; it names every field, its type, and its order.
2. A validated good-table row for the same entity (the "Inspect a validated event" query) — a
   cross-check.
3. The Iglu schema JSON itself — the fallback when no validated events exist yet (brand-new schema)
   and the `full_data_type` is ambiguous. Ask the user for it or its URL.

See `reference/strict-typing-and-classification.md` for why the shape has to match *exactly*, and
`templates/recovery-sql.md` for the queries.

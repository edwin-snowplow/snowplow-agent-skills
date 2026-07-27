# Reference — the failure context column & how to navigate it

**The decision this doc helps you make:** how to read the failed payload out of Athena correctly, so
the values you extract for the repaired entity are the real ones and typed right. The SQL that applies
these rules lives in `templates/recovery-sql.md`.

## Where the failure lives

Snowplow writes the failure into a column always named
`contexts_com_snowplowanalytics_snowplow_failure_1`. In an Iceberg table it is a native
`ARRAY(ROW(...))` — **not** a JSON string — but its `data` field *is* a **JSON string** (`VARCHAR`),
read with `json_extract_scalar`. That native typing is *why* you navigate the array with 1-indexed /
`UNNEST` syntax rather than string functions, while the payload inside `data` needs JSON extraction.

Fields of each failure ROW:

| Field | Type | Notes |
|---|---|---|
| `_schema_version` | `VARCHAR` | Version of the failure context schema |
| `component_name` | `VARCHAR` | Pipeline component that rejected the event |
| `component_version` | `VARCHAR` | |
| `data` | `VARCHAR` | Original event payload as a JSON string — read with `json_extract_scalar` |
| `errors` | `ARRAY(ROW(...))` | Structured validation errors; each element carries `source`, `path`, `keyword`, `message`, … — `source` is the key one (see below) |
| `failure_type` | `VARCHAR` | e.g. `SchemaViolation` |
| `schema` | `VARCHAR` | Iglu URI of the schema that failed validation |
| `timestamp` | `TIMESTAMP` | When the failure occurred |

## The `source` field — contexts vs unstruct (nested under `errors`, not top-level)

Whether the event failed as a **context entity** or the **self-describing (unstructured) event** is
recorded on each error, at **`errors[1].source`** (1-indexed, native `ROW` field) — *not* a top-level
failure field (`...failure_1[1].source` doesn't exist). Read it with
`contexts_com_snowplowanalytics_snowplow_failure_1[1].errors[1].source`. Values:

- **`contexts`** — failed as a context entity → recovers into the `contexts_<vendor>_<name>_<major>`
  **ARRAY(ROW(...))** column (this skill's main path).
- **`unstruct`** — the *same* schema failed as the self-describing event → recovers into the
  `unstruct_event_<vendor>_<name>_<major>` column, a **single `ROW`** (not an array) — a different
  target column **and** a different reconstruction (`CAST(ROW(...) AS ROW(...))`, no outer `ARRAY[...]`).

**One schema can fail both ways in the same window.** A mix of `contexts` and `unstruct` can't go
through one recovery expression or one generator run: scope to one `source`, recover it, then handle
the other as a separate run into its own column.

## Navigation rules (and why)

- **`[1]` — arrays are 1-indexed** in Trino/Presto (direct index). The failure context is normally a
  single-element array, so `contexts_..._failure_1[1]` is the failure struct, e.g.
  `contexts_..._failure_1[1].schema`, `...[1].failure_type`, `...[1].timestamp`. This is what the
  identification/drill-in/pre-check WHERE predicates filter on.
- **Athena does not support deep nested dot notation** (`col[1].data.field`) in a `SELECT`. To read a
  field *inside* the JSON payload you must first `UNNEST` the array, then `json_extract_scalar` the
  `data` field.
- **JSON extraction — `json_extract_scalar(failure_data, '$.field')`** pulls a scalar out of the
  payload JSON string as text. To land it in a typed struct field, wrap it:
  `CAST(json_extract_scalar(...) AS <type>)` — which coerces both a native value and a value sent as
  the *wrong* JSON type (e.g. an int sent as `"5"`), still erroring on a genuinely un-castable value.
  For read-only inspection, `json_extract_scalar(...)` alone returns everything as text.

**Don't guess the cast — profile first.** The value you're repairing was often sent as the wrong JSON
type. Run the **field profiling query** (`templates/recovery-sql.md`, Phase 3.5) to see, per field
across *all* failed rows, whether every value casts to the target type (`TRY_CAST` never errors) and
whether the field is always present; then build the expression. Because `CAST(json_extract_scalar(...)
AS <type>)` **errors** on a value it can't convert — and profiling has confirmed none exist — a clean
test SELECT genuinely proves the casts hold. **Avoid `TRY_CAST` in the recovery expression itself:** it
swallows a bad value into NULL silently, hiding an incomplete recovery (it belongs only in the
profiling query, where NULL is the signal you're counting).

## Expanding the array — UNNEST

To read the payload field-by-field, expand the failure array with `UNNEST`:
`FROM <table> CROSS JOIN UNNEST(contexts_com_snowplowanalytics_snowplow_failure_1) AS t(failure_ctx)`,
then read `failure_ctx.data`, `failure_ctx.schema`, etc. This is what the payload-inspection query and
the test SELECT / INSERT `unnested` CTE do so each field renders on its own. For **filtering** the
failed rows (WHERE predicates), direct `[1]` indexing on `...schema` is simpler.

## Grounding the target shape (never guess it) — the good-table DESCRIBE

The values you extract come from the failed payload, but the **types and field names you extract into
are not defined by the payload** — the payload is exactly what failed validation. They are defined by
the Iglu schema, materialised into the good-table entity column. Resolve the intended shape from
ground truth, in this order of preference:

1. The **good-table `DESCRIBE`** — `DESCRIBE <good_database>.<good_table>` names the recovery column
   and its full struct type (field names, types, order). This is the authoritative contract the
   recovery expression's `ARRAY(ROW(...))` must match after Hive→Athena type conversion. Usually
   enough on its own.
2. A validated good-table row for the same entity (`SELECT <recovery_column>[1] ...`) — a cross-check.
3. The **Iglu schema JSON** itself — the fallback when the entity isn't in the good-table `DESCRIBE`
   yet (a brand-new schema with no validated events) and the type is ambiguous. Ask the user for it or
   its URL and read the field types and `required` list from there.

Do not infer types from the failed payload alone. See
`reference/column-classification-and-validation.md` for why the reconstructed struct must match the
good-table type *exactly*, and `templates/recovery-sql.md` for the queries.

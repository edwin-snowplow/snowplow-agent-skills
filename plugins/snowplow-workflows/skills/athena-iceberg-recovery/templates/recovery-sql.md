# Recovery SQL templates — Athena Iceberg

Fill in every `<placeholder>`. These are the exact statements to hand the user at each phase; the
reasoning behind them lives in `reference/` and the failure modes in `troubleshooting/`. The SQL here
is identical to the canonical recovery flow — do not paraphrase it.

The bundled `scripts/generate_insert.py` implements the column-classification and type-conversion
rules for the full INSERT (Phase 7) — prefer running it. Use the statements here directly for the
identification/inspection/test queries, and as the fallback when code execution is unavailable.

---

## Time window (applies to every query below)

The failed/good tables are partitioned by `load_tstamp_day`, so bounding `load_tstamp` prunes
partitions and scans far less data. Ask the user in Phase 1 **when the failures occurred** and
prefer a **bounded range**:

```sql
load_tstamp >= TIMESTAMP '2026-03-01 00:00:00' AND load_tstamp < TIMESTAMP '2026-03-10 00:00:00'
```

If the timeframe is unknown, fall back to a **lookback** (shown in the templates below):

```sql
load_tstamp > current_timestamp - interval '<days>' day
```

Use the **same** window in every query (identification, inspection, test, INSERT, duplicate
pre-check) so the identified, validated, and recovered sets match. The bundled generator accepts
either form (`LOAD_TSTAMP_START`/`LOAD_TSTAMP_END`, or `DAYS_LOOKBACK`).

---

## Identification query — summary (Phase 2, default)

Lead with this: one row per schema × app_id × failure type with volumes and time span. Best for
surveying what's failing without dumping every row, especially over a wide window.

```sql
SELECT
    contexts_com_snowplowanalytics_snowplow_failure_1[1].schema         AS failing_schema,
    app_id,
    contexts_com_snowplowanalytics_snowplow_failure_1[1].failure_type   AS failure_type,
    COUNT(*)                                                            AS failed_events,
    MIN(contexts_com_snowplowanalytics_snowplow_failure_1[1].timestamp) AS first_seen,
    MAX(contexts_com_snowplowanalytics_snowplow_failure_1[1].timestamp) AS last_seen
FROM <failed_database>.<failed_table>
WHERE load_tstamp > current_timestamp - interval '<days>' day   -- or a bounded range (see Time window)
  AND contexts_com_snowplowanalytics_snowplow_failure_1 IS NOT NULL
GROUP BY
    contexts_com_snowplowanalytics_snowplow_failure_1[1].schema,
    app_id,
    contexts_com_snowplowanalytics_snowplow_failure_1[1].failure_type
ORDER BY failed_events DESC
```

## Identification query — row-level drill-in (after a schema is chosen)

Once the user picks a failing schema, inspect *why* it's failing for that schema + app_id. Scoped,
so it returns a handful of rows, not a flood.

```sql
SELECT
    contexts_com_snowplowanalytics_snowplow_failure_1[1].failure_type       AS failure_type,
    contexts_com_snowplowanalytics_snowplow_failure_1[1].errors[1].source   AS source,  -- contexts vs unstruct; a mix = separate runs (see reference/failure-context.md)
    contexts_com_snowplowanalytics_snowplow_failure_1[1].errors             AS errors,
    contexts_com_snowplowanalytics_snowplow_failure_1[1].data               AS data
FROM <failed_database>.<failed_table>
WHERE load_tstamp > current_timestamp - interval '<days>' day   -- or a bounded range (see Time window)
  AND contexts_com_snowplowanalytics_snowplow_failure_1[1].schema = '<failing_schema_uri>'
  AND app_id = '<app_id>'
LIMIT 10
```

---

## Payload inspection query (Phase 3)

Expands the failure context with `UNNEST` so the user sees the real value behind each field, read
with `json_extract_scalar` (Athena does not support deep nested dot notation on the array — you must
`UNNEST` first). Read the results for **how each value is encoded**, not just its content, and note
the exact field names.

```sql
SELECT
    failure_ctx.schema                                 AS failing_schema,
    json_extract_scalar(failure_ctx.data, '$.field1')  AS field1,
    json_extract_scalar(failure_ctx.data, '$.field2')  AS field2
FROM <failed_database>.<failed_table>
CROSS JOIN UNNEST(contexts_com_snowplowanalytics_snowplow_failure_1) AS t(failure_ctx)
WHERE load_tstamp > current_timestamp - interval '<days>' day
  AND contexts_com_snowplowanalytics_snowplow_failure_1[1].schema = '<failing_schema_uri>'
LIMIT 10
```

---

## Field profiling & coercibility confirmation (Phase 3.5 — gate before building the expression)

Before writing any recovery expression, profile **every field you'll recover** across **all** failed
rows, to confirm the whole population is recoverable by one expression. This query can't error
(`json_extract_scalar`, `TRY_CAST`, `count_if`). `<failure_data>` is the payload JSON string,
`contexts_com_snowplowanalytics_snowplow_failure_1[1].data`. Add one block per field, using that
field's **target type** (from the good-table `DESCRIBE`) in the `TRY_CAST`:

```sql
WITH prep AS (
    SELECT contexts_com_snowplowanalytics_snowplow_failure_1[1].data AS d
    FROM <failed_database>.<failed_table>
    WHERE <load_tstamp window>
      AND contexts_com_snowplowanalytics_snowplow_failure_1[1].schema = '<failing_schema_uri>'
      AND app_id = '<app_id>'
)
SELECT
    COUNT(*)                                                                              AS total_rows,
    -- one block per recovered field (swap <field> and its <TARGET_TYPE>):
    count_if(json_extract_scalar(d, '$.<field>') IS NULL)                                 AS <field>_absent,
    count_if(json_extract_scalar(d, '$.<field>') IS NOT NULL
             AND TRY_CAST(json_extract_scalar(d, '$.<field>') AS <TARGET_TYPE>) IS NULL)  AS <field>_uncoercible
FROM prep;
```

Decide, per field: any `_uncoercible > 0` → **stop, list the offending rows, and partition/scope**. Any
`_absent > 0` → required→partition, optional→NULL. Otherwise build the expression —
`CAST(json_extract_scalar(...) AS <type>)` runs clean because profiling confirmed every value coerces.
(Full decision table: `reference/column-classification-and-validation.md`.)

---

## Recovery expression pattern (Phase 4) — build & confirm with the user

The shape depends on the failure **source** (from the drill-in), mapping each
`json_extract_scalar(failure_data, '$.field')` to its target field/type in the **same order** as the
good-table struct.

**contexts source** — `CAST(ARRAY[ROW(...)] AS ARRAY(ROW(...)))`. `_schema_version` is the first field
(the `contexts_*` struct in DESCRIBE lists it):

```sql
CAST(
    ARRAY[
        ROW(
            '1-0-0',                                                         -- corrected _schema_version
            CAST(json_extract_scalar(failure_data, '$.field1') AS VARCHAR),
            CAST(json_extract_scalar(failure_data, '$.field2') AS BIGINT)
        )
    ]
AS ARRAY(ROW(_schema_version VARCHAR, field1 VARCHAR, field2 BIGINT)))
```

**unstruct source** — a **single `CAST(ROW(...) AS ROW(...))`, no outer `ARRAY[...]`/`ARRAY(...)`** (the
`unstruct_event_*` column is a single `ROW`). Its DESCRIBE struct typically has **no `_schema_version`**
field (only `contexts_*` does) — omit it unless DESCRIBE lists it:

```sql
CAST(
    ROW(
        CAST(json_extract_scalar(failure_data, '$.field1') AS VARCHAR),
        CAST(json_extract_scalar(failure_data, '$.field2') AS BIGINT)
    )
AS ROW(field1 VARCHAR, field2 BIGINT))
```

Entity columns must be constructed with `CAST(... AS ARRAY(ROW(...)))` (contexts) or `CAST(... AS
ROW(...))` (unstruct). The `ARRAY<STRUCT<...>>` / `STRUCT<...>` form in DESCRIBE output is DDL-only and
invalid in a query. Do **not** append `AS <column_name>` here — the generator adds it.

---

## Test SELECT + type validation (Phase 4 — run and validate before the INSERT)

Emit this so the user can prove the reconstructed column is correct before any INSERT is generated.
It wraps the recovery expression in the CTEs and adds `typeof(...)` so the exact Athena type can be
compared against the good-table column definition.

```sql
WITH prep AS (
    SELECT *
    FROM <failed_database>.<failed_table>
    WHERE load_tstamp > current_timestamp - interval '<days>' day
      AND app_id = '<app_id>'
      AND contexts_com_snowplowanalytics_snowplow_failure_1[1].schema = '<failing_schema_uri>'
),
unnested AS (
    SELECT prep.*, failure_ctx.data AS failure_data
    FROM prep
    CROSS JOIN UNNEST(contexts_com_snowplowanalytics_snowplow_failure_1) AS t(failure_ctx)
)
SELECT
    event_id,
    <recovery expression>                AS recovered,
    typeof(<recovery expression>)        AS recovered_type
FROM unnested
```

`typeof()` returns the type as a string, e.g.
`array(row(_schema_version varchar, numbers bigint, true_or_false boolean, words varchar))` for a
contexts run, or `row(numbers bigint, true_or_false boolean, words varchar)` (no outer `array(...)`,
no `_schema_version`) for an unstruct run. Compare it field-for-field against the good-table column type
(validation checklist: `reference/column-classification-and-validation.md`).

---

## Full INSERT template (Phase 7 — the generator emits this)

Prefer the bundled `scripts/generate_insert.py`, which builds the wide column list deterministically
from both DESCRIBE outputs. This is the shape it produces (and the fallback to build inline when code
execution is unavailable). Escape single quotes in `app_id` and the schema URI as `''`. Place any
`--` comment on a **leading** line of a SELECT expression, never trailing (a trailing comment eats
the comma that joins expressions).

```sql
INSERT INTO <good_database>.<good_table> (
    <comma-separated column list, failure column excluded>
)
WITH prep AS (
    SELECT *
    FROM <failed_database>.<failed_table>
    WHERE load_tstamp > current_timestamp - interval '<days>' day
      AND app_id = '<app_id>'
      AND contexts_com_snowplowanalytics_snowplow_failure_1[1].schema
          = '<failing_schema_uri>'
),
unnested AS (
    SELECT
        prep.*,
        failure_ctx.data AS failure_data
    FROM prep
    CROSS JOIN UNNEST(contexts_com_snowplowanalytics_snowplow_failure_1) AS t(failure_ctx)
)
SELECT
    <comma-separated SELECT expressions in the same column order>
FROM unnested;
```

- `<good_database>.<good_table>` is the **staging** table (staging path) or the **good** table
  (direct path); columns are always classified from the **good-table** DESCRIBE. Set
  `TARGET_DATABASE`/`TARGET_TABLE` on the generator for the staging path.
- The recovery column carries the validated Phase-4 expression; the failure column is excluded
  entirely; `load_tstamp` is refreshed to `current_timestamp`.
- How each column is classified (pass-through vs null-cast, Hive→Athena type conversion,
  reserved-word quoting): `reference/column-classification-and-validation.md`.

---

## Staging-table patterns (Phase 6 / Phase 8 — staging write path)

The staging table mirrors the good table's schema exactly, so the recovery INSERT (retargeted to
staging) and the final promote both line up positionally.

### 1. Create an empty staging table that mirrors the good table

```sql
CREATE TABLE <staging_database>.<staging_table>
WITH (
    table_type = 'ICEBERG',
    location = 's3://<your-bucket>/<path>/<staging_table>/',
    is_external = false
)
AS SELECT * FROM <good_database>.<good_table> WHERE 1 = 0;
```

`WHERE 1 = 0` clones the schema and column order with zero rows. Ask the user for the S3 location.

### 2. Recovery INSERT into staging

Same generated INSERT (above), but targeting the staging table — run the generator with
`TARGET_DATABASE`/`TARGET_TABLE` set to the staging table (column classification still comes from
the good-table DESCRIBE).

### 3. Validate the staging table

```sql
SELECT COUNT(*) AS rows_in_staging FROM <staging_database>.<staging_table>;

-- contexts source (ARRAY column):  SELECT event_id, <recovery_column>[1] ...   (1-based)
-- unstruct source (ROW column):    SELECT event_id, <recovery_column>    ...   (no [1])
SELECT event_id, <recovery_column>
FROM <staging_database>.<staging_table>
LIMIT 10;
```

### 4. Promote staging → good table

Column order matches (staging was cloned from good), so `SELECT *` is safe:

```sql
INSERT INTO <good_database>.<good_table>
SELECT * FROM <staging_database>.<staging_table>;
```

### 5. (Optional) drop the staging table afterwards

```sql
DROP TABLE <staging_database>.<staging_table>;
```

### Direct-path time travel (compare before/after)

Iceberg time travel compares the good table before/after a direct-path INSERT:

```sql
SELECT COUNT(*) FROM <good_db>.<good_table> FOR SYSTEM_TIME AS OF TIMESTAMP '2024-01-15 12:00:00'
```

---

## Duplicate pre-check (Phase 2 / Phase 8 — run before the INSERT)

Confirms the events haven't already been recovered. The events being recovered only exist because
they failed validation, so any of their `event_id`s already present in the good table means a prior
recovery — running again would duplicate them. **Expect 0.**

```sql
SELECT COUNT(*) AS already_in_good
FROM <good_database>.<good_table>
WHERE event_id IN (
    SELECT event_id
    FROM <failed_database>.<failed_table>
    WHERE load_tstamp > current_timestamp - interval '<days>' day
      AND app_id = '<app_id>'
      AND contexts_com_snowplowanalytics_snowplow_failure_1[1].schema = '<failing_schema_uri>'
);
```

If it returns > 0, list the overlapping `event_id`s and warn before proceeding:

```sql
SELECT g.event_id
FROM <good_database>.<good_table> g
WHERE g.event_id IN (
    SELECT event_id FROM <failed_database>.<failed_table>
    WHERE load_tstamp > current_timestamp - interval '<days>' day
      AND app_id = '<app_id>'
      AND contexts_com_snowplowanalytics_snowplow_failure_1[1].schema = '<failing_schema_uri>'
);
```

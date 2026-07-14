# SQL reference — BigQuery failed-events recovery

Load this file when you reach the SQL-generation phase. It holds the canonical column set, the
JSON/array-navigation rules, the strict-struct-typing rules, the metadata-driven INSERT generator,
and statement templates that must be applied exactly.

Like the Snowflake flow, there is **no local script**: the INSERT-generator query below reads both
tables' `INFORMATION_SCHEMA.COLUMNS` and builds the wide INSERT itself. Like the Athena flow,
BigQuery struct typing is **strict**: the reconstructed entity's field names, types, and order must
match the target column's type exactly. Use the templates here directly for every phase.

---

## The failure context column

Always named `contexts_com_snowplowanalytics_snowplow_failure_1`. In BigQuery it is a **native**
`ARRAY<STRUCT<...>>` (`REPEATED RECORD`), and its `data` field is a **`JSON`** value. Access it with
native array/struct/JSON syntax:

- **`[0]`** — arrays are **zero-based** (direct index; errors if out of range). The failure context
  is normally a single-element array, so `contexts_..._failure_1[0]` is the failure struct. Use
  `[SAFE_OFFSET(0)]` to get NULL instead of an error on an empty array.
- **`.field`** — navigate into a struct field, e.g. `...failure_1[0].data`, `...failure_1[0].schema`.
- **JSON extraction** — `data` is `JSON`. Read a typed scalar out of it with the extraction
  functions, which navigate **and** cast in one call:
  - `INT64(...failure_1[0].data.user_id)` → INT64
  - `STRING(...failure_1[0].data.user_name)` → STRING
  - `FLOAT64(...)`, `BOOL(...)` for the other scalar types
  - `JSON_VALUE(...)` returns the value as STRING; `JSON_QUERY(...)` returns a JSON subtree;
    `JSON_QUERY_ARRAY(...)` returns an array of JSON for a JSON array field.

  A failed extraction/cast (e.g. `INT64()` on a non-numeric JSON value) raises an error at query
  time — so a query that runs clean has already proved the casts hold.

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

To expand a multi-element failure array (or to read `errors` row-by-row), use `UNNEST`:
`FROM <table>, UNNEST(contexts_com_snowplowanalytics_snowplow_failure_1) AS f`, then read `f.data`,
`f.errors`, `f.schema`, etc. For reconstructing the entity, direct `[0]` indexing is simpler and is
what the recovery expression uses.

---

## Strict struct typing (the key BigQuery constraint)

Entity columns are native `ARRAY<STRUCT<...>>`. A positional `INSERT ... SELECT` requires the
SELECT's output type for each column to **match the target column's type exactly**. For the
reconstructed entity this means the `STRUCT` you build must have the **same field names, the same
field types, and the same field order** as the target column's declared type.

Get that contract from the recovery column's `data_type` in `INFORMATION_SCHEMA` (see "Finding the
recovery column name"), e.g.:

```
ARRAY<STRUCT<_schema_version STRING, user_id INT64, user_name STRING>>
```

Build the recovery expression to match it field-for-field, in order. Never guess the shape — read it
from this `data_type` and/or a validated good-table row.

---

## Column classification (done by the generator query, not by hand)

For the INSERT, every column in the **good** events table is classified. In BigQuery this is done by
the generator query joining `good` and `failed` `INFORMATION_SCHEMA.COLUMNS`, per column in
good-table `ordinal_position` order:

1. **The failure column** (`contexts_com_snowplowanalytics_snowplow_failure_1`) →
   `CAST(NULL AS <good data_type>)` — kept in position (positional INSERT), always null in recovered
   rows.
2. **`load_tstamp`** → `CURRENT_TIMESTAMP()` (fresh timestamp for reprocessed rows).
3. **The recovery column** (the entity being repaired) → the confirmed `[STRUCT(...)]` expression.
4. **Column present in both good and failed tables** → pass through by name (covers all atomic
   columns and any custom column that holds values for events which failed on a *different* entity).
5. **Column only in the good table** → `CAST(NULL AS <target_data_type>)`.

Because the failed events table already contains every atomic column, the good-vs-failed join does
the atomic-vs-custom classification for you — there is no separate atomic list to maintain. The
canonical atomic set is retained at the bottom of this file for reference only.

---

## Statement templates

### Time window (applies to every query below)

Bounding `load_tstamp` scans far less data (and prunes partitions on the typically
`load_tstamp`-partitioned tables). Ask the user in Phase 1 **when the failures occurred** and prefer
a **bounded range**:

```sql
load_tstamp >= TIMESTAMP('2026-03-01') AND load_tstamp < TIMESTAMP('2026-03-10')
```

If the timeframe is unknown, fall back to a **lookback**:

```sql
DATE(load_tstamp) > DATE_SUB(CURRENT_DATE(), INTERVAL <days> DAY)
```

Use the **same** window in every query (identification, inspection, test, INSERT, duplicate
pre-check) so the identified, validated, and recovered sets match.

### Identification query — summary (Step 1, default)

Lead with this: one row per schema × app_id × failure type with volumes and time span. Best for
surveying what's failing without dumping every row, especially over a wide window.

```sql
SELECT
    contexts_com_snowplowanalytics_snowplow_failure_1[0].schema       AS failing_schema,
    app_id,
    contexts_com_snowplowanalytics_snowplow_failure_1[0].failure_type AS failure_type,
    COUNT(*)                                                          AS failed_events,
    MIN(contexts_com_snowplowanalytics_snowplow_failure_1[0].timestamp) AS first_seen,
    MAX(contexts_com_snowplowanalytics_snowplow_failure_1[0].timestamp) AS last_seen
FROM `<failed_project>.<failed_dataset>.<failed_table>`
WHERE DATE(load_tstamp) > DATE_SUB(CURRENT_DATE(), INTERVAL <days> DAY)  -- or a bounded range (see Time window)
  AND contexts_com_snowplowanalytics_snowplow_failure_1 IS NOT NULL
GROUP BY 1, 2, 3
ORDER BY failed_events DESC;
```

### Identification query — row-level drill-in (after a schema is chosen)

Once the user picks a failing schema, inspect *why* it's failing for that schema + app_id. Scoped,
so it returns a handful of rows, not a flood.

```sql
SELECT
    contexts_com_snowplowanalytics_snowplow_failure_1[0].failure_type AS failure_type,
    contexts_com_snowplowanalytics_snowplow_failure_1[0].errors       AS errors,
    contexts_com_snowplowanalytics_snowplow_failure_1[0].data         AS data
FROM `<failed_project>.<failed_dataset>.<failed_table>`
WHERE DATE(load_tstamp) > DATE_SUB(CURRENT_DATE(), INTERVAL <days> DAY)  -- or a bounded range
  AND contexts_com_snowplowanalytics_snowplow_failure_1[0].schema = '<failing_schema_uri>'
  AND app_id = '<app_id>'
LIMIT 10;
```

### Payload inspection query (Step 2)

Expands the failure context with `UNNEST` so the user sees the real value behind each field. Read
`data` scalars with the JSON extraction functions.

```sql
SELECT
    f.schema                    AS failing_schema,
    STRING(f.data.field1)       AS field1,
    STRING(f.data.field2)       AS field2
FROM `<failed_project>.<failed_dataset>.<failed_table>`,
     UNNEST(contexts_com_snowplowanalytics_snowplow_failure_1) AS f
WHERE DATE(load_tstamp) > DATE_SUB(CURRENT_DATE(), INTERVAL <days> DAY)
  AND f.schema = '<failing_schema_uri>'
  AND app_id = '<app_id>'
LIMIT 10;
```

(`STRING(...)` is used here just to render every field readably; the real target types come from the
recovery column's `data_type`.)

### Inspect a validated event (ground the target shape in the schema)

The repaired entity's shape and field types are defined by the Iglu schema and materialised in the
recovery column's type — not chosen. Cross-check by pulling a recent **validated** event of the same
entity from the **good** table. `<recovery_column>` is the entity column found below.

```sql
SELECT
    event_id,
    <recovery_column>[0] AS validated_entity
FROM `<good_project>.<good_dataset>.<good_table>`
WHERE ARRAY_LENGTH(<recovery_column>) > 0
LIMIT 10;
```

The authoritative contract is the recovery column's `data_type` (field names, types, order). If the
good table has **no** validated events for this entity yet (e.g. a brand-new schema) and the
`data_type` is ambiguous, fall back to reading the field types and `required` list from the **Iglu
schema JSON** — ask the user for it or its URL. Do not infer types from the failed payload alone
(those values are exactly what failed validation).

### Recovery expression pattern (Step 3) — build & confirm with the user

Reconstruct the repaired entity as an array of one struct. `_schema_version` is **mandatory** (first
field) for the BigQuery Streaming Loader. Field names, types, and order must match the recovery
column's `data_type` exactly. Extract each field from the JSON `data` with the right function.

```sql
[STRUCT(
    '1-0-0'                                                                      AS _schema_version,  -- corrected version
    INT64(contexts_com_snowplowanalytics_snowplow_failure_1[0].data.user_id)     AS user_id,
    STRING(contexts_com_snowplowanalytics_snowplow_failure_1[0].data.user_name)  AS user_name
)]
```

`ARRAY(SELECT AS STRUCT ...)` is an equivalent form, but the array literal `[STRUCT(...)]` is
simplest for a single element. Do **not** append `AS <column_name>` here — the generator adds it.
When embedding this into the generator query (Phase 7), **wrap the whole expression in double
quotes** so its single quotes (`'1-0-0'`) need no escaping — ensure the expression itself contains no
double-quote characters (JSON paths use dot navigation, so it won't).

### Test SELECT (run and validate before the INSERT)

Emit this so the user can prove the reconstructed column is correct before any INSERT is generated.

```sql
WITH prep AS (
    SELECT *
    FROM `<failed_project>.<failed_dataset>.<failed_table>`
    WHERE DATE(load_tstamp) > DATE_SUB(CURRENT_DATE(), INTERVAL <days> DAY)
      AND app_id = '<app_id>'
      AND contexts_com_snowplowanalytics_snowplow_failure_1[0].schema = '<failing_schema_uri>'
)
SELECT
    event_id,
    <recovery expression> AS recovered
FROM prep
LIMIT 20;
```

Because BigQuery raises an error on a failed extraction/cast, a test SELECT that runs at all already
proves the casts hold. BigQuery has no `TYPEOF`; validate the struct **shape** by comparing the
reconstructed fields against the recovery column's `data_type`, and eyeball the values.

### Validation checklist (the gate)

Confirm all of these against the pasted test output before generating the INSERT:

1. **The query ran clean (decisive).** Every extraction/cast succeeded — BigQuery errors on a bad
   cast rather than silently returning null, so a successful run is strong evidence the types match.
2. **Struct shape matches the target** — the reconstructed struct's field names, types, and order
   match the recovery column's `data_type` exactly (this is what the positional INSERT requires), and
   `_schema_version` is present as the first field.
3. **Row count** equals the failed-event count for this schema + app_id (from the identification query).
4. **No unexpected nulls** in `recovered` — a null usually means a wrong JSON path. Investigate.
5. **Values are corrected** — the repaired field holds the fixed value, and pass-through fields are intact.

If any check fails, fix the recovery expression and re-run the test SELECT. Only generate the INSERT
once the casts hold, the shape matches, and the values are correct.

---

## INSERT-generator query (Phase 7 — BigQuery builds the INSERT itself)

This joins the good and failed tables' `INFORMATION_SCHEMA.COLUMNS`, classifies each good-table
column, and emits the **full INSERT statement as a single text cell**. The user runs it, copies the
returned statement, and runs that.

Fill in every `<placeholder>`. The recovery expression is embedded as a **double-quoted** string
literal (so its single quotes need no escaping). The outer `FORMAT(""" ... """)` template is
triple-quoted, so the single quotes around `<app_id>` / `<failing_schema_uri>` inside it are literal.

```sql
WITH good_cols AS (
    SELECT ordinal_position, column_name, data_type
    FROM `<good_project>.<good_dataset>`.INFORMATION_SCHEMA.COLUMNS
    WHERE table_name = '<good_table>'
),
failed_cols AS (
    SELECT column_name
    FROM `<failed_project>.<failed_dataset>`.INFORMATION_SCHEMA.COLUMNS
    WHERE table_name = '<failed_table>'
),
mapping AS (
    SELECT
        g.ordinal_position,
        CASE
            WHEN LOWER(g.column_name) = 'contexts_com_snowplowanalytics_snowplow_failure_1'
                THEN FORMAT('CAST(NULL AS %s) AS %s', g.data_type, g.column_name)
            WHEN LOWER(g.column_name) = 'load_tstamp'
                THEN 'CURRENT_TIMESTAMP() AS load_tstamp'
            WHEN LOWER(g.column_name) = LOWER('<recovery_column>')
                THEN "<recovery_expression> AS <recovery_column>"
            WHEN f.column_name IS NOT NULL
                THEN g.column_name
            ELSE FORMAT('CAST(NULL AS %s) AS %s', g.data_type, g.column_name)
        END AS select_expression
    FROM good_cols g
    LEFT JOIN failed_cols f ON LOWER(g.column_name) = LOWER(f.column_name)
),
select_clause AS (
    SELECT STRING_AGG(select_expression, ',\n    ' ORDER BY ordinal_position) AS columns_sql
    FROM mapping
)
SELECT FORMAT("""INSERT INTO `<target_project>.<target_dataset>.<target_table>`
WITH prep AS (
  SELECT *
  FROM `<failed_project>.<failed_dataset>.<failed_table>`
  WHERE DATE(load_tstamp) > DATE_SUB(CURRENT_DATE(), INTERVAL <days> DAY)
    AND app_id = '<app_id>'
    AND contexts_com_snowplowanalytics_snowplow_failure_1[0].schema = '<failing_schema_uri>'
)
SELECT
    %s
FROM prep;""", columns_sql) AS generated_sql
FROM select_clause;
```

Notes:
- `<target_*>` is the **staging** table (staging path) or the **good** table (direct path). Column
  classification always comes from the **good** table, so a staging table created `LIKE` the good
  table lines up positionally.
- The generated INSERT is **positional** (no explicit column list): the SELECT expressions are
  emitted in good-table `ordinal_position` order, so they land in the right columns. This is exactly
  why the recovery struct's field order must match the target type.
- `<recovery_expression>` is the validated Phase-4 expression, inserted verbatim inside the double
  quotes; `<recovery_column>` is the entity column name.
- Before the user runs the generated INSERT, review it: recovery column carries the expression,
  failure column nulled, `load_tstamp` refreshed, and the column count matches the good table. If the
  recovery column wasn't matched (its expression is missing), check `<recovery_column>` against the
  good table's actual column name and regenerate.
- **If `CAST(NULL AS <data_type>)` for the failure column is rejected** (its full
  `ARRAY<STRUCT<... data JSON ...>>` type is unusual), fall back to a simplified null of the same
  shape — `CAST(NULL AS ARRAY<STRUCT<schema STRING, data JSON>>)` — or give the INSERT an explicit
  column list that omits the failure column entirely (BigQuery leaves unlisted columns NULL).

---

## Staging-table patterns (staging write path)

### 1. Create an empty staging table that mirrors the good table

`LIKE` copies the exact column set, types, and order with zero rows:

```sql
CREATE TABLE `<staging_project>.<staging_dataset>.<staging_table>`
LIKE `<good_project>.<good_dataset>.<good_table>`;
```

### 2. Recovery INSERT into staging

Same generated INSERT, but with `<target_*>` set to the staging table (column classification still
comes from the good table).

### 3. Validate the staging table

```sql
SELECT COUNT(*) AS rows_in_staging FROM `<staging_project>.<staging_dataset>.<staging_table>`;

SELECT event_id, <recovery_column>[0]
FROM `<staging_project>.<staging_dataset>.<staging_table>`
LIMIT 10;
```

### 4. Promote staging → good table

Column order matches (staging was created `LIKE` good), so `SELECT *` is safe:

```sql
INSERT INTO `<good_project>.<good_dataset>.<good_table>`
SELECT * FROM `<staging_project>.<staging_dataset>.<staging_table>`;
```

### 5. (Optional) drop the staging table afterwards

```sql
DROP TABLE `<staging_project>.<staging_dataset>.<staging_table>`;
```

---

## Duplicate pre-check (run before the INSERT)

Confirms the events haven't already been recovered. The events being recovered only exist because
they failed validation, so any of their `event_id`s already present in the good table means a prior
recovery — running again would duplicate them. **Expect 0.**

```sql
SELECT COUNT(*) AS already_in_good
FROM `<good_project>.<good_dataset>.<good_table>`
WHERE event_id IN (
    SELECT event_id
    FROM `<failed_project>.<failed_dataset>.<failed_table>`
    WHERE DATE(load_tstamp) > DATE_SUB(CURRENT_DATE(), INTERVAL <days> DAY)
      AND app_id = '<app_id>'
      AND contexts_com_snowplowanalytics_snowplow_failure_1[0].schema = '<failing_schema_uri>'
);
```

If it returns > 0, list the overlapping `event_id`s and warn before proceeding:

```sql
SELECT g.event_id
FROM `<good_project>.<good_dataset>.<good_table>` g
WHERE g.event_id IN (
    SELECT event_id
    FROM `<failed_project>.<failed_dataset>.<failed_table>`
    WHERE DATE(load_tstamp) > DATE_SUB(CURRENT_DATE(), INTERVAL <days> DAY)
      AND app_id = '<app_id>'
      AND contexts_com_snowplowanalytics_snowplow_failure_1[0].schema = '<failing_schema_uri>'
);
```

---

## Finding the recovery column name and its type (query, don't guess)

Snowplow materialises a custom entity into a column named
`contexts_<vendor>_<schema_name>_<major_version>`, with dots in the vendor replaced by underscores —
so `iglu:com.example/user_entity/jsonschema/2-0-1` *should* map to
`contexts_com_example_user_entity_2`. But casing and edge cases vary, and this is the
`<recovery_column>` placeholder used everywhere downstream, so **never confirm it from memory — have
the user run this and read the real name and `data_type` back**:

```sql
SELECT column_name, data_type
FROM `<good_project>.<good_dataset>`.INFORMATION_SCHEMA.COLUMNS
WHERE table_name = '<good_table>'
  AND LOWER(column_name) LIKE '%<schema_name_with_underscores>%'
ORDER BY column_name;
```

e.g. `LIKE '%user_entity%'`. The returned `column_name` is the exact `<recovery_column>`, and its
`data_type` (e.g. `ARRAY<STRUCT<_schema_version STRING, user_id INT64, user_name STRING>>`) is the
struct contract the recovery expression must match. If several rows come back (multiple major
versions), pick the one whose trailing `_<major>` matches the schema version being recovered.

---

## Canonical atomic column set (reference only)

In BigQuery the good-vs-failed join classifies columns for you, so this list is informational — it is
the set of columns that are "atomic" (always present, pass through by name). Anything else is a
custom `contexts_*` / `unstruct_event_*` column.

```
app_id platform etl_tstamp collector_tstamp dvce_created_tstamp
event event_id txn_id name_tracker v_tracker v_collector v_etl
user_id user_ipaddress user_fingerprint domain_userid domain_sessionidx
network_userid geo_country geo_region geo_city geo_zipcode
geo_latitude geo_longitude geo_region_name ip_isp ip_organization
ip_domain ip_netspeed page_url page_title page_referrer
page_urlscheme page_urlhost page_urlport page_urlpath page_urlquery
page_urlfragment refr_urlscheme refr_urlhost refr_urlport refr_urlpath
refr_urlquery refr_urlfragment refr_medium refr_source refr_term
mkt_medium mkt_source mkt_term mkt_content mkt_campaign
se_category se_action se_label se_property se_value
tr_orderid tr_affiliation tr_total tr_tax tr_shipping
tr_city tr_state tr_country ti_orderid ti_sku ti_name
ti_category ti_price ti_quantity pp_xoffset_min pp_xoffset_max
pp_yoffset_min pp_yoffset_max useragent br_name br_family
br_version br_type br_renderengine br_lang br_features_pdf
br_features_flash br_features_java br_features_director
br_features_quicktime br_features_realplayer br_features_windowsmedia
br_features_gears br_features_silverlight br_cookies br_colordepth
br_viewwidth br_viewheight os_name os_family os_manufacturer
os_timezone dvce_type dvce_ismobile dvce_screenwidth dvce_screenheight
doc_charset doc_width doc_height tr_currency tr_total_base
tr_tax_base tr_shipping_base ti_currency ti_price_base base_currency
geo_timezone mkt_clickid mkt_network etl_tags dvce_sent_tstamp
refr_domain_userid refr_dvce_tstamp domain_sessionid derived_tstamp
event_vendor event_name event_format event_version event_fingerprint
true_tstamp load_tstamp
```

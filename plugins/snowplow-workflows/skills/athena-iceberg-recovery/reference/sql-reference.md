# SQL reference — Athena Iceberg failed-events recovery

Load this file when you reach the SQL-generation phase. It holds the canonical
column set, type-conversion rules, reserved words, and statement templates that
must be applied exactly.

The bundled `scripts/generate_insert.py` implements the column-classification and
type-conversion rules below — prefer running it for the full INSERT (Phase 6). Use
the rules here directly for the identification/inspection/test queries, and as the
fallback when code execution is unavailable.

---

## The failure context column

Always named `contexts_com_snowplowanalytics_snowplow_failure_1`. In an Iceberg
table it is a native `ARRAY(ROW(...))`, **not** a JSON string. Fields of each ROW:

| Field | Type | Notes |
|---|---|---|
| `_schema_version` | `VARCHAR` | |
| `component_name` | `VARCHAR` | Pipeline component that rejected the event |
| `component_version` | `VARCHAR` | |
| `data` | `VARCHAR` | Original event payload as a JSON string — read with `json_extract_scalar` |
| `errors` | `ARRAY(ROW(...))` | Structured validation errors |
| `failure_type` | `VARCHAR` | e.g. `SchemaViolation` |
| `schema` | `VARCHAR` | Iglu URI of the schema that failed validation |
| `timestamp` | `TIMESTAMP` | When the failure occurred |

Access the first element with 1-indexed notation: `contexts_..._failure_1[1].schema`.
Athena does **not** support deep nested dot notation (`col[1].data.field`) in a
`SELECT` — you must `UNNEST` the array first, then `json_extract_scalar` the `data` field.

---

## Column classification (for the INSERT)

For every column in the **good** events table DESCRIBE, decide its SELECT expression:

1. **The failure column** (`contexts_com_snowplowanalytics_snowplow_failure_1`) → **exclude entirely** from both the column list and the SELECT.
2. **`load_tstamp`** → `current_timestamp AS load_tstamp` (fresh timestamp for reprocessed rows).
3. **The recovery column** (the entity being repaired) → the user's confirmed `CAST(ARRAY[ROW(...)] AS ARRAY(ROW(...)))` expression, followed by `AS <column_name>`. Strip any trailing `AS <column_name>` the user already included before re-adding it.
4. **Atomic column** (present in the canonical atomic set below) → pass through by name.
5. **Custom column also present in the failed table DESCRIBE** → pass through by name (it can hold values for events that failed on a *different* entity).
6. **Custom column only in the good table** → `CAST(NULL AS <athena_type>) AS <column_name>`.

After classifying, report a sanity summary to the user:
`good columns: N | failed columns: M | custom pass-through: P | custom nulled: Q`.
Warn if the recovery column name is not found in the good DESCRIBE.

---

## Hive → Athena type conversion

DESCRIBE output uses Hive DDL types. Convert to Athena query-expression types for the
`CAST(NULL AS ...)` cases:

| Hive (DESCRIBE) | Athena (INSERT expression) |
|---|---|
| `string` | `varchar` |
| `int` | `integer` |
| `array<...>` | `array(...)` |
| `struct<...>` | `row(...)` |
| `field:type` (colon separator) | `field type` (space separator) |
| `decimal(18, 2)` | `decimal(18,2)` (no space) |

Mechanical steps: `array<`→`array(`, `struct<`→`row(`, every `>`→`)`, `name:`→`name `,
`string`→`varchar`, `int`→`integer`, remove the space inside `decimal(p, s)`.

### Reserved words inside ROW(...) definitions

If a struct **field name** equals a reserved word, it must be double-quoted or Athena
rejects the type, e.g. `row("timestamp" varchar, ...)`. Quote a field name when it (case-insensitive) is one of:

```
current_time current_date current_timestamp timestamp date time interval
year month day hour minute second zone value values table column row type
position filter over partition range select from where group order by having
limit union all distinct as in not and or is null true false case when then
else end exists between like natural cross join inner outer left right full on using
```

Confirmed real cases seen in Snowplow tables: `"current_time"` (media_player_2),
`"timestamp"` (spamreport_3, geolocation_context_1), `"all"` (consent_withdrawn_1),
`"type"` (ecommerce_action_1), `"value"` (http_cookie_1, http_header_1),
`"position"` (ecommerce_product_1).

---

## Parsing DESCRIBE output

- Lines may be **tab-separated** or **space-separated** depending on how the user copied them. For space-separated, split only on the *first* run of whitespace (`col_name` then the rest is the type) so complex types containing spaces stay intact.
- Skip blank lines and `#` comment lines.
- **Stop parsing** at a line starting with `# Partition spec` or `# Partition information` — everything after is the partition spec, not columns.

---

## Statement templates

### Time window (applies to every query below)

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

### Identification query — summary (Step 1, default)

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

### Identification query — row-level drill-in (after a schema is chosen)

Once the user picks a failing schema, inspect *why* it's failing for that schema + app_id. Scoped,
so it returns a handful of rows, not a flood.

```sql
SELECT
    contexts_com_snowplowanalytics_snowplow_failure_1[1].failure_type AS failure_type,
    contexts_com_snowplowanalytics_snowplow_failure_1[1].errors       AS errors,
    contexts_com_snowplowanalytics_snowplow_failure_1[1].data         AS data
FROM <failed_database>.<failed_table>
WHERE load_tstamp > current_timestamp - interval '<days>' day   -- or a bounded range (see Time window)
  AND contexts_com_snowplowanalytics_snowplow_failure_1[1].schema = '<failing_schema_uri>'
  AND app_id = '<app_id>'
LIMIT 10
```

### Payload inspection query (Step 2)

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

### Field profiling & coercibility confirmation (Step 2.5 — gate before building the expression)

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

### Recovery expression pattern (Step 3) — build & confirm with the user

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

Entity columns must be constructed as `CAST(ARRAY[ROW(...)] AS ARRAY(ROW(...)))`.
The `ARRAY<STRUCT<...>>` form in DESCRIBE output is DDL-only and invalid in a query.
Do **not** append `AS <column_name>` here — the generator adds it.

### Test SELECT + type validation (run and validate before the INSERT)

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
`array(row(_schema_version varchar, numbers bigint, true_or_false boolean, words varchar))`.

### Validation checklist (the gate)

Confirm all of these against the pasted test output before generating the INSERT:

1. **Type match (decisive).** `recovered_type` must equal the good-table column's type after
   Hive→Athena conversion — same field **names, types, and order**. Athena inserts positionally into
   a struct, so order matters. If `typeof` matches the target column type, the INSERT will type-check.
2. **Row count** equals the failed-event count for this schema + app_id (from the identification query).
3. **No unexpected nulls** in `recovered` — a null usually means a wrong `$.json_path` or a cast that
   silently returned null. Investigate before proceeding.
4. **Values are corrected** — the repaired field holds the fixed value (e.g. a proper integer, not a
   quoted string), and pass-through fields are intact.

If any check fails, fix the recovery expression and re-run the test SELECT. Only generate the INSERT
once the type matches and values are correct.

### Full INSERT template

Escape single quotes in `app_id` and the schema URI as `''`. Place any `--` comment
on a **leading** line of a SELECT expression, never trailing (a trailing comment eats
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

---

## Staging-table patterns (staging write path)

When the user chooses to write to a staging table first, use these. The staging table mirrors the
good table's schema exactly, so the recovery INSERT (retargeted to staging) and the final promote
both line up positionally.

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

Same generated INSERT, but targeting the staging table — run the generator with
`TARGET_DATABASE`/`TARGET_TABLE` set to the staging table (column classification still comes from
the good-table DESCRIBE).

### 3. Validate the staging table

```sql
SELECT COUNT(*) AS rows_in_staging FROM <staging_database>.<staging_table>;

SELECT event_id, <recovery_column>[1]
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

---

## Duplicate pre-check (run before the INSERT)

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

---

## Canonical atomic column set

A column is "atomic" (pass through by name, never null-cast) if its name is in this set.
Anything else is a custom `contexts_*` / `unstruct_event_*` column and follows the
pass-through-vs-null rule.

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

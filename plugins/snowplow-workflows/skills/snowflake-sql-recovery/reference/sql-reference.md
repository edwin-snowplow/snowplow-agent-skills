# SQL reference — Snowflake failed-events recovery

Load this file when you reach the SQL-generation phase. It holds the canonical column set, the
VARIANT-navigation rules, the metadata-driven INSERT generator, and statement templates that must be
applied exactly.

Unlike the Athena flow, there is **no local script**. The INSERT-generator query below reads both
tables' `INFORMATION_SCHEMA.COLUMNS` and builds the wide INSERT itself. Use the templates here
directly for every phase.

---

## The failure context column

Always named `contexts_com_snowplowanalytics_snowplow_failure_1`. In Snowflake it is a
semi-structured `ARRAY` of `OBJECT`s (VARIANT), **not** a native struct. Access it with VARIANT
syntax, not SQL dot notation:

- **`[0]`** — arrays are **0-indexed** (Athena is 1-indexed). The failure context is normally a
  single-element array, so `contexts_..._failure_1[0]` is the failure object.
- **`:`** — navigate into a VARIANT/OBJECT key, e.g. `...failure_1[0]:data`, `...failure_1[0]:schema`.
- **`::TYPE`** — cast a VARIANT value to a SQL type, e.g. `...:data:user_id::INTEGER`,
  `...:schema::STRING`.

Fields of each failure object:

| Key | Meaning |
|---|---|
| `_schema_version` | Version of the failure context schema |
| `component_name` | Pipeline component that rejected the event |
| `component_version` | |
| `data` | Original event payload as VARIANT — read fields with `:field::TYPE` |
| `errors` | Structured validation errors (array) |
| `failure_type` | e.g. `SchemaViolation` |
| `schema` | Iglu URI of the schema that failed validation |
| `timestamp` | When the failure occurred |

To expand a multi-element failure array (or to read `errors`), use `LATERAL FLATTEN`:
`FROM <table>, LATERAL FLATTEN(input => contexts_com_snowplowanalytics_snowplow_failure_1) f`, then
read `f.value:data`, `f.value:errors`, etc. For reconstructing the entity, direct `[0]` indexing is
simpler and is what the recovery expression uses.

---

## Column classification (done by the generator query, not by hand)

For the INSERT, every column in the **good** events table is classified. In Snowflake this is done
by the generator query joining `good` and `failed` `INFORMATION_SCHEMA.COLUMNS`, per column in
good-table `ordinal_position` order:

1. **The failure column** (`contexts_com_snowplowanalytics_snowplow_failure_1`) → `CAST(NULL AS ...)`
   — kept in position (positional INSERT), always null in recovered rows.
2. **`load_tstamp`** → `CURRENT_TIMESTAMP()` (fresh timestamp for reprocessed rows).
3. **The recovery column** (the entity being repaired) → the confirmed
   `ARRAY_CONSTRUCT(OBJECT_CONSTRUCT(...))` expression.
4. **Column present in both good and failed tables** → pass through by name (covers all atomic
   columns and any custom column that holds values for events which failed on a *different* entity).
5. **Column only in the good table** → `CAST(NULL AS <target_data_type>)`.

Because the failed events table already contains every atomic column, the good-vs-failed join does
the atomic-vs-custom classification for you — there is no separate atomic list to maintain and no
Hive→engine type conversion. The canonical atomic set is retained at the bottom of this file for
reference only.

---

## Statement templates

### Time window (applies to every query below)

Bounding `load_tstamp` scans far less data. Ask the user in Phase 1 **when the failures occurred**
and prefer a **bounded range**:

```sql
load_tstamp >= '2026-03-01'::TIMESTAMP AND load_tstamp < '2026-03-10'::TIMESTAMP
```

If the timeframe is unknown, fall back to a **lookback**:

```sql
load_tstamp::DATE > CURRENT_DATE - INTERVAL '<days> DAY'
```

Use the **same** window in every query (identification, inspection, test, INSERT, duplicate
pre-check) so the identified, validated, and recovered sets match.

### Identification query — summary (Step 1, default)

Lead with this: one row per schema × app_id × failure type with volumes and time span. Best for
surveying what's failing without dumping every row, especially over a wide window.

```sql
SELECT
    contexts_com_snowplowanalytics_snowplow_failure_1[0]:schema::STRING       AS failing_schema,
    app_id,
    contexts_com_snowplowanalytics_snowplow_failure_1[0]:failure_type::STRING AS failure_type,
    COUNT(*)                                                                  AS failed_events,
    MIN(contexts_com_snowplowanalytics_snowplow_failure_1[0]:timestamp::TIMESTAMP) AS first_seen,
    MAX(contexts_com_snowplowanalytics_snowplow_failure_1[0]:timestamp::TIMESTAMP) AS last_seen
FROM <failed_database>.<failed_schema>.<failed_table>
WHERE load_tstamp::DATE > CURRENT_DATE - INTERVAL '<days> DAY'   -- or a bounded range (see Time window)
  AND contexts_com_snowplowanalytics_snowplow_failure_1 IS NOT NULL
GROUP BY
    contexts_com_snowplowanalytics_snowplow_failure_1[0]:schema::STRING,
    app_id,
    contexts_com_snowplowanalytics_snowplow_failure_1[0]:failure_type::STRING
ORDER BY failed_events DESC;
```

### Identification query — row-level drill-in (after a schema is chosen)

Once the user picks a failing schema, inspect *why* it's failing for that schema + app_id. Scoped,
so it returns a handful of rows, not a flood.

```sql
SELECT
    contexts_com_snowplowanalytics_snowplow_failure_1[0]:failure_type::STRING AS failure_type,
    contexts_com_snowplowanalytics_snowplow_failure_1[0]:errors               AS errors,
    contexts_com_snowplowanalytics_snowplow_failure_1[0]:data                 AS data
FROM <failed_database>.<failed_schema>.<failed_table>
WHERE load_tstamp::DATE > CURRENT_DATE - INTERVAL '<days> DAY'   -- or a bounded range (see Time window)
  AND contexts_com_snowplowanalytics_snowplow_failure_1[0]:schema::STRING = '<failing_schema_uri>'
  AND app_id = '<app_id>'
LIMIT 10;
```

### Payload inspection query (Step 2)

Expands the failure context with `LATERAL FLATTEN` so the user sees the real value behind each field.

```sql
SELECT
    f.value:schema::STRING                AS failing_schema,
    f.value:data:field1::STRING           AS field1,
    f.value:data:field2::STRING           AS field2
FROM <failed_database>.<failed_schema>.<failed_table>,
     LATERAL FLATTEN(input => contexts_com_snowplowanalytics_snowplow_failure_1) f
WHERE load_tstamp::DATE > CURRENT_DATE - INTERVAL '<days> DAY'
  AND f.value:schema::STRING = '<failing_schema_uri>'
  AND app_id = '<app_id>'
LIMIT 10;
```

### Inspect a validated event (ground the target types in the schema)

The repaired entity's shape and field types are defined by the Iglu schema, not chosen. The quickest
way to see them is to pull a recent **validated** event of the same entity from the **good** table
and read its keys and value types. Use this before building the recovery expression whenever the
intended types aren't already obvious. `<recovery_column>` is the entity column found above.

```sql
SELECT
    event_id,
    <recovery_column>[0]                              AS validated_entity,
    TYPEOF(<recovery_column>[0]:field1)               AS field1_type,
    TYPEOF(<recovery_column>[0]:field2)               AS field2_type
FROM <good_database>.<good_schema>.<good_table>
WHERE <recovery_column> IS NOT NULL
  AND ARRAY_SIZE(<recovery_column>) > 0
LIMIT 10;
```

The `TYPEOF()` of each field on a known-good row is the target type your `::TYPE` casts must produce.
If the good table has **no** validated events for this entity yet (e.g. a brand-new schema), fall
back to reading the field types and `required` list from the **Iglu schema JSON** — ask the user for
it or its URL. Do not infer types from the failed payload alone (the values are exactly what failed
validation).

### Recovery expression pattern (Step 3) — build & confirm with the user

Reconstruct the repaired entity as a VARIANT array of objects. `_schema_version` is **mandatory**
(first key) for the Snowflake Streaming Loader. Cast each field with `::TYPE`.

```sql
ARRAY_CONSTRUCT(
    OBJECT_CONSTRUCT(
        '_schema_version', '1-0-0',                                                    -- corrected version
        'field1', contexts_com_snowplowanalytics_snowplow_failure_1[0]:data:field1::STRING,
        'field2', contexts_com_snowplowanalytics_snowplow_failure_1[0]:data:field2::INTEGER
    )
)
```

There is no strict struct-type matching in Snowflake, so no field-order or reserved-word concerns —
the keys are plain string literals. Do **not** append `AS <column_name>` here — the generator adds
it. When embedding this into the generator query's string literal (Phase 7), **double every single
quote** (`'` → `''`).

### Test SELECT + type validation (run and validate before the INSERT)

Emit this so the user can prove the reconstructed column is correct before any INSERT is generated.

```sql
WITH prep AS (
    SELECT *
    FROM <failed_database>.<failed_schema>.<failed_table>
    WHERE load_tstamp::DATE > CURRENT_DATE - INTERVAL '<days> DAY'
      AND app_id = '<app_id>'
      AND contexts_com_snowplowanalytics_snowplow_failure_1[0]:schema::STRING = '<failing_schema_uri>'
)
SELECT
    event_id,
    <recovery expression>                          AS recovered,
    TYPEOF(<recovery expression>[0]:field1)        AS field1_type,
    TYPEOF(<recovery expression>[0]:field2)        AS field2_type
FROM prep
LIMIT 20;
```

`TYPEOF()` returns a VARIANT value's type as a string, e.g. `INTEGER`, `VARCHAR`, `BOOLEAN`. Because
Snowflake raises an error on a failed `::TYPE` cast, a test SELECT that runs at all already proves
the casts hold.

### Validation checklist (the gate)

Confirm all of these against the pasted test output before generating the INSERT:

1. **The query ran clean (decisive).** Every `::TYPE` cast succeeded — Snowflake errors on a bad
   cast rather than silently returning null, so a successful run is strong evidence the types match.
2. **Field types** — each `TYPEOF()` returns the intended type, and `_schema_version` is present in
   the reconstructed object.
3. **Row count** equals the failed-event count for this schema + app_id (from the identification query).
4. **No unexpected nulls** in `recovered` — a null usually means a wrong `:` path. Investigate.
5. **Values are corrected** — the repaired field holds the fixed value, and pass-through fields are intact.

If any check fails, fix the recovery expression and re-run the test SELECT. Only generate the INSERT
once the casts hold and values are correct.

---

## INSERT-generator query (Phase 7 — Snowflake builds the INSERT itself)

This is the centrepiece that replaces the Athena skill's Python script. It joins the good and failed
tables' `INFORMATION_SCHEMA.COLUMNS`, classifies each good-table column, and emits the **full INSERT
statement as a single text cell**. The user runs it, copies the returned statement, and runs that.

Fill in every `<placeholder>`. Note the doubled single quotes inside the generated string literals.

```sql
WITH good_cols AS (
    SELECT ordinal_position, column_name, data_type
    FROM <good_database>.INFORMATION_SCHEMA.COLUMNS
    WHERE LOWER(table_schema) = LOWER('<good_schema>')
      AND LOWER(table_name)   = LOWER('<good_table>')
),
failed_cols AS (
    SELECT column_name
    FROM <failed_database>.INFORMATION_SCHEMA.COLUMNS
    WHERE LOWER(table_schema) = LOWER('<failed_schema>')
      AND LOWER(table_name)   = LOWER('<failed_table>')
),
mapping AS (
    SELECT
        g.ordinal_position,
        CASE
            WHEN LOWER(g.column_name) = 'contexts_com_snowplowanalytics_snowplow_failure_1'
                THEN 'CAST(NULL AS ' || g.data_type || ') AS ' || g.column_name
            WHEN LOWER(g.column_name) = 'load_tstamp'
                THEN 'CURRENT_TIMESTAMP() AS load_tstamp'
            WHEN LOWER(g.column_name) = LOWER('<recovery_column>')
                THEN '<recovery_expression_with_doubled_quotes> AS ' || g.column_name
            WHEN f.column_name IS NOT NULL
                THEN g.column_name
            ELSE 'CAST(NULL AS ' || g.data_type || ') AS ' || g.column_name
        END AS select_expression
    FROM good_cols g
    LEFT JOIN failed_cols f ON LOWER(g.column_name) = LOWER(f.column_name)
),
select_clause AS (
    SELECT LISTAGG(select_expression, ',\n    ')
             WITHIN GROUP (ORDER BY ordinal_position) AS columns_sql
    FROM mapping
)
SELECT
    'INSERT INTO <target_database>.<target_schema>.<target_table>\n'
    || 'WITH prep AS (\n'
    || '    SELECT *\n'
    || '    FROM <failed_database>.<failed_schema>.<failed_table>\n'
    || '    WHERE load_tstamp::DATE > CURRENT_DATE - INTERVAL ''<days> DAY''\n'
    || '      AND app_id = ''<app_id>''\n'
    || '      AND contexts_com_snowplowanalytics_snowplow_failure_1[0]:schema::STRING = ''<failing_schema_uri>''\n'
    || ')\n'
    || 'SELECT\n    '
    || columns_sql
    || '\nFROM prep;' AS insert_statement
FROM select_clause;
```

Notes:
- `<target_*>` is the **staging** table (staging path) or the **good** table (direct path). Column
  classification always comes from the **good** table, so a staging table created `LIKE` the good
  table lines up positionally.
- The generated INSERT is **positional** (no explicit column list): the SELECT expressions are
  emitted in good-table `ordinal_position` order, so they land in the right columns.
- The `<recovery_expression_with_doubled_quotes>` is the Phase-4 recovery expression with every `'`
  replaced by `''`, because it lives inside a SQL string literal here.
- Before the user runs the generated INSERT, review it: recovery column carries the expression,
  failure column nulled, `load_tstamp` refreshed, and the column count matches the good table. If
  the recovery column wasn't matched (its expression is missing), check `<recovery_column>` against
  the good table's actual column name and regenerate.

---

## Staging-table patterns (staging write path)

### 1. Create an empty staging table that mirrors the good table

`LIKE` copies the exact column set, types, and order with zero rows:

```sql
CREATE TABLE <staging_database>.<staging_schema>.<staging_table>
    LIKE <good_database>.<good_schema>.<good_table>;
```

### 2. Recovery INSERT into staging

Same generated INSERT, but with `<target_*>` set to the staging table (column classification still
comes from the good table).

### 3. Validate the staging table

```sql
SELECT COUNT(*) AS rows_in_staging FROM <staging_database>.<staging_schema>.<staging_table>;

SELECT event_id, <recovery_column>[0]
FROM <staging_database>.<staging_schema>.<staging_table>
LIMIT 10;
```

### 4. Promote staging → good table

Column order matches (staging was created `LIKE` good), so `SELECT *` is safe:

```sql
INSERT INTO <good_database>.<good_schema>.<good_table>
SELECT * FROM <staging_database>.<staging_schema>.<staging_table>;
```

### 5. (Optional) drop the staging table afterwards

```sql
DROP TABLE <staging_database>.<staging_schema>.<staging_table>;
```

---

## Duplicate pre-check (run before the INSERT)

Confirms the events haven't already been recovered. The events being recovered only exist because
they failed validation, so any of their `event_id`s already present in the good table means a prior
recovery — running again would duplicate them. **Expect 0.**

```sql
SELECT COUNT(*) AS already_in_good
FROM <good_database>.<good_schema>.<good_table>
WHERE event_id IN (
    SELECT event_id
    FROM <failed_database>.<failed_schema>.<failed_table>
    WHERE load_tstamp::DATE > CURRENT_DATE - INTERVAL '<days> DAY'
      AND app_id = '<app_id>'
      AND contexts_com_snowplowanalytics_snowplow_failure_1[0]:schema::STRING = '<failing_schema_uri>'
);
```

If it returns > 0, list the overlapping `event_id`s and warn before proceeding:

```sql
SELECT g.event_id
FROM <good_database>.<good_schema>.<good_table> g
WHERE g.event_id IN (
    SELECT event_id
    FROM <failed_database>.<failed_schema>.<failed_table>
    WHERE load_tstamp::DATE > CURRENT_DATE - INTERVAL '<days> DAY'
      AND app_id = '<app_id>'
      AND contexts_com_snowplowanalytics_snowplow_failure_1[0]:schema::STRING = '<failing_schema_uri>'
);
```

---

## Finding the recovery column name (query, don't guess)

Snowplow materialises a custom entity into a column named
`contexts_<vendor>_<schema_name>_<major_version>`, with dots in the vendor replaced by underscores —
so `iglu:com.example/user_entity/jsonschema/2-0-1` *should* map to
`contexts_com_example_user_entity_2`. But casing and edge cases vary, and this is the
`<recovery_column>` placeholder used everywhere downstream, so **never confirm it from memory — have
the user run this and read the real name back**:

```sql
SELECT column_name, data_type
FROM <good_database>.INFORMATION_SCHEMA.COLUMNS
WHERE LOWER(table_schema) = LOWER('<good_schema>')
  AND LOWER(table_name)   = LOWER('<good_table>')
  AND LOWER(column_name) LIKE '%<schema_name_with_underscores>%'
ORDER BY column_name;
```

e.g. `LIKE '%user_entity%'`. The returned `column_name` is the exact `<recovery_column>`. If several
rows come back (multiple major versions), pick the one whose trailing `_<major>` matches the schema
version being recovered.

---

## Canonical atomic column set (reference only)

In Snowflake the good-vs-failed join classifies columns for you, so this list is informational — it
is the set of columns that are "atomic" (always present, pass through by name). Anything else is a
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

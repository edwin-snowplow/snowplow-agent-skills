# SQL reference — Databricks failed-events recovery

Load this file when you reach the SQL-generation phase. It holds the canonical column set, the
JSON/array-navigation rules, the strict-struct-typing rules, the metadata-driven INSERT generator,
and statement templates that must be applied exactly.

Like the Snowflake flow, there is **no local script**: the INSERT-generator query below reads both
tables' `INFORMATION_SCHEMA.COLUMNS` and builds the wide INSERT itself. Like the Athena/BigQuery
flow, Databricks struct typing is **strict**: the reconstructed entity's field names, types, and
order must match the target column's type. Use the templates here directly for every phase.

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
must actively look for unexpected NULLs and eyeball the values. The optional per-field null-count
query below makes this mechanical.

---

## The failure context column

Always named `contexts_com_snowplowanalytics_snowplow_failure_1`. In Databricks it is a **native**
`ARRAY<STRUCT<...>>`, and its `data` field is a **JSON-serialised STRING** (not a native JSON/VARIANT
type). Access it with native array/struct syntax, and read `data` with `GET_JSON_OBJECT`:

- **`[0]`** — arrays are **zero-based** (direct index). The failure context is normally a
  single-element array, so `contexts_..._failure_1[0]` is the failure struct. `element_at(col, 1)` is
  the 1-based equivalent; `try_element_at(col, 1)` returns NULL instead of erroring on an empty array.
- **`.field`** — navigate into a struct field, e.g. `...failure_1[0].data`, `...failure_1[0].schema`.
- **JSON extraction** — `data` is a STRING. Pull a scalar out with `GET_JSON_OBJECT(data, '$.field')`,
  which returns a **STRING**, then wrap in `CAST(... AS <type>)` for the target type:
  - `CAST(GET_JSON_OBJECT(...failure_1[0].data, '$.user_id') AS BIGINT)`
  - `CAST(GET_JSON_OBJECT(...failure_1[0].data, '$.user_name') AS STRING)`
  - `CAST(... AS DOUBLE)`, `CAST(... AS BOOLEAN)`, `CAST(... AS TIMESTAMP)` for other scalar types.
  - For a nested JSON object/array field, `GET_JSON_OBJECT(data, '$.obj')` returns the JSON substring;
    use `FROM_JSON(..., '<schema>')` if you need it as a typed struct/array.

  Remember the silent-NULL trap above: a wrong path or un-castable value can yield NULL, not an error.

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

To expand a multi-element failure array (or to read `errors` row-by-row), use
`LATERAL VIEW explode(contexts_com_snowplowanalytics_snowplow_failure_1) t AS f`, then read `f.data`,
`f.errors`, `f.schema`, etc. For reconstructing the entity, direct `[0]` indexing is simpler and is
what the recovery expression uses.

---

## Strict struct typing (the key Databricks constraint)

Entity columns are native `ARRAY<STRUCT<...>>`. When you `INSERT ... SELECT` into a Delta table, the
reconstructed struct must be **type-compatible** with the target column. Delta resolves struct fields
**by name**, so the field **names must match exactly**; keep the field **types** matching too, and
keep the **order** matching so the generated positional INSERT reads cleanly and reviews easily.

Get that contract from the recovery column's `full_data_type` in `INFORMATION_SCHEMA` (see "Finding
the recovery column name"), e.g.:

```
ARRAY<STRUCT<_schema_version: STRING, user_id: BIGINT, user_name: STRING>>
```

Build the recovery expression to match it field-for-field, in order. Never guess the shape — read it
from this `full_data_type` and/or a validated good-table row.

---

## Column classification (done by the generator query, not by hand)

For the INSERT, every column in the **good** events table is classified. In Databricks this is done
by the generator query joining `good` and `failed` `INFORMATION_SCHEMA.COLUMNS`, per column in
good-table `ordinal_position` order:

1. **The failure column** (`contexts_com_snowplowanalytics_snowplow_failure_1`) →
   `CAST(NULL AS <good full_data_type>)` — kept in position (positional INSERT), always null in
   recovered rows.
2. **`load_tstamp`** → `CURRENT_TIMESTAMP()` (fresh timestamp for reprocessed rows).
3. **The recovery column** (the entity being repaired) → the confirmed `ARRAY(NAMED_STRUCT(...))`
   expression.
4. **Column present in both good and failed tables** → pass through by name (covers all atomic
   columns and any custom column that holds values for events which failed on a *different* entity).
5. **Column only in the good table** → `CAST(NULL AS <target full_data_type>)`.

Because the failed events table already contains every atomic column, the good-vs-failed join does
the atomic-vs-custom classification for you — there is no separate atomic list to maintain. The
canonical atomic set is retained at the bottom of this file for reference only.

---

## Statement templates

### Time window (applies to every query below)

Bounding `load_tstamp` scans far less data (and prunes files on the typically `load_tstamp`-clustered
tables). Ask the user in Phase 1 **when the failures occurred** and prefer a **bounded range**:

```sql
load_tstamp >= TIMESTAMP('2026-03-01') AND load_tstamp < TIMESTAMP('2026-03-10')
```

If the timeframe is unknown, fall back to a **lookback** (`DATE_SUB` takes an integer number of days):

```sql
DATE(load_tstamp) > DATE_SUB(CURRENT_DATE(), <days>)
```

Use the **same** window in every query (identification, inspection, test, INSERT, duplicate
pre-check) so the identified, validated, and recovered sets match.

### Identification query — summary (Step 1, default)

Lead with this: one row per schema × app_id × failure type with volumes and time span. Best for
surveying what's failing without dumping every row, especially over a wide window.

```sql
SELECT
    contexts_com_snowplowanalytics_snowplow_failure_1[0].schema         AS failing_schema,
    app_id,
    contexts_com_snowplowanalytics_snowplow_failure_1[0].failure_type   AS failure_type,
    COUNT(*)                                                            AS failed_events,
    MIN(contexts_com_snowplowanalytics_snowplow_failure_1[0].timestamp) AS first_seen,
    MAX(contexts_com_snowplowanalytics_snowplow_failure_1[0].timestamp) AS last_seen
FROM <failed_catalog>.<failed_schema>.<failed_table>
WHERE DATE(load_tstamp) > DATE_SUB(CURRENT_DATE(), <days>)  -- or a bounded range (see Time window)
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
FROM <failed_catalog>.<failed_schema>.<failed_table>
WHERE DATE(load_tstamp) > DATE_SUB(CURRENT_DATE(), <days>)  -- or a bounded range
  AND contexts_com_snowplowanalytics_snowplow_failure_1[0].schema = '<failing_schema_uri>'
  AND app_id = '<app_id>'
LIMIT 10;
```

### Payload inspection query (Step 2)

Expands the failure context with `LATERAL VIEW explode` so the user sees the real value behind each
field. Read `data` scalars with `GET_JSON_OBJECT` (they render as strings — the real target types
come from the recovery column's `full_data_type`).

```sql
SELECT
    f.schema                              AS failing_schema,
    GET_JSON_OBJECT(f.data, '$.field1')   AS field1,
    GET_JSON_OBJECT(f.data, '$.field2')   AS field2
FROM <failed_catalog>.<failed_schema>.<failed_table>
     LATERAL VIEW explode(contexts_com_snowplowanalytics_snowplow_failure_1) t AS f
WHERE DATE(load_tstamp) > DATE_SUB(CURRENT_DATE(), <days>)
  AND f.schema = '<failing_schema_uri>'
  AND app_id = '<app_id>'
LIMIT 10;
```

### Inspect a validated event (ground the target shape in the schema)

The repaired entity's shape and field types are defined by the Iglu schema and materialised in the
recovery column's type — not chosen. Cross-check by pulling a recent **validated** event of the same
entity from the **good** table. `<recovery_column>` is the entity column found below.

```sql
SELECT
    event_id,
    <recovery_column>[0] AS validated_entity
FROM <good_catalog>.<good_schema>.<good_table>
WHERE size(<recovery_column>) > 0
LIMIT 10;
```

The authoritative contract is the recovery column's `full_data_type` (field names, types, order). If
the good table has **no** validated events for this entity yet (e.g. a brand-new schema) and the
`full_data_type` is ambiguous, fall back to reading the field types and `required` list from the
**Iglu schema JSON** — ask the user for it or its URL. Do not infer types from the failed payload
alone (those values are exactly what failed validation).

### Recovery expression pattern (Step 3) — build & confirm with the user

Reconstruct the repaired entity as an array of one struct. `_schema_version` is **mandatory** (first
field) for the Databricks Loader. Field names, types, and order must match the recovery column's
`full_data_type` exactly. Extract each field from the JSON `data` string with `GET_JSON_OBJECT`, then
`CAST` to the target type.

```sql
ARRAY(NAMED_STRUCT(
    '_schema_version', '1-0-0',  -- corrected version
    'user_id',   CAST(GET_JSON_OBJECT(contexts_com_snowplowanalytics_snowplow_failure_1[0].data, '$.user_id')   AS BIGINT),
    'user_name', CAST(GET_JSON_OBJECT(contexts_com_snowplowanalytics_snowplow_failure_1[0].data, '$.user_name') AS STRING)
))
```

`ARRAY(STRUCT(CAST(...) AS user_id, ...))` is an equivalent form (positional struct fields with
`AS name`), but `NAMED_STRUCT` makes the field names explicit and is preferred. Do **not** append
`AS <column_name>` here — the generator adds it. When embedding this into the generator query
(Phase 7), it is placed inside a **single-quoted** string literal, so **double every single quote**
inside the expression (`'1-0-0'` → `''1-0-0''`, `'$.user_id'` → `''$.user_id''`).

### Test SELECT (run and validate before the INSERT)

Emit this so the user can prove the reconstructed column is correct before any INSERT is generated.

```sql
WITH prep AS (
    SELECT *
    FROM <failed_catalog>.<failed_schema>.<failed_table>
    WHERE DATE(load_tstamp) > DATE_SUB(CURRENT_DATE(), <days>)
      AND app_id = '<app_id>'
      AND contexts_com_snowplowanalytics_snowplow_failure_1[0].schema = '<failing_schema_uri>'
)
SELECT
    event_id,
    <recovery expression> AS recovered
FROM prep
LIMIT 20;
```

**A clean run does not prove correctness in Databricks** (see "Silent NULLs" above). Inspect the
`recovered` values directly, and run the optional per-field null-count check below to catch a wrong
path or an un-castable value.

### Optional per-field null-count check (catches silent NULLs)

Because bad paths/casts can yield NULL rather than an error, count how many recovered rows are NULL
in each reconstructed field. **Expect 0** for every required field.

```sql
WITH prep AS (
    SELECT *
    FROM <failed_catalog>.<failed_schema>.<failed_table>
    WHERE DATE(load_tstamp) > DATE_SUB(CURRENT_DATE(), <days>)
      AND app_id = '<app_id>'
      AND contexts_com_snowplowanalytics_snowplow_failure_1[0].schema = '<failing_schema_uri>'
)
SELECT
    COUNT(*)                                                                                                   AS total_rows,
    SUM(CASE WHEN CAST(GET_JSON_OBJECT(contexts_com_snowplowanalytics_snowplow_failure_1[0].data, '$.user_id')   AS BIGINT) IS NULL THEN 1 ELSE 0 END) AS null_user_id,
    SUM(CASE WHEN CAST(GET_JSON_OBJECT(contexts_com_snowplowanalytics_snowplow_failure_1[0].data, '$.user_name') AS STRING) IS NULL THEN 1 ELSE 0 END) AS null_user_name
FROM prep;
```

A non-zero count on a required field means a wrong JSON path or a genuinely bad value — fix before
proceeding. (A nullable/optional field may legitimately be NULL; judge against the schema.)

### Validation checklist (the gate)

Confirm all of these against the pasted test output before generating the INSERT:

1. **No unexpected NULLs (decisive here).** Databricks returns NULL silently on a wrong
   `GET_JSON_OBJECT` path and (in legacy mode) on a bad `CAST` — so unexpected nulls, not query
   errors, are the primary failure signal. Use the per-field null-count check; every required field
   should be 0. **Do not** rely on "the query ran" as proof.
2. **Values are corrected** — the repaired field holds the fixed value (e.g. a real integer, not
   NULL), and pass-through fields are intact.
3. **Struct shape matches the target** — the reconstructed struct's field names, types, and order
   match the recovery column's `full_data_type` exactly (Delta resolves struct fields by name for the
   INSERT), and `_schema_version` is present as the first field.
4. **Row count** equals the failed-event count for this schema + app_id (from the identification query).

If any check fails, fix the recovery expression and re-run the test SELECT. Only generate the INSERT
once the values are correct (no unexpected nulls), the shape matches, and the row count is right.

---

## INSERT-generator query (Phase 7 — Databricks builds the INSERT itself)

This joins the good and failed tables' `INFORMATION_SCHEMA.COLUMNS`, classifies each good-table
column, and emits the **full INSERT statement as a single text cell**. The user runs it, copies the
returned statement, and runs that.

Fill in every `<placeholder>`. The recovery expression is embedded inside a **single-quoted** string
literal, so **every single quote inside it must be doubled** (`''`). `array_sort` orders the emitted
expressions by `ordinal_position` (its first struct field) so the INSERT is positional and lines up
with the good table — `collect_list` alone does not guarantee order.

```sql
WITH good_cols AS (
    SELECT ordinal_position, column_name, full_data_type
    FROM <good_catalog>.information_schema.columns
    WHERE table_schema = '<good_schema>' AND table_name = '<good_table>'
),
failed_cols AS (
    SELECT column_name
    FROM <failed_catalog>.information_schema.columns
    WHERE table_schema = '<failed_schema>' AND table_name = '<failed_table>'
),
mapping AS (
    SELECT
        g.ordinal_position,
        CASE
            WHEN lower(g.column_name) = 'contexts_com_snowplowanalytics_snowplow_failure_1'
                THEN concat('CAST(NULL AS ', g.full_data_type, ') AS ', g.column_name)
            WHEN lower(g.column_name) = 'load_tstamp'
                THEN 'CURRENT_TIMESTAMP() AS load_tstamp'
            WHEN lower(g.column_name) = lower('<recovery_column>')
                THEN '<recovery_expression> AS <recovery_column>'
            WHEN f.column_name IS NOT NULL
                THEN g.column_name
            ELSE concat('CAST(NULL AS ', g.full_data_type, ') AS ', g.column_name)
        END AS select_expression
    FROM good_cols g
    LEFT JOIN failed_cols f ON lower(g.column_name) = lower(f.column_name)
),
select_clause AS (
    SELECT array_join(
        transform(
            array_sort(collect_list(named_struct('pos', ordinal_position, 'expr', select_expression))),
            x -> x.expr
        ),
        ',\n    '
    ) AS columns_sql
    FROM mapping
)
SELECT concat(
    'INSERT INTO <target_catalog>.<target_schema>.<target_table>\n',
    'WITH prep AS (\n',
    '  SELECT *\n',
    '  FROM <failed_catalog>.<failed_schema>.<failed_table>\n',
    '  WHERE DATE(load_tstamp) > DATE_SUB(CURRENT_DATE(), <days>)\n',
    '    AND app_id = ''<app_id>''\n',
    '    AND contexts_com_snowplowanalytics_snowplow_failure_1[0].schema = ''<failing_schema_uri>''\n',
    ')\n',
    'SELECT\n    ',
    columns_sql,
    '\nFROM prep;'
) AS generated_sql
FROM select_clause;
```

Notes:
- `<target_*>` is the **staging** table (staging path) or the **good** table (direct path). Column
  classification always comes from the **good** table, so a staging table created with `SELECT * … LIMIT 0`
  lines up positionally.
- The generated INSERT is **positional** (no explicit column list): the SELECT expressions are
  emitted in good-table `ordinal_position` order, so they land in the right columns. This is why the
  recovery struct's field order should match the target type.
- `<recovery_expression>` is the validated Phase-4 expression with **every single quote doubled**,
  inserted verbatim inside the single-quoted CASE branch; `<recovery_column>` is the entity column name.
- Before the user runs the generated INSERT, review it: recovery column carries the expression,
  failure column nulled, `load_tstamp` refreshed, and the column count matches the good table. If the
  recovery column wasn't matched (its expression is missing), check `<recovery_column>` against the
  good table's actual column name and regenerate.
- **If `CAST(NULL AS <full_data_type>)` for the failure column is rejected** (its full nested type is
  unusual), fall back to a simplified null of a compatible shape — e.g.
  `CAST(NULL AS ARRAY<STRUCT<schema:STRING, data:STRING>>)` — or give the INSERT an explicit column
  list that omits the failure column entirely.

---

## Staging-table patterns (staging write path)

### 1. Create an empty staging table that mirrors the good table

CTAS with `LIMIT 0` copies the exact column set, types, and order with zero rows:

```sql
CREATE TABLE <staging_catalog>.<staging_schema>.<staging_table> AS
SELECT * FROM <good_catalog>.<good_schema>.<good_table> LIMIT 0;
```

(`CREATE TABLE <staging> LIKE <good>` is an equivalent alternative on Databricks.)

### 2. Recovery INSERT into staging

Same generated INSERT, but with `<target_*>` set to the staging table (column classification still
comes from the good table).

### 3. Validate the staging table

```sql
SELECT COUNT(*) AS rows_in_staging FROM <staging_catalog>.<staging_schema>.<staging_table>;

SELECT event_id, <recovery_column>[0]
FROM <staging_catalog>.<staging_schema>.<staging_table>
LIMIT 10;
```

### 4. Promote staging → good table

Column order matches (staging was created with `SELECT * … LIMIT 0`), so `SELECT *` is safe:

```sql
INSERT INTO <good_catalog>.<good_schema>.<good_table>
SELECT * FROM <staging_catalog>.<staging_schema>.<staging_table>;
```

### 5. (Optional) drop the staging table afterwards

```sql
DROP TABLE <staging_catalog>.<staging_schema>.<staging_table>;
```

---

## Duplicate pre-check (run before the INSERT)

Confirms the events haven't already been recovered. The events being recovered only exist because
they failed validation, so any of their `event_id`s already present in the good table means a prior
recovery — running again would duplicate them. **Expect 0.**

```sql
SELECT COUNT(*) AS already_in_good
FROM <good_catalog>.<good_schema>.<good_table>
WHERE event_id IN (
    SELECT event_id
    FROM <failed_catalog>.<failed_schema>.<failed_table>
    WHERE DATE(load_tstamp) > DATE_SUB(CURRENT_DATE(), <days>)
      AND app_id = '<app_id>'
      AND contexts_com_snowplowanalytics_snowplow_failure_1[0].schema = '<failing_schema_uri>'
);
```

If it returns > 0, list the overlapping `event_id`s and warn before proceeding:

```sql
SELECT g.event_id
FROM <good_catalog>.<good_schema>.<good_table> g
WHERE g.event_id IN (
    SELECT event_id
    FROM <failed_catalog>.<failed_schema>.<failed_table>
    WHERE DATE(load_tstamp) > DATE_SUB(CURRENT_DATE(), <days>)
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
the user run this and read the real name and `full_data_type` back**:

```sql
SELECT column_name, full_data_type
FROM <good_catalog>.information_schema.columns
WHERE table_schema = '<good_schema>' AND table_name = '<good_table>'
  AND lower(column_name) LIKE '%<schema_name_with_underscores>%'
ORDER BY column_name;
```

e.g. `LIKE '%user_entity%'`. The returned `column_name` is the exact `<recovery_column>`, and its
`full_data_type` (e.g. `ARRAY<STRUCT<_schema_version: STRING, user_id: BIGINT, user_name: STRING>>`)
is the struct contract the recovery expression must match. If several rows come back (multiple major
versions), pick the one whose trailing `_<major>` matches the schema version being recovered.

---

## Canonical atomic column set (reference only)

In Databricks the good-vs-failed join classifies columns for you, so this list is informational — it
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

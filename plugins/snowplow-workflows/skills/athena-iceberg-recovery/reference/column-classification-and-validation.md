# Reference — column classification, type conversion & the validation gate

**The decision this doc helps you make:** whether the reconstructed entity is shaped correctly enough
that the wide `INSERT ... SELECT` will type-check and land clean data — and what the generator does
with every other column. This is the judgment layer behind `scripts/generate_insert.py` and the INSERT
template in `templates/recovery-sql.md`.

## Strict struct typing (the key Athena constraint)

Entity columns are native `ARRAY(ROW(...))`. Athena inserts **positionally** into a struct, so the
reconstructed entity's `ROW(...)` must have the **same field names, the same field types, and the same
field order** as the good-table column's type. Get that contract from the good-table `DESCRIBE` (see
`reference/failure-context.md`), and build the recovery expression as `CAST(ARRAY[ROW(...)] AS
ARRAY(ROW(...)))` to match it field-for-field, `_schema_version` first. The `ARRAY<STRUCT<...>>` form
that DESCRIBE prints is DDL-only and invalid in a query.

**Why order matters:** the generated INSERT lists columns explicitly but the *struct fields* inside the
recovery column are matched positionally, so a reordered field is a type mismatch even if the names are
right.

## Column classification (what the generator does, per column)

For every column in the **good** events table DESCRIBE, decide its SELECT expression:

1. **The failure column** (`contexts_com_snowplowanalytics_snowplow_failure_1`) → **exclude entirely**
   from both the column list and the SELECT. *Why:* re-carrying the failure context into the good
   table would contaminate it.
2. **`load_tstamp`** → `current_timestamp AS load_tstamp` (fresh timestamp for reprocessed rows).
3. **The recovery column** (the entity being repaired) → the user's confirmed `CAST(ARRAY[ROW(...)] AS
   ARRAY(ROW(...)))` expression, followed by `AS <column_name>`. Strip any trailing `AS <column_name>`
   the user already included before re-adding it.
4. **Atomic column** (present in the canonical atomic set below) → pass through by name.
5. **Custom column also present in the failed table DESCRIBE** → pass through by name (it can hold
   values for events that failed on a *different* entity).
6. **Custom column only in the good table** → `CAST(NULL AS <athena_type>) AS <column_name>`.

After classifying, report a sanity summary to the user:
`good columns: N | failed columns: M | custom pass-through: P | custom nulled: Q`.
Warn if the recovery column name is not found in the good DESCRIBE.

Because the failed events table already contains every atomic column, the good-vs-failed comparison
does the atomic-vs-custom classification for you; the canonical atomic set below is retained for
reference and for the fallback when building the INSERT inline.

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

If a struct **field name** equals a reserved word, it must be double-quoted or Athena rejects the
type, e.g. `row("timestamp" varchar, ...)`. Quote a field name when it (case-insensitive) is one of:

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

## Parsing DESCRIBE output

- Lines may be **tab-separated** or **space-separated** depending on how the user copied them. For
  space-separated, split only on the *first* run of whitespace (`col_name` then the rest is the type)
  so complex types containing spaces stay intact.
- Skip blank lines and `#` comment lines.
- **Stop parsing** at a line starting with `# Partition spec` or `# Partition information` —
  everything after is the partition spec, not columns.

## Choosing the extractor per field (from profiling — Phase 3.5)

Run the profiling query (`templates/recovery-sql.md`) to learn, per field across *all* failed rows,
whether every value casts to the target type and whether the field is always present, then decide in
order:

1. **Any field `_uncoercible > 0`** → the failed rows don't share one recovery; some values can't
   become the target type. **Stop, surface the offending rows, and partition/scope** with the user.
   Don't suggest a recovery expression yet.
2. **Any field `_absent > 0`** → it's missing from some rows: if the schema requires the field,
   partition those rows off; if optional, the extractor returns NULL for them (fine).
3. **Otherwise** the population is uniform → build the expression with
   `CAST(json_extract_scalar(...) AS <type>)`, which coerces native and string-encoded values alike and
   **errors** (never silent-NULLs) on a bad value. Because profiling already ruled out un-castable
   values, the built expression is guaranteed to run clean. **Never use `TRY_CAST` in the recovery
   expression** — it hides un-recovered values as NULL (it belongs only in the profiling query).

## The validation gate (Phase 5 — do not skip)

Confirm all of these against the pasted test-SELECT output before generating the INSERT:

1. **Type match (decisive).** `recovered_type` (from `typeof(...)`) must equal the good-table column's
   type after Hive→Athena conversion — same field **names, types, and order**. Athena inserts
   positionally into a struct, so order matters. If `typeof` matches the target column type, the INSERT
   will type-check.
2. **Row count** equals the failed-event count for this schema + app_id (from the identification
   query).
3. **No unexpected nulls** in `recovered` — a null usually means a wrong `$.json_path` or a cast that
   silently returned null. Investigate before proceeding.
4. **Values are corrected** — the repaired field holds the fixed value (e.g. a proper integer, not a
   quoted string), and pass-through fields are intact.

If any check fails, fix the recovery expression and re-run the test SELECT (return to Phase 4). Only
generate the INSERT once the type matches and the values are correct. When a check fails and the cause
isn't obvious, see `troubleshooting/recovery-issues.md`.

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

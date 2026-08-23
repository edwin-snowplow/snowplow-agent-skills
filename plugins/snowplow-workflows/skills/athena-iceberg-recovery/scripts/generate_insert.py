#!/usr/bin/env python3
"""
Generate the recovery INSERT statement for the athena-iceberg-recovery skill.

Non-interactive. Reads its inputs from files and prints the INSERT to stdout
(or writes it with --out). Standard library only — runs on any local Python 3.6+.

Usage:
    python3 generate_insert.py \
        --config config.json \
        --good-describe good_describe.txt \
        --failed-describe failed_describe.txt \
        --recovery-expr recovery_expression.sql \
        [--out recovery.sql]

config.json keys:
    GOOD_DATABASE, GOOD_TABLE, FAILED_DATABASE, FAILED_TABLE,
    RECOVERY_COLUMN, FAILED_SCHEMA, APP_ID, DAYS_LOOKBACK
    FAILURE_SOURCE  (optional) — 'contexts' or 'unstruct'. When set, scopes prep to
        rows whose failure source matches (errors[1].source), so a schema that failed
        both ways recovers one source per run into its own column. Omit for a
        single-source schema. RECOVERY_COLUMN is the contexts_* column for a contexts
        run or the unstruct_event_* column for an unstruct run.
    TARGET_DATABASE, TARGET_TABLE  (optional) — where the INSERT writes. Defaults to
        GOOD_DATABASE/GOOD_TABLE. Set these to a staging table when using the staging path;
        column classification still comes from the good-table DESCRIBE, so the staging table
        must mirror the good table's schema.

The three text files are raw pastes:
    good-describe / failed-describe : the DESCRIBE output for each table
    recovery-expr                   : the CAST(ARRAY[ROW(...)] AS ARRAY(ROW(...)))
                                      expression, WITHOUT a trailing "AS <column>"

Prints a sanity summary (column counts, pass-through, nulled) to stderr.
"""

import argparse
import json
import re
import sys

FAILURE_COL = "contexts_com_snowplowanalytics_snowplow_failure_1"

ATOMIC_COLUMNS = set("""
app_id platform etl_tstamp collector_tstamp dvce_created_tstamp event event_id txn_id
name_tracker v_tracker v_collector v_etl user_id user_ipaddress user_fingerprint
domain_userid domain_sessionidx network_userid geo_country geo_region geo_city geo_zipcode
geo_latitude geo_longitude geo_region_name ip_isp ip_organization ip_domain ip_netspeed
page_url page_title page_referrer page_urlscheme page_urlhost page_urlport page_urlpath
page_urlquery page_urlfragment refr_urlscheme refr_urlhost refr_urlport refr_urlpath
refr_urlquery refr_urlfragment refr_medium refr_source refr_term mkt_medium mkt_source
mkt_term mkt_content mkt_campaign se_category se_action se_label se_property se_value
tr_orderid tr_affiliation tr_total tr_tax tr_shipping tr_city tr_state tr_country ti_orderid
ti_sku ti_name ti_category ti_price ti_quantity pp_xoffset_min pp_xoffset_max pp_yoffset_min
pp_yoffset_max useragent br_name br_family br_version br_type br_renderengine br_lang
br_features_pdf br_features_flash br_features_java br_features_director br_features_quicktime
br_features_realplayer br_features_windowsmedia br_features_gears br_features_silverlight
br_cookies br_colordepth br_viewwidth br_viewheight os_name os_family os_manufacturer
os_timezone dvce_type dvce_ismobile dvce_screenwidth dvce_screenheight doc_charset doc_width
doc_height tr_currency tr_total_base tr_tax_base tr_shipping_base ti_currency ti_price_base
base_currency geo_timezone mkt_clickid mkt_network etl_tags dvce_sent_tstamp refr_domain_userid
refr_dvce_tstamp domain_sessionid derived_tstamp event_vendor event_name event_format
event_version event_fingerprint true_tstamp load_tstamp
""".split())

RESERVED = set("""
current_time current_date current_timestamp timestamp date time interval year month day hour
minute second zone value values table column row type position filter over partition range
select from where group order by having limit union all distinct as in not and or is null
true false case when then else end exists between like natural cross join inner outer left
right full on using
""".split())


def strip_partition_spec(text):
    lines = []
    for line in text.splitlines():
        low = line.strip().lower()
        if low.startswith("# partition spec") or low.startswith("# partition information"):
            break
        lines.append(line)
    return "\n".join(lines)


def parse_describe(output):
    columns = []
    for line in strip_partition_spec(output).splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        parts = s.split("\t") if "\t" in s else s.split(None, 1)
        if len(parts) < 2:
            continue
        name, dtype = parts[0].strip(), parts[1].strip()
        if name and dtype:
            columns.append((name, dtype))
    return columns


def hive_to_athena_type(hive_type):
    t = re.sub(r'array<', 'array(', hive_type)
    t = re.sub(r'struct<', 'row(', t)
    t = t.replace('>', ')')
    t = re.sub(r'(\w+):', r'\1 ', t)

    def quote_reserved(m):
        word = m.group(1)
        return f'"{word}"' if word.lower() in RESERVED else word

    t = re.sub(
        r'(?<!")\b([a-z_][a-z0-9_]*)(?=\s+(?:varchar|string|bigint|integer|int|double|boolean|decimal|timestamp|array|row|struct))',
        quote_reserved, t,
    )
    t = re.sub(r'\bstring\b', 'varchar', t)
    t = re.sub(r'\bint\b', 'integer', t)
    t = re.sub(r'decimal\((\d+),\s*(\d+)\)', r'decimal(\1,\2)', t)
    t = re.sub(r'  +', ' ', t)
    return t


def time_filter(cfg):
    """Bounded load_tstamp range if LOAD_TSTAMP_START is set (prunes partitions), else lookback."""
    start = cfg.get("LOAD_TSTAMP_START")
    end = cfg.get("LOAD_TSTAMP_END")
    if start:
        clause = f"load_tstamp >= TIMESTAMP '{start.replace(chr(39), chr(39) * 2)}'"
        if end:
            clause += f"\n      AND load_tstamp < TIMESTAMP '{end.replace(chr(39), chr(39) * 2)}'"
        return clause
    return f"load_tstamp > current_timestamp - interval '{cfg['DAYS_LOOKBACK']}' day"


def make_insert(good_columns, failed_col_names, cfg, recovery_expr):
    insert_cols, select_exprs = [], []
    recovery_col = cfg["RECOVERY_COLUMN"]
    passthrough = nulled = 0

    for name, dtype in good_columns:
        if name == FAILURE_COL:
            continue
        insert_cols.append(name)
        if name == "load_tstamp":
            select_exprs.append("-- fresh timestamp for reprocessed rows\n    current_timestamp AS load_tstamp")
        elif name == recovery_col:
            expr = re.sub(r'\bAS\s+' + re.escape(recovery_col) + r'\s*$', '', recovery_expr,
                          flags=re.IGNORECASE).strip()
            select_exprs.append(f"{expr}\n    AS {name}")
        elif name in ATOMIC_COLUMNS:
            select_exprs.append(name)
        elif name in failed_col_names:
            select_exprs.append(name)
            passthrough += 1
        else:
            select_exprs.append(f"CAST(NULL AS {hive_to_athena_type(dtype)}) AS {name}")
            nulled += 1

    app_id_safe = cfg["APP_ID"].replace("'", "''")
    schema_safe = cfg["FAILED_SCHEMA"].replace("'", "''")
    target_db = cfg.get("TARGET_DATABASE") or cfg["GOOD_DATABASE"]
    target_tbl = cfg.get("TARGET_TABLE") or cfg["GOOD_TABLE"]

    # Optional source scoping (contexts vs unstruct). A schema can fail both as a
    # context entity and as the self-describing event; each is a separate run into
    # its own column. errors is array(varchar) of JSON strings (1-based), so read
    # source with json_extract_scalar, not a struct field access.
    source_predicate = ""
    if cfg.get("FAILURE_SOURCE"):
        source_safe = cfg["FAILURE_SOURCE"].replace("'", "''")
        source_predicate = (
            "      AND json_extract_scalar(contexts_com_snowplowanalytics_snowplow_failure_1[1].errors[1], '$.source')"
            f"\n          = '{source_safe}'\n"
        )

    sql = (
        f"INSERT INTO {target_db}.{target_tbl} (\n    "
        + ",\n    ".join(insert_cols)
        + "\n)\nWITH prep AS (\n    SELECT *\n    FROM "
        + f"{cfg['FAILED_DATABASE']}.{cfg['FAILED_TABLE']}\n"
        + f"    WHERE {time_filter(cfg)}\n"
        + f"      AND app_id = '{app_id_safe}'\n"
        + f"      AND contexts_com_snowplowanalytics_snowplow_failure_1[1].schema\n          = '{schema_safe}'\n"
        + source_predicate
        + "),\n"
        + "unnested AS (\n    SELECT\n        prep.*,\n        failure_ctx.data AS failure_data\n    FROM prep\n"
        + "    CROSS JOIN UNNEST(contexts_com_snowplowanalytics_snowplow_failure_1) AS t(failure_ctx)\n)\n"
        + "SELECT\n    " + ",\n    ".join(select_exprs) + "\nFROM unnested;"
    )
    return sql, insert_cols, passthrough, nulled


def read_file(path):
    with open(path) as f:
        return f.read()


def read_expr_file(path):
    lines = read_file(path).splitlines()
    return "\n".join(l.rstrip() for l in lines if not l.strip().startswith("#")).strip()


def main():
    ap = argparse.ArgumentParser(description="Generate the recovery INSERT statement.")
    ap.add_argument("--config", required=True, help="config.json path")
    ap.add_argument("--good-describe", required=True)
    ap.add_argument("--failed-describe", required=True)
    ap.add_argument("--recovery-expr", required=True)
    ap.add_argument("--out", help="write SQL here; otherwise print to stdout")
    args = ap.parse_args()

    try:
        cfg = json.loads(read_file(args.config))
    except json.JSONDecodeError as e:
        sys.exit(f"ERROR: {args.config} is not valid JSON ({e}).")

    required = ["GOOD_DATABASE", "GOOD_TABLE", "FAILED_DATABASE", "FAILED_TABLE",
                "RECOVERY_COLUMN", "FAILED_SCHEMA", "APP_ID"]
    missing = [k for k in required if not cfg.get(k)]
    if missing:
        sys.exit(f"ERROR: config missing keys: {', '.join(missing)}")
    # Time window: need either a bounded start or a lookback.
    if not cfg.get("LOAD_TSTAMP_START") and not cfg.get("DAYS_LOOKBACK"):
        sys.exit("ERROR: config needs either LOAD_TSTAMP_START (bounded range) or DAYS_LOOKBACK (lookback).")

    good_columns = parse_describe(read_file(args.good_describe))
    failed_col_names = {c for c, _ in parse_describe(read_file(args.failed_describe))}
    recovery_expr = read_expr_file(args.recovery_expr)

    if not good_columns:
        sys.exit("ERROR: no columns parsed from good-describe. Check the file.")
    if not recovery_expr:
        sys.exit("ERROR: recovery-expr is empty.")

    rec = cfg["RECOVERY_COLUMN"]
    sql, insert_cols, passthrough, nulled = make_insert(good_columns, failed_col_names, cfg, recovery_expr)

    if args.out:
        with open(args.out, "w") as f:
            f.write(sql)

    # Sanity summary -> stderr so stdout stays pure SQL when piped.
    print(f"good columns:   {len(good_columns)}", file=sys.stderr)
    print(f"failed columns: {len(failed_col_names)}", file=sys.stderr)
    print(f"insert columns: {len(insert_cols)}  (failure col excluded)", file=sys.stderr)
    print(f"pass-through:   {passthrough}", file=sys.stderr)
    print(f"nulled:         {nulled}", file=sys.stderr)
    if not any(c == rec for c, _ in good_columns):
        print(f"WARNING: recovery column '{rec}' not found in good DESCRIBE.", file=sys.stderr)
    if args.out:
        print(f"written to {args.out}", file=sys.stderr)

    if not args.out:
        print(sql)


if __name__ == "__main__":
    main()

"""Convert destination-local ERP delivery clocks to the same clock as ERP dispatch."""
from pathlib import Path

import pandas as pd

from . import core


def wms_clock_basis(alias):
    warehouse = f"upper(trim({alias}.仓库))"
    carrier = f"lower(trim({alias}.C端承运商))"
    return f"""CASE
        WHEN {warehouse} IN ('JY01','JY02','JY03','XC01','XCWH01','WH01','ZBJ','MGZYTH01') THEN '北京时间'
        WHEN contains({warehouse},'WINIT') AND {carrier} IN
            ('yanwen','speedx','veho','fedex','fedex smartpost','ontrac','dhl ecommerce','dhl ecommerce us') THEN '北京时间'
        WHEN contains({warehouse},'WINIT') THEN '目的地当地时间'
    END"""


# Countries with multiple offsets require a region or postal match.
COUNTRY_ZONES = {
    "united kingdom": "Europe/London", "ireland": "Europe/Dublin",
    "france": "Europe/Paris", "germany": "Europe/Berlin", "netherlands": "Europe/Amsterdam",
    "belgium": "Europe/Brussels", "sweden": "Europe/Stockholm", "italy": "Europe/Rome",
    "switzerland": "Europe/Zurich", "norway": "Europe/Oslo", "denmark": "Europe/Copenhagen",
    "austria": "Europe/Vienna", "south africa": "Africa/Johannesburg", "nigeria": "Africa/Lagos",
    "zambia": "Africa/Lusaka", "china": "Asia/Shanghai", "japan": "Asia/Tokyo",
    "united arab emirates": "Asia/Dubai", "saudi arabia": "Asia/Riyadh",
    "puerto rico": "America/Puerto_Rico", "virgin islands, u.s.": "America/St_Thomas",
    "poland": "Europe/Warsaw", "luxembourg": "Europe/Luxembourg",
    "south korea": "Asia/Seoul", "thailand": "Asia/Bangkok", "ghana": "Africa/Accra",
    "botswana": "Africa/Gaborone", "zimbabwe": "Africa/Harare",
}

REGION_ZONES = {
    "australia": {"victoria": "Australia/Melbourne", "tasmania": "Australia/Hobart",
                  "australian capital territory": "Australia/Sydney", "northern territory": "Australia/Darwin",
                  "south australia": "Australia/Adelaide", "queensland": "Australia/Brisbane",
                  "western australia": "Australia/Perth"},
    "canada": {"nova scotia": "America/Halifax", "new brunswick": "America/Moncton",
               "prince edward island": "America/Halifax", "yukon": "America/Whitehorse"},
    "united states": {"virgin islands": "America/St_Thomas", "puerto rico": "America/Puerto_Rico"},
}


def build_delivery_clock(con, *, temporary=False):
    """Keep raw local times and compute Beijing times for unambiguous deliveries."""
    postal_file = Path(__file__).resolve().parents[1] / "reference" / "us-postal-timezones.csv"
    con.register("postal_zones", pd.read_csv(postal_file, dtype=str))
    con.register("destination_zones", pd.DataFrame(
        [(country, "", zone) for country, zone in COUNTRY_ZONES.items()] +
        [(country, region, zone) for country, regions in REGION_ZONES.items()
         for region, zone in regions.items()], columns=["country", "region", "zone"],
    ))
    columns = core.columns(con, "履约单明细")
    region = 'lower(max(nullif(trim("省、州"),\'\')))' if "省、州" in columns else "NULL::VARCHAR"
    postal = "upper(max(nullif(trim(邮编),'')))" if "邮编" in columns else "NULL::VARCHAR"
    zip5 = "max(nullif(regexp_extract(trim(邮编),'^([0-9]{5})(?:-[0-9]{4})?$',1),''))" \
        if "邮编" in columns else "NULL::VARCHAR"
    # Older imports discarded some deliveries based on a comparison across time zones.
    recovered = """max(try_cast(签收时间 AS TIMESTAMP)) FILTER (WHERE 时间异常='签收早于签出')""" \
        if {"签收时间", "时间异常"} <= columns else "NULL::TIMESTAMP"
    table = "TEMP TABLE" if temporary else "TABLE"
    try:
        con.execute(f"""
            CREATE OR REPLACE {table} fct_delivery_clock AS
            WITH deliveries AS (
                SELECT 履约单号, min(签出时间_ts) AS shipped,
                       max(签收时间_ts) AS original, {recovered} AS recovered,
                       lower(max(nullif(trim(目的国家英文名称),''))) AS country,
                       {region} AS region, {postal} AS postal, {zip5} AS zip5
                FROM 履约单明细 GROUP BY 1
                HAVING original IS NOT NULL OR recovered IS NOT NULL
            ), local_clock AS (
                SELECT d.*, coalesce(d.original,d.recovered) AS signed_local,
                       coalesce(p.iana_zone,r.zone,c.zone,CASE
                           WHEN d.country='canada' AND d.region='ontario'
                                AND regexp_matches(d.postal,'^[KLMN][0-9][A-Z]') THEN 'America/Toronto'
                           WHEN d.country='canada' AND d.region='quebec'
                                AND regexp_matches(d.postal,'^[HJ][0-9][A-Z]') THEN 'America/Toronto'
                           WHEN d.country='spain' AND regexp_matches(d.postal,'^[0-9]{5}$')
                                THEN CASE WHEN left(d.postal,2) IN ('35','38') THEN 'Atlantic/Canary' ELSE 'Europe/Madrid' END
                           WHEN d.country='australia' AND d.region='new south wales'
                                AND regexp_matches(d.postal,'^[12][0-9]{3}$')
                                AND d.postal NOT IN ('2880','2898') THEN 'Australia/Sydney'
                       END) AS zone
                FROM deliveries d
                LEFT JOIN postal_zones p ON d.country='united states' AND p.postal_code=d.zip5
                LEFT JOIN destination_zones r ON r.country=d.country AND r.region=d.region
                LEFT JOIN destination_zones c ON c.country=d.country AND c.region=''
            ), absolute_clock AS (
                SELECT *, timezone(zone,signed_local) AS signed_instant FROM local_clock
            ), resolved AS (
                SELECT *, CASE
                    WHEN timezone(zone,signed_instant)<>signed_local THEN NULL
                    WHEN timezone(zone,signed_instant-INTERVAL '1 hour')=signed_local THEN NULL
                    WHEN timezone(zone,signed_instant+INTERVAL '1 hour')=signed_local THEN NULL
                    ELSE timezone('Asia/Shanghai',signed_instant) END AS beijing
                FROM absolute_clock
            )
            SELECT 履约单号, signed_local, zone,
                   CASE WHEN shipped IS NULL OR beijing>=shipped THEN beijing END AS signed_beijing
            FROM resolved WHERE original IS NOT NULL OR beijing>=shipped
        """)
    finally:
        con.unregister("postal_zones")
        con.unregister("destination_zones")


def delivery_facts(con, facts="fct_order"):
    if "签收北京时间" in core.columns(con, "fct_order"):
        return facts
    if not core.columns(con, "fct_delivery_clock"):
        build_delivery_clock(con, temporary=True)
    return f"""(SELECT o.* REPLACE (
                coalesce(d.signed_local,o.签收时间) AS 签收时间,
                CASE WHEN d.signed_local IS NOT NULL THEN 1 ELSE o.已签收 END AS 已签收,
                date_diff('minute',o.签出时间,d.signed_beijing) AS 物流分钟),
                d.signed_beijing AS 签收北京时间, d.zone AS 签收时区
                FROM {facts} o LEFT JOIN fct_delivery_clock d USING (履约单号))"""

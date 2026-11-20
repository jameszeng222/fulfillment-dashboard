"""Normalize the main tracking number from ERP fields with quantity suffixes."""
import re


_QUANTITY_SUFFIX = r'\s*\+\s*[0-9]{1,3}\s*$'


def strip_quantity(value):
    return re.sub(_QUANTITY_SUFFIX, '', str(value or ''))


def normalized(value):
    return re.sub(r'[^A-Za-z0-9]', '', strip_quantity(value)).upper()


def number_text_sql(expression):
    return f"regexp_replace(coalesce({expression},''),'{_QUANTITY_SUFFIX}','')"


def key_sql(expression):
    return f"upper(regexp_replace({number_text_sql(expression)},'[^A-Za-z0-9]','','g'))"


def shipped_waybill_sql():
    """When ERP lines disagree, select the waybill on the latest dispatched line."""
    return """arg_max(nullif(trim(快递单号),''),
                      struct_pack(dispatched := 签出时间_ts IS NOT NULL,
                                  event_at := coalesce(签出时间_ts,创建时间_ts),
                                  number := trim(快递单号)))"""

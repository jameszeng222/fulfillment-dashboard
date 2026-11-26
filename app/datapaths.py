"""Resolve runtime database paths without reading or moving business data."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def configured_path(name):
    value = os.environ.get(name, "").strip()
    return Path(value).expanduser().resolve() if value else None


def data_directory():
    return configured_path("OPS_DATA_DIR") or ROOT


def fact_database():
    return configured_path("OPS_FACT_DB") or data_directory() / "履约单.duckdb"


def operations_database():
    return configured_path("OPS_OPS_DB") or data_directory() / "ops.db"

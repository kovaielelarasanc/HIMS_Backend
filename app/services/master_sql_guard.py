# app/services/master_sql_guard.py
import re
from typing import Optional

IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
DB_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")

TYPE_RE = re.compile(
    r"^(?:"
    r"TINYINT|SMALLINT|INT|BIGINT|"
    r"TINYINT\(1\)|"
    r"FLOAT|DOUBLE|"
    r"DECIMAL\(\d{1,2},\d{1,2}\)|"
    r"DATE|TIME|DATETIME|TIMESTAMP|"
    r"CHAR\(\d{1,3}\)|VARCHAR\(\d{1,4}\)|"
    r"TEXT|MEDIUMTEXT|LONGTEXT|"
    r"JSON"
    r")$",
    re.IGNORECASE
)

TYPE_ALLOWLIST = [
    "TINYINT", "TINYINT(1)", "SMALLINT", "INT", "BIGINT",
    "FLOAT", "DOUBLE",
    "DECIMAL(10,2)", "DECIMAL(12,2)", "DECIMAL(18,2)",
    "DATE", "TIME", "DATETIME", "TIMESTAMP",
    "CHAR(36)", "VARCHAR(64)", "VARCHAR(100)", "VARCHAR(190)", "VARCHAR(255)",
    "TEXT", "MEDIUMTEXT", "LONGTEXT",
    "JSON",
]

def assert_ident(v: str, what: str = "identifier") -> str:
    if not v or not IDENT_RE.match(v):
        raise ValueError(f"Invalid {what}: {v!r}")
    return v

def assert_db(v: str) -> str:
    if not v or not DB_RE.match(v):
        raise ValueError(f"Invalid db_name: {v!r}")
    return v

def assert_type(v: str) -> str:
    t = (v or "").strip().upper()
    if t == "BOOLEAN":
        t = "TINYINT(1)"
    if not TYPE_RE.match(t):
        raise ValueError(f"Unsupported/unsafe type: {v!r}")
    return t

def q(name: str) -> str:
    return f"`{name}`"

def safe_default_literal(v: Optional[str]) -> Optional[str]:
    if v is None:
        return None
    s = v.strip()
    if s.upper() in ("NULL", "CURRENT_TIMESTAMP"):
        return s.upper()
    if re.fullmatch(r"-?\d+(\.\d+)?", s):
        return s
    if re.fullmatch(r"'[^']*'", s):
        return s
    raise ValueError(f"Unsafe default literal: {v!r}")

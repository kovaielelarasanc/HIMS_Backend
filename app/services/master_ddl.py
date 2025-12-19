# app/services/master_ddl.py
from typing import Dict, List
from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.services.master_sql_guard import assert_ident, assert_db, assert_type, q, safe_default_literal

DESTRUCTIVE = {"drop_database", "drop_table", "drop_column"}

def is_destructive(op: str) -> bool:
    return op in DESTRUCTIVE

def build_sql_for_op(op: Dict, db_name: str) -> List[str]:
    op_type = op.get("op")

    # Global ops
    if op_type == "create_database":
        db = assert_db(op["db_name"])
        return [f"CREATE DATABASE IF NOT EXISTS {q(db)} CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"]

    if op_type == "drop_database":
        db = assert_db(op["db_name"])
        return [f"DROP DATABASE IF EXISTS {q(db)}"]

    # Tenant-scoped ops
    dbq = q(assert_db(db_name))

    if op_type == "create_table":
        table = assert_ident(op["table"], "table")
        cols = op.get("columns") or []
        if not cols:
            raise ValueError("create_table requires columns[]")

        col_sql = []
        for c in cols:
            name = assert_ident(c["name"], "column")
            typ = assert_type(c["type"])
            nullable = "NULL" if c.get("nullable", True) else "NOT NULL"
            default = safe_default_literal(c.get("default"))
            default_sql = f" DEFAULT {default}" if default is not None else ""
            comment = (c.get("comment") or "").replace("'", "''")
            comment_sql = f" COMMENT '{comment}'" if comment else ""
            col_sql.append(f"{q(name)} {typ} {nullable}{default_sql}{comment_sql}")

        return [
            f"CREATE TABLE IF NOT EXISTS {dbq}.{q(table)} (\n  " + ",\n  ".join(col_sql) + "\n)"
            " ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"
        ]

    if op_type == "drop_table":
        table = assert_ident(op["table"], "table")
        return [f"DROP TABLE IF EXISTS {dbq}.{q(table)}"]

    if op_type == "add_column":
        table = assert_ident(op["table"], "table")
        c = op["column"]
        col = assert_ident(c["name"], "column")
        typ = assert_type(c["type"])
        nullable = "NULL" if c.get("nullable", True) else "NOT NULL"
        default = safe_default_literal(c.get("default"))
        default_sql = f" DEFAULT {default}" if default is not None else ""
        comment = (c.get("comment") or "").replace("'", "''")
        comment_sql = f" COMMENT '{comment}'" if comment else ""
        after = c.get("after")
        after_sql = f" AFTER {q(assert_ident(after,'column'))}" if after else ""
        return [f"ALTER TABLE {dbq}.{q(table)} ADD COLUMN {q(col)} {typ} {nullable}{default_sql}{comment_sql}{after_sql}"]

    if op_type == "modify_column":
        table = assert_ident(op["table"], "table")
        c = op["column"]
        col = assert_ident(c["name"], "column")
        typ = assert_type(c["type"])
        nullable = "NULL" if c.get("nullable", True) else "NOT NULL"
        default = safe_default_literal(c.get("default"))
        default_sql = f" DEFAULT {default}" if default is not None else ""
        comment = (c.get("comment") or "").replace("'", "''")
        comment_sql = f" COMMENT '{comment}'" if comment else ""
        return [f"ALTER TABLE {dbq}.{q(table)} MODIFY COLUMN {q(col)} {typ} {nullable}{default_sql}{comment_sql}"]

    if op_type == "drop_column":
        table = assert_ident(op["table"], "table")
        col = assert_ident(op["column_name"], "column")
        return [f"ALTER TABLE {dbq}.{q(table)} DROP COLUMN {q(col)}"]

    if op_type == "rename_column":
        table = assert_ident(op["table"], "table")
        old = assert_ident(op["old_column_name"], "old_column_name")
        new = assert_ident(op["new_column_name"], "new_column_name")
        return [f"ALTER TABLE {dbq}.{q(table)} RENAME COLUMN {q(old)} TO {q(new)}"]

    raise ValueError(f"Unsupported op: {op_type}")

def exec_sql(engine: Engine, sql: str) -> None:
    with engine.connect() as conn:
        conn.execute(text(sql))
        conn.commit()

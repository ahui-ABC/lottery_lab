"""SQLite 读写封装（设计文档 §3.3 / 实现计划 T2）。"""
import sqlite3
from pathlib import Path
from typing import Iterable

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def connect(db_path: str) -> sqlite3.Connection:
    """打开数据库连接，启用外键、行字典。"""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """执行 schema.sql 建表（幂等）。"""
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()


def upsert(conn: sqlite3.Connection, table: str, row: dict, keys: Iterable[str]) -> None:
    """按 keys 列做 INSERT ... ON CONFLICT DO UPDATE，幂等。"""
    keys = list(keys)
    cols = list(row)
    if not cols or not keys:
        raise ValueError("upsert 需要至少一列与冲突键")
    update_cols = [c for c in cols if c not in keys]
    if update_cols:
        updates = ",".join(f"{c}=excluded.{c}" for c in update_cols)
    else:
        # 仅冲突键 → DO NOTHING 等价；保持列在 ON CONFLICT 内即可
        updates = f"{keys[0]}={keys[0]}"
    sql = (
        f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))}) "
        f"ON CONFLICT({','.join(keys)}) DO UPDATE SET {updates}"
    )
    conn.execute(sql, [row[c] for c in cols])
    conn.commit()


def fetchone(conn: sqlite3.Connection, sql: str, params: Iterable = ()) -> sqlite3.Row | None:
    return conn.execute(sql, params).fetchone()


def fetchall(conn: sqlite3.Connection, sql: str, params: Iterable = ()) -> list[sqlite3.Row]:
    return list(conn.execute(sql, params))

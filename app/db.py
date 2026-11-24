"""SQLite 持久层：申报、核定版、预览快照。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from typing import Iterator

from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS recording (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL UNIQUE,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS declaration (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    recording_id INTEGER NOT NULL REFERENCES recording(id),
    order_no     TEXT NOT NULL,
    start_date   TEXT NOT NULL,
    end_date     TEXT NOT NULL,
    shares_json  TEXT NOT NULL,
    version      INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (recording_id, order_no)
);

CREATE TABLE IF NOT EXISTS declaration_history (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    declaration_id INTEGER NOT NULL REFERENCES declaration(id),
    order_no       TEXT NOT NULL,
    start_date     TEXT NOT NULL,
    end_date       TEXT NOT NULL,
    shares_json    TEXT NOT NULL,
    version        INTEGER NOT NULL,
    changed_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS settlement_version (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    recording_id     INTEGER NOT NULL REFERENCES recording(id),
    period_start     TEXT NOT NULL,
    period_end       TEXT NOT NULL,
    version_no       INTEGER NOT NULL,
    status           TEXT NOT NULL DEFAULT 'published',
    returned_orders  TEXT NOT NULL,
    retained_orders  TEXT NOT NULL,
    segments_json    TEXT NOT NULL,
    input_signature  TEXT NOT NULL,
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (recording_id, period_start, period_end, version_no)
);

CREATE TABLE IF NOT EXISTS preview_snapshot (
    token          TEXT PRIMARY KEY,
    recording_id   INTEGER NOT NULL,
    period_start   TEXT NOT NULL,
    period_end     TEXT NOT NULL,
    result_json    TEXT NOT NULL,
    input_signature TEXT NOT NULL,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_history_decl
    ON declaration_history (declaration_id, version);
"""

# 并发写入等待上限（毫秒）：同一时刻只有一个写事务，其余排队。
_BUSY_TIMEOUT_MS = 30000


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        # isolation_level=None：由下面的事务助手显式控制 BEGIN/COMMIT，
        # 避免隐式事务把读-改-写拆成多个不一致的快照。
        conn = sqlite3.connect(
            current_app.config["DATABASE"],
            detect_types=sqlite3.PARSE_DECLTYPES,
            timeout=_BUSY_TIMEOUT_MS / 1000,
            isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        g.db = conn
    return g.db


@contextmanager
def write_tx() -> Iterator[sqlite3.Connection]:
    """写事务：BEGIN IMMEDIATE 串行化并发的读-改-写。

    事务内重新读取现行值，因此并发的两次更正会按拿到写锁的先后
    依次入账（第二次看到并留档第一次的结果）。任一步失败整体回滚，
    现行申报与留档两处都保持原值。
    """
    conn = get_db()
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.rollback()
        raise
    try:
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


@contextmanager
def read_snapshot() -> Iterator[sqlite3.Connection]:
    """只读事务：多条查询共享同一个已提交快照。

    WAL 模式下读事务不阻塞写入，因此查档期间其他请求仍可保存更正。
    """
    conn = get_db()
    conn.execute("BEGIN")
    try:
        yield conn
    except BaseException:
        conn.rollback()
        raise
    try:
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def close_db(exc=None) -> None:
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db() -> None:
    conn = get_db()
    conn.executescript(SCHEMA)
    conn.commit()


# ---------- 录音 / 申报 ----------

def get_or_create_recording(title: str) -> int:
    conn = get_db()
    row = conn.execute(
        "SELECT id FROM recording WHERE title = ?", (title,)
    ).fetchone()
    if row:
        return row["id"]
    try:
        with write_tx() as tx:
            cur = tx.execute("INSERT INTO recording (title) VALUES (?)", (title,))
            return cur.lastrowid
    except sqlite3.IntegrityError:
        row = conn.execute(
            "SELECT id FROM recording WHERE title = ?", (title,)
        ).fetchone()
        if row is None:
            raise
        return row["id"]


def list_recordings() -> list[sqlite3.Row]:
    return get_db().execute("SELECT * FROM recording ORDER BY title").fetchall()


def get_recording(recording_id: int) -> sqlite3.Row | None:
    return get_db().execute(
        "SELECT * FROM recording WHERE id = ?", (recording_id,)
    ).fetchone()


def _row_to_decl(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "recording_id": row["recording_id"],
        "order_no": row["order_no"],
        "start_date": row["start_date"],
        "end_date": row["end_date"],
        "shares": json.loads(row["shares_json"]),
        "version": row["version"],
        "updated_at": row["updated_at"],
    }


def _row_to_history(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "declaration_id": row["declaration_id"],
        "order_no": row["order_no"],
        "start_date": row["start_date"],
        "end_date": row["end_date"],
        "shares": json.loads(row["shares_json"]),
        "version": row["version"],
        "changed_at": row["changed_at"],
    }


class DuplicateOrderError(ValueError):
    """同一录音下已存在该单号（仅新增时会抛出，绝不覆盖原申报）。"""


class DeclarationLimitError(ValueError):
    """超过每首录音 12 单上限。"""


def list_declarations(recording_id: int) -> list[dict]:
    rows = get_db().execute(
        "SELECT * FROM declaration WHERE recording_id = ? ORDER BY order_no",
        (recording_id,),
    ).fetchall()
    return [_row_to_decl(r) for r in rows]


def get_declaration(declaration_id: int) -> dict | None:
    row = get_db().execute(
        "SELECT * FROM declaration WHERE id = ?", (declaration_id,)
    ).fetchone()
    return _row_to_decl(row) if row else None


def find_declaration(recording_id: int, order_no: str) -> dict | None:
    row = get_db().execute(
        "SELECT * FROM declaration WHERE recording_id = ? AND order_no = ?",
        (recording_id, order_no),
    ).fetchone()
    return _row_to_decl(row) if row else None


def get_declaration_with_history(declaration_id: int) -> tuple[dict | None, list[dict]]:
    """同一已提交快照内读取现行申报与其留档。

    若两者来自不同提交状态，更正页会把“旧档”与“现值”拼错；
    只读事务保证一致，且 WAL 下不阻塞其他请求继续保存更正。
    """
    with read_snapshot() as conn:
        row = conn.execute(
            "SELECT * FROM declaration WHERE id = ?", (declaration_id,)
        ).fetchone()
        if row is None:
            return None, []
        history = conn.execute(
            "SELECT * FROM declaration_history WHERE declaration_id = ? "
            "ORDER BY version, id",
            (declaration_id,),
        ).fetchall()
    return _row_to_decl(row), [_row_to_history(h) for h in history]


def _insert_history(conn: sqlite3.Connection, existing: sqlite3.Row) -> None:
    """把即将被替换的现行版（版号、起止日、份额）写入留档。"""
    conn.execute(
        """INSERT INTO declaration_history
           (declaration_id, order_no, start_date, end_date, shares_json, version)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            existing["id"],
            existing["order_no"],
            existing["start_date"],
            existing["end_date"],
            existing["shares_json"],
            existing["version"],
        ),
    )


def create_declaration(
    recording_id: int,
    order_no: str,
    start_date: str,
    end_date: str,
    shares: dict[str, int],
) -> dict:
    """新增申报。事务内查重，重复单号只拒绝、不覆盖已有申报与留档。"""
    shares_json = json.dumps(shares, ensure_ascii=False, sort_keys=True)
    try:
        with write_tx() as conn:
            exists = conn.execute(
                "SELECT 1 FROM declaration WHERE recording_id = ? AND order_no = ?",
                (recording_id, order_no),
            ).fetchone()
            if exists:
                raise DuplicateOrderError(order_no)
            count = conn.execute(
                "SELECT COUNT(*) AS c FROM declaration WHERE recording_id = ?",
                (recording_id,),
            ).fetchone()["c"]
            if count >= 12:
                raise DeclarationLimitError("每首录音最多 12 单申报")
            cur = conn.execute(
                """INSERT INTO declaration
                   (recording_id, order_no, start_date, end_date, shares_json)
                   VALUES (?, ?, ?, ?, ?)""",
                (recording_id, order_no, start_date, end_date, shares_json),
            )
            declaration_id = cur.lastrowid
            row = conn.execute(
                "SELECT * FROM declaration WHERE id = ?", (declaration_id,)
            ).fetchone()
            return _row_to_decl(row)
    except sqlite3.IntegrityError as exc:
        # UNIQUE(recording_id, order_no) 兜底：并发下也绝不覆盖先入账的一份。
        raise DuplicateOrderError(order_no) from exc


def update_declaration(
    declaration_id: int,
    start_date: str,
    end_date: str,
    shares: dict[str, int],
) -> dict:
    """更正申报：同一事务内留档被替换的现行版并递增版号。

    BEGIN IMMEDIATE 使并发的两次更正按拿到写锁的顺序串行执行；
    第二次在锁内读到的是第一次提交后的版号，因此留档依次为 v1、v2，
    现行版为 v3。留档与现行版更新同事务，失败时两处都保留原值。
    """
    shares_json = json.dumps(shares, ensure_ascii=False, sort_keys=True)
    with write_tx() as conn:
        existing = conn.execute(
            "SELECT * FROM declaration WHERE id = ?", (declaration_id,)
        ).fetchone()
        if existing is None:
            raise LookupError(declaration_id)
        _insert_history(conn, existing)
        conn.execute(
            """UPDATE declaration
               SET start_date = ?, end_date = ?, shares_json = ?,
                   version = version + 1, updated_at = datetime('now')
               WHERE id = ?""",
            (start_date, end_date, shares_json, declaration_id),
        )
        row = conn.execute(
            "SELECT * FROM declaration WHERE id = ?", (declaration_id,)
        ).fetchone()
        return _row_to_decl(row)


def list_history(declaration_id: int) -> list[sqlite3.Row]:
    return get_db().execute(
        "SELECT * FROM declaration_history WHERE declaration_id = ? "
        "ORDER BY version, id",
        (declaration_id,),
    ).fetchall()


# ---------- 预览快照 ----------

def save_preview(token: str, recording_id: int, start: str, end: str,
                  result: dict, signature: str) -> None:
    with write_tx() as conn:
        conn.execute(
            """INSERT INTO preview_snapshot
               (token, recording_id, period_start, period_end, result_json, input_signature)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (token, recording_id, start, end,
             json.dumps(result, ensure_ascii=False), signature),
        )


def get_preview(token: str) -> dict | None:
    row = get_db().execute(
        "SELECT * FROM preview_snapshot WHERE token = ?", (token,)
    ).fetchone()
    if not row:
        return None
    return {
        "token": row["token"],
        "recording_id": row["recording_id"],
        "period_start": row["period_start"],
        "period_end": row["period_end"],
        "result": json.loads(row["result_json"]),
        "input_signature": row["input_signature"],
    }


# ---------- 核定版 ----------

def list_versions(recording_id: int) -> list[sqlite3.Row]:
    return get_db().execute(
        "SELECT * FROM settlement_version WHERE recording_id = ? "
        "ORDER BY period_start, period_end, version_no",
        (recording_id,),
    ).fetchall()


def get_version(version_id: int) -> sqlite3.Row | None:
    return get_db().execute(
        "SELECT * FROM settlement_version WHERE id = ?", (version_id,)
    ).fetchone()


def publish_version(recording_id: int, start: str, end: str, result: dict,
                    signature: str, fail: bool = False) -> int:
    """发布核定版；fail=True 时模拟写入失败（事务回滚，不产生新版）。

    版号在写事务内取 MAX+1，并发确认不会写出重复版号。
    """
    with write_tx() as conn:
        version_no = conn.execute(
            "SELECT COALESCE(MAX(version_no), 0) AS m FROM settlement_version "
            "WHERE recording_id = ? AND period_start = ? AND period_end = ?",
            (recording_id, start, end),
        ).fetchone()["m"] + 1
        if fail:
            raise sqlite3.OperationalError("模拟写入失败")
        cur = conn.execute(
            """INSERT INTO settlement_version
               (recording_id, period_start, period_end, version_no, status,
                returned_orders, retained_orders, segments_json, input_signature)
               VALUES (?, ?, ?, ?, 'published', ?, ?, ?, ?)""",
            (
                recording_id, start, end, version_no,
                json.dumps(result["returned"], ensure_ascii=False),
                json.dumps(result["retain"], ensure_ascii=False),
                json.dumps(result["segments"], ensure_ascii=False),
                signature,
            ),
        )
        return cur.lastrowid


# ---------- 资料指纹 ----------

def input_signature(declarations: list[dict], start: str, end: str) -> str:
    """对结算期内相关申报做规范哈希，确认时与预览指纹核对。"""
    relevant = sorted(
        (d for d in declarations
         if d["end_date"] > start and d["start_date"] < end),
        key=lambda d: (d["order_no"], d["id"]),
    )
    payload = [
        {
            "order_no": d["order_no"],
            "start_date": d["start_date"],
            "end_date": d["end_date"],
            "shares": {k: d["shares"][k] for k in sorted(d["shares"])},
            "version": d["version"],
        }
        for d in relevant
    ]
    blob = json.dumps(
        {"period": [start, end], "declarations": payload},
        ensure_ascii=False, sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()

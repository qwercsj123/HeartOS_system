"""SQLite 存储层。

替代原先散落在 data/ 目录下的多个 JSON 文件（users.json、notebooks.json、
notebook_sources.json、notebook_result_files.json、notebook_tombstones.json、
feedback.json、ai_configs.json、uploads/.meta.json）。

设计要点：
- WAL 模式：读写互不阻塞，多个写排队（毫秒级），解决 JSON 整文件覆盖导致的
  并发丢数据问题。
- 行级粒度：每个用户/每个对话各占一行，写入成本与单条记录大小相关，
  不再随总数据量线性增长。
- 读-改-写序列放进 BEGIN IMMEDIATE 事务（tx()），跨进程也安全。
- 首次启动自动把旧 JSON 文件导入（meta 表打标记，幂等）。
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .config import settings

DATA_DIR = Path(settings.users_file).resolve().parent
DB_PATH = DATA_DIR / "heartos.db"

# 消息 _html 持久化上限（main.py 保存路径同样使用这两个值）
MAX_PERSISTED_NOTEBOOK_MSG_HTML = 20_000
MAX_PERSISTED_NOTEBOOK_MSG_HTML_NO_IMAGE = 300_000

_MIGRATION_MARKER = "json_migrated_at"

_local = threading.local()
_init_lock = threading.Lock()
_initialized = False

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  k TEXT PRIMARY KEY,
  v TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY,
  username TEXT NOT NULL DEFAULT '',
  phone TEXT NOT NULL DEFAULT '',
  phone_verified INTEGER NOT NULL DEFAULT 0,
  phone_verified_at INTEGER NOT NULL DEFAULT 0,
  display_name TEXT NOT NULL DEFAULT '',
  name TEXT NOT NULL DEFAULT '',
  organization TEXT NOT NULL DEFAULT '',
  department TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL DEFAULT '',
  user_type TEXT NOT NULL DEFAULT '',
  use_case TEXT NOT NULL DEFAULT '',
  email TEXT NOT NULL DEFAULT '',
  salt TEXT NOT NULL DEFAULT '',
  password_hash TEXT NOT NULL DEFAULT '',
  active INTEGER NOT NULL DEFAULT 1,
  is_admin INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL DEFAULT 0,
  last_login_at INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_users_phone ON users(phone);
CREATE INDEX IF NOT EXISTS idx_users_username ON users(username COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS verification_codes (
  id TEXT PRIMARY KEY,
  phone TEXT NOT NULL DEFAULT '',
  purpose TEXT NOT NULL DEFAULT '',
  code_hash TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL DEFAULT 0,
  expires_at INTEGER NOT NULL DEFAULT 0,
  used INTEGER NOT NULL DEFAULT 0,
  attempts INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_codes_scope ON verification_codes(phone, purpose, created_at);

CREATE TABLE IF NOT EXISTS notebooks (
  uid TEXT NOT NULL,
  id TEXT NOT NULL,
  payload TEXT NOT NULL,
  position INTEGER NOT NULL DEFAULT 0,
  updated_at INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (uid, id)
);
CREATE INDEX IF NOT EXISTS idx_notebooks_uid_pos ON notebooks(uid, position);

CREATE TABLE IF NOT EXISTS notebook_sources (
  uid TEXT NOT NULL,
  notebook_id TEXT NOT NULL,
  items TEXT NOT NULL DEFAULT '[]',
  updated_at INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (uid, notebook_id)
);

CREATE TABLE IF NOT EXISTS notebook_result_files (
  uid TEXT NOT NULL,
  notebook_id TEXT NOT NULL,
  items TEXT NOT NULL DEFAULT '[]',
  updated_at INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (uid, notebook_id)
);

CREATE TABLE IF NOT EXISTS notebook_tombstones (
  uid TEXT NOT NULL,
  notebook_id TEXT NOT NULL,
  deleted_at INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (uid, notebook_id)
);

CREATE TABLE IF NOT EXISTS feedback (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL DEFAULT '',
  payload TEXT NOT NULL DEFAULT '{}',
  created_at INTEGER NOT NULL DEFAULT 0,
  updated_at INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_feedback_user ON feedback(user_id, created_at);

CREATE TABLE IF NOT EXISTS ai_configs (
  uid TEXT NOT NULL,
  provider TEXT NOT NULL,
  payload TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY (uid, provider)
);

CREATE TABLE IF NOT EXISTS file_meta (
  file_id TEXT PRIMARY KEY,
  payload TEXT NOT NULL DEFAULT '{}'
);
"""

_BUCKET_TABLES = {"notebook_sources", "notebook_result_files"}


# ---------------------------------------------------------------------------
# 连接与事务
# ---------------------------------------------------------------------------

def _connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.isolation_level = None  # 自动提交；事务由 tx() 显式管理
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def get_conn() -> sqlite3.Connection:
    init_db()
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = _connect()
        _local.conn = conn
    return conn


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    """读-改-写要放在同一个事务里，避免并发覆盖。支持嵌套（内层不重复开启）。"""
    conn = get_conn()
    if getattr(_local, "in_tx", False):
        yield conn
        return
    _local.in_tx = True
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        _local.in_tx = False


def init_db() -> None:
    global _initialized
    if _initialized:
        return
    with _init_lock:
        if _initialized:
            return
        conn = _connect()
        try:
            conn.executescript(_SCHEMA)
            _migrate_from_json(conn)
        finally:
            conn.close()
        _initialized = True


# ---------------------------------------------------------------------------
# meta（迁移/清理标记）
# ---------------------------------------------------------------------------

def meta_get(key: str) -> str:
    row = get_conn().execute("SELECT v FROM meta WHERE k = ?", (str(key),)).fetchone()
    return str(row["v"]) if row else ""


def meta_set(key: str, value: str) -> None:
    get_conn().execute(
        "INSERT OR REPLACE INTO meta (k, v) VALUES (?, ?)", (str(key), str(value))
    )


# ---------------------------------------------------------------------------
# 用户记录归一化（auth.py 复用）
# ---------------------------------------------------------------------------

def normalize_phone(phone: Any) -> str:
    digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
    if digits.startswith("86") and len(digits) == 13:
        digits = digits[2:]
    return digits


def normalize_user_record(raw: dict[str, Any]) -> dict[str, Any]:
    now = int(time.time())
    phone = normalize_phone(str(raw.get("phone") or ""))
    username = str(raw.get("username") or phone or "").strip()
    display_name = str(raw.get("display_name") or raw.get("name") or username).strip() or username
    return {
        "id": str(raw.get("id") or f"u_{secrets.token_hex(6)}"),
        "username": username,
        "phone": phone,
        "phone_verified": bool(raw.get("phone_verified", bool(phone))),
        "phone_verified_at": int(raw.get("phone_verified_at") or (now if phone else 0)),
        "display_name": display_name,
        "name": str(raw.get("name") or display_name),
        "organization": str(raw.get("organization") or ""),
        "department": str(raw.get("department") or ""),
        "title": str(raw.get("title") or ""),
        "user_type": str(raw.get("user_type") or ""),
        "use_case": str(raw.get("use_case") or ""),
        "email": str(raw.get("email") or ""),
        "salt": str(raw.get("salt") or ""),
        "password_hash": str(raw.get("password_hash") or ""),
        "active": bool(raw.get("active", True)),
        "is_admin": bool(raw.get("is_admin", str(raw.get("id") or "") == "u_admin")),
        "created_at": int(raw.get("created_at") or now),
        "last_login_at": int(raw.get("last_login_at") or 0),
    }


_USER_FIELDS = (
    "id", "username", "phone", "phone_verified", "phone_verified_at",
    "display_name", "name", "organization", "department", "title",
    "user_type", "use_case", "email", "salt", "password_hash",
    "active", "is_admin", "created_at", "last_login_at",
)
_USER_BOOL_FIELDS = {"phone_verified", "active", "is_admin"}


def _user_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    record = {key: row[key] for key in _USER_FIELDS}
    for key in _USER_BOOL_FIELDS:
        record[key] = bool(record[key])
    return record


# ---------------------------------------------------------------------------
# users
# ---------------------------------------------------------------------------

def user_get(*, user_id: str = "", phone: str = "", username: str = "") -> dict[str, Any] | None:
    conn = get_conn()
    target_id = str(user_id or "").strip()
    if target_id:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (target_id,)).fetchone()
        if row:
            return _user_row_to_dict(row)
    phone_norm = normalize_phone(phone)
    if phone_norm:
        row = conn.execute("SELECT * FROM users WHERE phone = ?", (phone_norm,)).fetchone()
        if row:
            return _user_row_to_dict(row)
    username_value = str(username or "").strip()
    if username_value:
        row = conn.execute(
            "SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username_value,)
        ).fetchone()
        if row:
            return _user_row_to_dict(row)
    return None


def user_upsert(record: dict[str, Any]) -> None:
    values = {key: record.get(key) for key in _USER_FIELDS}
    for key in _USER_BOOL_FIELDS:
        values[key] = 1 if values[key] else 0
    columns = ", ".join(_USER_FIELDS)
    placeholders = ", ".join(f":{key}" for key in _USER_FIELDS)
    get_conn().execute(
        f"INSERT OR REPLACE INTO users ({columns}) VALUES ({placeholders})", values
    )


def user_delete(user_id: str) -> None:
    get_conn().execute("DELETE FROM users WHERE id = ?", (str(user_id),))


def user_count() -> int:
    row = get_conn().execute("SELECT COUNT(*) AS n FROM users").fetchone()
    return int(row["n"])


def user_all() -> list[dict[str, Any]]:
    rows = get_conn().execute("SELECT * FROM users").fetchall()
    return [_user_row_to_dict(row) for row in rows]


# ---------------------------------------------------------------------------
# verification_codes
# ---------------------------------------------------------------------------

_CODE_FIELDS = ("id", "phone", "purpose", "code_hash", "created_at", "expires_at", "used", "attempts")


def _code_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    record = {key: row[key] for key in _CODE_FIELDS}
    record["used"] = bool(record["used"])
    return record


def codes_cleanup() -> None:
    now = int(time.time())
    get_conn().execute(
        "DELETE FROM verification_codes WHERE (expires_at > 0 AND expires_at < ?) OR (created_at > 0 AND created_at < ?)",
        (now - 3600, now - 86400),
    )


def code_latest(phone: str, purpose: str) -> dict[str, Any] | None:
    row = get_conn().execute(
        "SELECT * FROM verification_codes WHERE phone = ? AND purpose = ? ORDER BY created_at DESC LIMIT 1",
        (str(phone), str(purpose)),
    ).fetchone()
    return _code_row_to_dict(row) if row else None


def code_count_since(phone: str, purpose: str, since: int) -> int:
    row = get_conn().execute(
        "SELECT COUNT(*) AS n FROM verification_codes WHERE phone = ? AND purpose = ? AND created_at >= ?",
        (str(phone), str(purpose), int(since)),
    ).fetchone()
    return int(row["n"])


def code_insert(entry: dict[str, Any]) -> None:
    values = {key: entry.get(key) for key in _CODE_FIELDS}
    values["used"] = 1 if values["used"] else 0
    columns = ", ".join(_CODE_FIELDS)
    placeholders = ", ".join(f":{key}" for key in _CODE_FIELDS)
    get_conn().execute(
        f"INSERT OR REPLACE INTO verification_codes ({columns}) VALUES ({placeholders})", values
    )


def code_update(entry: dict[str, Any]) -> None:
    get_conn().execute(
        "UPDATE verification_codes SET used = ?, attempts = ? WHERE id = ?",
        (1 if entry.get("used") else 0, int(entry.get("attempts") or 0), str(entry.get("id"))),
    )


# ---------------------------------------------------------------------------
# notebooks（payload 为整个 notebook item 的 JSON 文档）
# ---------------------------------------------------------------------------

def notebook_get(uid: str, notebook_id: str) -> dict[str, Any] | None:
    row = get_conn().execute(
        "SELECT payload FROM notebooks WHERE uid = ? AND id = ?", (str(uid), str(notebook_id))
    ).fetchone()
    if not row:
        return None
    try:
        payload = json.loads(row["payload"])
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def notebook_list(uid: str) -> list[dict[str, Any]]:
    rows = get_conn().execute(
        "SELECT payload FROM notebooks WHERE uid = ? ORDER BY position ASC", (str(uid),)
    ).fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except Exception:
            continue
        if isinstance(payload, dict):
            items.append(payload)
    return items


def notebook_upsert(uid: str, item: dict[str, Any]) -> None:
    notebook_id = str(item.get("id") or "")
    payload = json.dumps(item, ensure_ascii=False)
    updated_at = int(item.get("updatedAt") or 0)
    with tx() as conn:
        row = conn.execute(
            "SELECT position FROM notebooks WHERE uid = ? AND id = ?", (str(uid), notebook_id)
        ).fetchone()
        if row is not None:
            position = int(row["position"])
        else:
            # 新对话排到最前面（与旧实现 items.insert(0, item) 一致）
            min_row = conn.execute(
                "SELECT MIN(position) AS p FROM notebooks WHERE uid = ?", (str(uid),)
            ).fetchone()
            position = (int(min_row["p"]) if min_row and min_row["p"] is not None else 1) - 1
        conn.execute(
            "INSERT OR REPLACE INTO notebooks (uid, id, payload, position, updated_at) VALUES (?, ?, ?, ?, ?)",
            (str(uid), notebook_id, payload, position, updated_at),
        )


def notebook_delete(uid: str, notebook_id: str) -> int:
    cur = get_conn().execute(
        "DELETE FROM notebooks WHERE uid = ? AND id = ?", (str(uid), str(notebook_id))
    )
    return cur.rowcount


def notebook_delete_many(uid: str, notebook_ids: list[str]) -> int:
    if not notebook_ids:
        return 0
    placeholders = ", ".join("?" for _ in notebook_ids)
    cur = get_conn().execute(
        f"DELETE FROM notebooks WHERE uid = ? AND id IN ({placeholders})",
        (str(uid), *[str(x) for x in notebook_ids]),
    )
    return cur.rowcount


def notebook_iter_all_payloads() -> Iterator[dict[str, Any]]:
    for row in get_conn().execute("SELECT payload FROM notebooks"):
        try:
            payload = json.loads(row["payload"])
        except Exception:
            continue
        if isinstance(payload, dict):
            yield payload


def notebook_iter_all() -> Iterator[tuple[str, dict[str, Any]]]:
    for row in get_conn().execute("SELECT uid, payload FROM notebooks"):
        try:
            payload = json.loads(row["payload"])
        except Exception:
            continue
        if isinstance(payload, dict):
            yield str(row["uid"]), payload


# ---------------------------------------------------------------------------
# notebook_sources / notebook_result_files（同构：items JSON + updatedAt）
# ---------------------------------------------------------------------------

def _check_bucket_table(table: str) -> str:
    if table not in _BUCKET_TABLES:
        raise ValueError(f"unknown bucket table: {table}")
    return table


def bucket_all(table: str, uid: str) -> dict[str, dict[str, Any]]:
    table = _check_bucket_table(table)
    rows = get_conn().execute(
        f"SELECT notebook_id, items, updated_at FROM {table} WHERE uid = ?", (str(uid),)
    ).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        try:
            items = json.loads(row["items"])
        except Exception:
            items = []
        out[str(row["notebook_id"])] = {
            "items": items if isinstance(items, list) else [],
            "updatedAt": int(row["updated_at"] or 0),
        }
    return out


def bucket_entry_get(table: str, uid: str, notebook_id: str) -> dict[str, Any] | None:
    table = _check_bucket_table(table)
    row = get_conn().execute(
        f"SELECT items, updated_at FROM {table} WHERE uid = ? AND notebook_id = ?",
        (str(uid), str(notebook_id)),
    ).fetchone()
    if not row:
        return None
    try:
        items = json.loads(row["items"])
    except Exception:
        items = []
    return {
        "items": items if isinstance(items, list) else [],
        "updatedAt": int(row["updated_at"] or 0),
    }


def bucket_entry_set(
    table: str, uid: str, notebook_id: str, items: list[dict[str, Any]], updated_at: int
) -> bool:
    """写入条目；若库内版本更新则拒绝（返回 False），与旧实现的时间戳冲突检查一致。"""
    table = _check_bucket_table(table)
    safe_updated_at = int(updated_at or 0)
    with tx() as conn:
        row = conn.execute(
            f"SELECT updated_at FROM {table} WHERE uid = ? AND notebook_id = ?",
            (str(uid), str(notebook_id)),
        ).fetchone()
        existing_updated_at = int(row["updated_at"] or 0) if row else 0
        if safe_updated_at and existing_updated_at and existing_updated_at > safe_updated_at:
            return False
        conn.execute(
            f"INSERT OR REPLACE INTO {table} (uid, notebook_id, items, updated_at) VALUES (?, ?, ?, ?)",
            (
                str(uid),
                str(notebook_id),
                json.dumps(items if isinstance(items, list) else [], ensure_ascii=False),
                safe_updated_at,
            ),
        )
        return True


def bucket_iter_all(table: str) -> Iterator[tuple[str, str, dict[str, Any]]]:
    table = _check_bucket_table(table)
    for row in get_conn().execute(f"SELECT uid, notebook_id, items, updated_at FROM {table}"):
        try:
            items = json.loads(row["items"])
        except Exception:
            continue
        yield (
            str(row["uid"]),
            str(row["notebook_id"]),
            {
                "items": items if isinstance(items, list) else [],
                "updatedAt": int(row["updated_at"] or 0),
            },
        )


def bucket_entry_delete(table: str, uid: str, notebook_id: str) -> None:
    table = _check_bucket_table(table)
    get_conn().execute(
        f"DELETE FROM {table} WHERE uid = ? AND notebook_id = ?", (str(uid), str(notebook_id))
    )


def bucket_delete_many(table: str, uid: str, notebook_ids: list[str]) -> None:
    table = _check_bucket_table(table)
    if not notebook_ids:
        return
    placeholders = ", ".join("?" for _ in notebook_ids)
    get_conn().execute(
        f"DELETE FROM {table} WHERE uid = ? AND notebook_id IN ({placeholders})",
        (str(uid), *[str(x) for x in notebook_ids]),
    )


# ---------------------------------------------------------------------------
# notebook_tombstones
# ---------------------------------------------------------------------------

def tombstones_all(uid: str) -> dict[str, int]:
    rows = get_conn().execute(
        "SELECT notebook_id, deleted_at FROM notebook_tombstones WHERE uid = ?", (str(uid),)
    ).fetchall()
    return {str(row["notebook_id"]): int(row["deleted_at"] or 0) for row in rows}


def tombstone_get(uid: str, notebook_id: str) -> int:
    row = get_conn().execute(
        "SELECT deleted_at FROM notebook_tombstones WHERE uid = ? AND notebook_id = ?",
        (str(uid), str(notebook_id)),
    ).fetchone()
    return int(row["deleted_at"] or 0) if row else 0


def tombstone_set(uid: str, notebook_id: str, deleted_at: int) -> None:
    get_conn().execute(
        "INSERT OR REPLACE INTO notebook_tombstones (uid, notebook_id, deleted_at) VALUES (?, ?, ?)",
        (str(uid), str(notebook_id), int(deleted_at or 0)),
    )


def tombstone_clear(uid: str, notebook_id: str) -> None:
    get_conn().execute(
        "DELETE FROM notebook_tombstones WHERE uid = ? AND notebook_id = ?",
        (str(uid), str(notebook_id)),
    )


def tombstone_delete_many(uid: str, notebook_ids: list[str]) -> None:
    if not notebook_ids:
        return
    placeholders = ", ".join("?" for _ in notebook_ids)
    get_conn().execute(
        f"DELETE FROM notebook_tombstones WHERE uid = ? AND notebook_id IN ({placeholders})",
        (str(uid), *[str(x) for x in notebook_ids]),
    )


# ---------------------------------------------------------------------------
# feedback
# ---------------------------------------------------------------------------

def _feedback_row_to_dict(row: sqlite3.Row) -> dict[str, Any] | None:
    try:
        payload = json.loads(row["payload"])
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def feedback_all() -> list[dict[str, Any]]:
    rows = get_conn().execute(
        "SELECT payload FROM feedback ORDER BY created_at DESC"
    ).fetchall()
    return [item for item in (_feedback_row_to_dict(row) for row in rows) if item]


def feedback_for_user(user_id: str) -> list[dict[str, Any]]:
    rows = get_conn().execute(
        "SELECT payload FROM feedback WHERE user_id = ? ORDER BY created_at DESC", (str(user_id),)
    ).fetchall()
    return [item for item in (_feedback_row_to_dict(row) for row in rows) if item]


def feedback_get(feedback_id: str) -> dict[str, Any] | None:
    row = get_conn().execute(
        "SELECT payload FROM feedback WHERE id = ?", (str(feedback_id),)
    ).fetchone()
    return _feedback_row_to_dict(row) if row else None


def feedback_save(record: dict[str, Any]) -> None:
    owner = record.get("user") if isinstance(record.get("user"), dict) else {}
    get_conn().execute(
        "INSERT OR REPLACE INTO feedback (id, user_id, payload, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        (
            str(record.get("id") or ""),
            str(owner.get("id") or ""),
            json.dumps(record, ensure_ascii=False),
            int(record.get("createdAt") or 0),
            int(record.get("updatedAt") or 0),
        ),
    )


def feedback_trim(limit: int = 1000) -> None:
    get_conn().execute(
        "DELETE FROM feedback WHERE id NOT IN (SELECT id FROM feedback ORDER BY created_at DESC LIMIT ?)",
        (int(limit),),
    )


# ---------------------------------------------------------------------------
# ai_configs
# ---------------------------------------------------------------------------

def ai_config_all(uid: str) -> dict[str, dict[str, Any]]:
    rows = get_conn().execute(
        "SELECT provider, payload FROM ai_configs WHERE uid = ?", (str(uid),)
    ).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except Exception:
            continue
        if isinstance(payload, dict):
            out[str(row["provider"])] = payload
    return out


def ai_config_set(uid: str, provider: str, payload: dict[str, Any]) -> None:
    get_conn().execute(
        "INSERT OR REPLACE INTO ai_configs (uid, provider, payload) VALUES (?, ?, ?)",
        (str(uid), str(provider), json.dumps(payload, ensure_ascii=False)),
    )


# ---------------------------------------------------------------------------
# file_meta（uploads/.meta.json 的替代）
# ---------------------------------------------------------------------------

def file_meta_all() -> dict[str, dict[str, Any]]:
    rows = get_conn().execute("SELECT file_id, payload FROM file_meta").fetchall()
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except Exception:
            continue
        if isinstance(payload, dict):
            out[str(row["file_id"])] = payload
    return out


def file_meta_get(file_id: str) -> dict[str, Any] | None:
    row = get_conn().execute(
        "SELECT payload FROM file_meta WHERE file_id = ?", (str(file_id),)
    ).fetchone()
    if not row:
        return None
    try:
        payload = json.loads(row["payload"])
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def file_meta_set(file_id: str, payload: dict[str, Any]) -> None:
    get_conn().execute(
        "INSERT OR REPLACE INTO file_meta (file_id, payload) VALUES (?, ?)",
        (str(file_id), json.dumps(payload, ensure_ascii=False)),
    )


def file_meta_delete(file_id: str) -> None:
    get_conn().execute("DELETE FROM file_meta WHERE file_id = ?", (str(file_id),))


# ---------------------------------------------------------------------------
# 旧 JSON 数据一次性迁移
# ---------------------------------------------------------------------------

def _read_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _trim_msg_html(item: dict[str, Any]) -> dict[str, Any]:
    """迁移时应用与当前保存路径一致的 _html 裁剪策略（main.py 同款阈值）。"""
    msgs = item.get("msgs")
    if not isinstance(msgs, list):
        return item
    trimmed: list[Any] = []
    for msg in msgs:
        if isinstance(msg, dict) and isinstance(msg.get("_html"), str):
            html = msg["_html"]
            limit = (
                MAX_PERSISTED_NOTEBOOK_MSG_HTML
                if "data:image" in html
                else MAX_PERSISTED_NOTEBOOK_MSG_HTML_NO_IMAGE
            )
            if len(html) > limit:
                msg = {key: value for key, value in msg.items() if key != "_html"}
        trimmed.append(msg)
    item = dict(item)
    item["msgs"] = trimmed
    return item


def _normalize_bucket_entry_raw(entry: Any) -> dict[str, Any]:
    if isinstance(entry, dict):
        items = entry.get("items")
        return {
            "items": items if isinstance(items, list) else [],
            "updatedAt": int(entry.get("updatedAt") or 0),
        }
    if isinstance(entry, list):
        return {"items": entry, "updatedAt": 0}
    return {"items": [], "updatedAt": 0}


def _migrate_from_json(conn: sqlite3.Connection) -> None:
    row = conn.execute("SELECT v FROM meta WHERE k = ?", (_MIGRATION_MARKER,)).fetchone()
    if row:
        return

    conn.execute("BEGIN IMMEDIATE")
    try:
        # users.json: {"users": [...], "verification_codes": [...]}
        users_raw = _read_json(Path(settings.users_file).resolve())
        if isinstance(users_raw, dict):
            for raw in users_raw.get("users") or []:
                if not isinstance(raw, dict):
                    continue
                record = normalize_user_record(raw)
                values = {key: record.get(key) for key in _USER_FIELDS}
                for key in _USER_BOOL_FIELDS:
                    values[key] = 1 if values[key] else 0
                columns = ", ".join(_USER_FIELDS)
                placeholders = ", ".join(f":{key}" for key in _USER_FIELDS)
                conn.execute(
                    f"INSERT OR REPLACE INTO users ({columns}) VALUES ({placeholders})", values
                )
            for raw in users_raw.get("verification_codes") or []:
                if not isinstance(raw, dict) or not raw.get("id"):
                    continue
                conn.execute(
                    "INSERT OR REPLACE INTO verification_codes (id, phone, purpose, code_hash, created_at, expires_at, used, attempts) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(raw.get("id")),
                        normalize_phone(raw.get("phone")),
                        str(raw.get("purpose") or ""),
                        str(raw.get("code_hash") or ""),
                        int(raw.get("created_at") or 0),
                        int(raw.get("expires_at") or 0),
                        1 if raw.get("used") else 0,
                        int(raw.get("attempts") or 0),
                    ),
                )

        # notebooks.json: {uid: [item, ...]}（列表顺序即展示顺序）
        notebooks_raw = _read_json(DATA_DIR / "notebooks.json")
        if isinstance(notebooks_raw, dict):
            for uid, items in notebooks_raw.items():
                if not isinstance(items, list):
                    continue
                for index, item in enumerate(items):
                    if not isinstance(item, dict) or not str(item.get("id") or "").strip():
                        continue
                    item = _trim_msg_html(item)
                    conn.execute(
                        "INSERT OR REPLACE INTO notebooks (uid, id, payload, position, updated_at) VALUES (?, ?, ?, ?, ?)",
                        (
                            str(uid),
                            str(item.get("id")),
                            json.dumps(item, ensure_ascii=False),
                            index,
                            int(item.get("updatedAt") or 0),
                        ),
                    )

        # notebook_sources.json / notebook_result_files.json: {uid: {nid: entry}}
        for filename, table in (
            ("notebook_sources.json", "notebook_sources"),
            ("notebook_result_files.json", "notebook_result_files"),
        ):
            raw = _read_json(DATA_DIR / filename)
            if not isinstance(raw, dict):
                continue
            for uid, bucket in raw.items():
                if not isinstance(bucket, dict):
                    continue
                for notebook_id, entry in bucket.items():
                    normalized = _normalize_bucket_entry_raw(entry)
                    conn.execute(
                        f"INSERT OR REPLACE INTO {table} (uid, notebook_id, items, updated_at) VALUES (?, ?, ?, ?)",
                        (
                            str(uid),
                            str(notebook_id),
                            json.dumps(normalized["items"], ensure_ascii=False),
                            int(normalized["updatedAt"]),
                        ),
                    )

        # notebook_tombstones.json: {uid: {nid: deleted_at}}
        tombstones_raw = _read_json(DATA_DIR / "notebook_tombstones.json")
        if isinstance(tombstones_raw, dict):
            for uid, bucket in tombstones_raw.items():
                if not isinstance(bucket, dict):
                    continue
                for notebook_id, deleted_at in bucket.items():
                    try:
                        deleted_at_int = int(deleted_at or 0)
                    except Exception:
                        continue
                    conn.execute(
                        "INSERT OR REPLACE INTO notebook_tombstones (uid, notebook_id, deleted_at) VALUES (?, ?, ?)",
                        (str(uid), str(notebook_id), deleted_at_int),
                    )

        # feedback.json: {"items": [...]}
        feedback_raw = _read_json(DATA_DIR / "feedback.json")
        if isinstance(feedback_raw, dict):
            for record in feedback_raw.get("items") or []:
                if not isinstance(record, dict) or not record.get("id"):
                    continue
                owner = record.get("user") if isinstance(record.get("user"), dict) else {}
                conn.execute(
                    "INSERT OR REPLACE INTO feedback (id, user_id, payload, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (
                        str(record.get("id")),
                        str(owner.get("id") or ""),
                        json.dumps(record, ensure_ascii=False),
                        int(record.get("createdAt") or 0),
                        int(record.get("updatedAt") or 0),
                    ),
                )

        # ai_configs.json: {uid: {provider: cfg}}
        ai_raw = _read_json(DATA_DIR / "ai_configs.json")
        if isinstance(ai_raw, dict):
            for uid, bucket in ai_raw.items():
                if not isinstance(bucket, dict):
                    continue
                for provider, cfg in bucket.items():
                    if not isinstance(cfg, dict):
                        continue
                    conn.execute(
                        "INSERT OR REPLACE INTO ai_configs (uid, provider, payload) VALUES (?, ?, ?)",
                        (str(uid), str(provider), json.dumps(cfg, ensure_ascii=False)),
                    )

        # uploads/.meta.json: {file_id: meta}
        meta_raw = _read_json(Path(settings.upload_dir).resolve() / ".meta.json")
        if isinstance(meta_raw, dict):
            for file_id, payload in meta_raw.items():
                if not isinstance(payload, dict):
                    continue
                conn.execute(
                    "INSERT OR REPLACE INTO file_meta (file_id, payload) VALUES (?, ?)",
                    (str(file_id), json.dumps(payload, ensure_ascii=False)),
                )

        conn.execute(
            "INSERT OR REPLACE INTO meta (k, v) VALUES (?, ?)",
            (_MIGRATION_MARKER, str(int(time.time()))),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def vacuum() -> None:
    """回收已删除数据占用的空间（清理大字段后库文件才会真正变小）。"""
    get_conn().execute("VACUUM")


# ---------------------------------------------------------------------------
# 测试辅助
# ---------------------------------------------------------------------------

def wipe_all_for_tests() -> None:
    """清空全部业务数据（保留迁移标记，避免把旧 JSON 重新导入）。"""
    conn = get_conn()
    with tx():
        for table in (
            "users", "verification_codes", "notebooks", "notebook_sources",
            "notebook_result_files", "notebook_tombstones", "feedback",
            "ai_configs", "file_meta", "meta",
        ):
            conn.execute(f"DELETE FROM {table}")
        conn.execute(
            "INSERT OR REPLACE INTO meta (k, v) VALUES (?, ?)",
            (_MIGRATION_MARKER, str(int(time.time()))),
        )

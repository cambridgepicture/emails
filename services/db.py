from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


DEFAULT_FOLDER_ROWS = [
    {"graph_folder_id": "inbox", "display_name": "Inbox", "role": "inbox"},
    {"graph_folder_id": "urgent", "display_name": "Urgent", "role": "urgent"},
    {"graph_folder_id": "action", "display_name": "Action", "role": "action"},
    {"graph_folder_id": "later", "display_name": "Later", "role": "later"},
    {"graph_folder_id": "financial", "display_name": "Financial", "role": "financial"},
    {"graph_folder_id": "delete_candidate", "display_name": "Delete Candidate", "role": "delete_candidate"},
    {"graph_folder_id": "archive_candidate", "display_name": "Archive Candidate", "role": "archive_candidate"},
    {"graph_folder_id": "ignored", "display_name": "Ignored", "role": "ignored"},
]


SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS settings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL UNIQUE,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mail_folders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    graph_folder_id TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    graph_message_id TEXT UNIQUE,
    internet_message_id TEXT UNIQUE,
    conversation_id TEXT,
    sender_email TEXT,
    sender_name TEXT,
    subject TEXT,
    body_excerpt TEXT,
    received_at TEXT,
    importance TEXT,
    to_recipients_json TEXT,
    cc_recipients_json TEXT,
    attachment_names_json TEXT,
    initial_folder TEXT,
    classified_folder TEXT,
    current_folder TEXT,
    classification_source TEXT,
    classification_confidence REAL,
    classification_reason TEXT,
    app_moved_at TEXT,
    last_seen_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 100,
    enabled INTEGER NOT NULL DEFAULT 1,
    conditions_json TEXT NOT NULL,
    action_json TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'manual',
    confidence REAL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS corrections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL,
    from_folder TEXT NOT NULL,
    to_folder TEXT NOT NULL,
    correction_type TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(message_id) REFERENCES messages(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS rule_suggestions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    proposed_rule_json TEXT NOT NULL,
    based_on_correction_count INTEGER NOT NULL,
    example_message_ids_json TEXT NOT NULL,
    target_folder TEXT NOT NULL,
    confidence REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS classification_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(message_id) REFERENCES messages(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_messages_received_at ON messages(received_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_current_folder ON messages(current_folder);
CREATE INDEX IF NOT EXISTS idx_rules_enabled_priority ON rules(enabled, priority);
CREATE INDEX IF NOT EXISTS idx_corrections_message_id ON corrections(message_id);
CREATE INDEX IF NOT EXISTS idx_rule_suggestions_status ON rule_suggestions(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_classification_events_message_id ON classification_events(message_id, created_at DESC);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect_db(path: str | Path) -> sqlite3.Connection:
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    now = utc_now()
    for row in DEFAULT_FOLDER_ROWS:
        conn.execute(
            """
            INSERT OR IGNORE INTO mail_folders (graph_folder_id, display_name, role, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (row["graph_folder_id"], row["display_name"], row["role"], now, now),
        )
    conn.commit()


def _json_load(value: Any, default: Any) -> Any:
    if value is None or value == "":
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return default


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True)


def get_setting(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return None if row is None else str(row["value"])


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    now = utc_now()
    conn.execute(
        """
        INSERT INTO settings (key, value)
        VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )
    conn.commit()


def list_folders(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM mail_folders ORDER BY role").fetchall()


def get_folder_by_role(conn: sqlite3.Connection, role: str):
    return conn.execute("SELECT * FROM mail_folders WHERE role = ?", (role,)).fetchone()


def get_folder_by_display_name(conn: sqlite3.Connection, display_name: str):
    return conn.execute("SELECT * FROM mail_folders WHERE display_name = ?", (display_name,)).fetchone()


def list_messages(conn: sqlite3.Connection, limit: int = 100) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM messages ORDER BY datetime(received_at) DESC, id DESC LIMIT ?",
        (limit,),
    ).fetchall()


def get_message(conn: sqlite3.Connection, message_id: int):
    return conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()


def upsert_message(conn: sqlite3.Connection, payload: dict[str, Any]) -> int:
    now = utc_now()
    graph_message_id = payload.get("graph_message_id")
    internet_message_id = payload.get("internet_message_id")
    lookup = None
    lookup_value = None
    if graph_message_id:
        lookup = "graph_message_id"
        lookup_value = graph_message_id
    elif internet_message_id:
        lookup = "internet_message_id"
        lookup_value = internet_message_id

    existing = None
    if lookup is not None:
        existing = conn.execute(f"SELECT * FROM messages WHERE {lookup} = ?", (lookup_value,)).fetchone()

    values = {
        "graph_message_id": graph_message_id,
        "internet_message_id": internet_message_id,
        "conversation_id": payload.get("conversation_id"),
        "sender_email": payload.get("sender_email"),
        "sender_name": payload.get("sender_name"),
        "subject": payload.get("subject"),
        "body_excerpt": payload.get("body_excerpt"),
        "received_at": payload.get("received_at") or now,
        "importance": payload.get("importance") or "normal",
        "to_recipients_json": _json_dump(payload.get("to_recipients") or []),
        "cc_recipients_json": _json_dump(payload.get("cc_recipients") or []),
        "attachment_names_json": _json_dump(payload.get("attachment_names") or []),
        "initial_folder": payload.get("initial_folder") or "inbox",
        "classified_folder": payload.get("classified_folder") or payload.get("initial_folder") or "inbox",
        "current_folder": payload.get("current_folder") or payload.get("initial_folder") or "inbox",
        "classification_source": payload.get("classification_source") or "unknown",
        "classification_confidence": payload.get("classification_confidence"),
        "classification_reason": payload.get("classification_reason"),
        "app_moved_at": payload.get("app_moved_at"),
        "last_seen_at": payload.get("last_seen_at") or now,
        "created_at": now,
        "updated_at": now,
    }

    if existing is None:
        columns = ", ".join(values.keys())
        placeholders = ", ".join(["?"] * len(values))
        conn.execute(
            f"INSERT INTO messages ({columns}) VALUES ({placeholders})",
            tuple(values.values()),
        )
        conn.commit()
        row = conn.execute(
            "SELECT id FROM messages WHERE rowid = last_insert_rowid()"
        ).fetchone()
        return int(row["id"])

    updates = {
        "graph_message_id": graph_message_id if graph_message_id is not None else existing["graph_message_id"],
        "internet_message_id": internet_message_id if internet_message_id is not None else existing["internet_message_id"],
        "conversation_id": payload.get("conversation_id") if payload.get("conversation_id") is not None else existing["conversation_id"],
        "sender_email": payload.get("sender_email") if payload.get("sender_email") is not None else existing["sender_email"],
        "sender_name": payload.get("sender_name") if payload.get("sender_name") is not None else existing["sender_name"],
        "subject": payload.get("subject") if payload.get("subject") is not None else existing["subject"],
        "body_excerpt": payload.get("body_excerpt") if payload.get("body_excerpt") is not None else existing["body_excerpt"],
        "received_at": payload.get("received_at") if payload.get("received_at") is not None else existing["received_at"],
        "importance": payload.get("importance") if payload.get("importance") is not None else existing["importance"],
        "to_recipients_json": _json_dump(payload.get("to_recipients") or _json_load(existing["to_recipients_json"], [])),
        "cc_recipients_json": _json_dump(payload.get("cc_recipients") or _json_load(existing["cc_recipients_json"], [])),
        "attachment_names_json": _json_dump(payload.get("attachment_names") or _json_load(existing["attachment_names_json"], [])),
        "initial_folder": payload.get("initial_folder") if payload.get("initial_folder") is not None else existing["initial_folder"],
        "current_folder": payload.get("current_folder") if payload.get("current_folder") is not None else existing["current_folder"],
        "last_seen_at": now,
        "updated_at": now,
    }
    assignment_sql = ", ".join([f"{key} = ?" for key in updates.keys()])
    params = [updates[key] for key in updates.keys()]
    params.append(int(existing["id"]))
    conn.execute(f"UPDATE messages SET {assignment_sql} WHERE id = ?", params)
    conn.commit()
    return int(existing["id"])


def update_message_fields(conn: sqlite3.Connection, message_id: int, **fields: Any) -> None:
    if not fields:
        return
    fields["updated_at"] = utc_now()
    assignment_sql = ", ".join([f"{key} = ?" for key in fields.keys()])
    conn.execute(
        f"UPDATE messages SET {assignment_sql} WHERE id = ?",
        (*fields.values(), message_id),
    )
    conn.commit()


def list_rules(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM rules ORDER BY enabled DESC, priority ASC, id ASC").fetchall()


def get_rule(conn: sqlite3.Connection, rule_id: int):
    return conn.execute("SELECT * FROM rules WHERE id = ?", (rule_id,)).fetchone()


def create_rule(
    conn: sqlite3.Connection,
    name: str,
    priority: int,
    enabled: bool,
    conditions_json: str,
    action_json: str,
    source: str = "manual",
    confidence: float | None = None,
) -> int:
    now = utc_now()
    cur = conn.execute(
        """
        INSERT INTO rules (name, priority, enabled, conditions_json, action_json, source, confidence, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (name, priority, 1 if enabled else 0, conditions_json, action_json, source, confidence, now, now),
    )
    conn.commit()
    return int(cur.lastrowid)


def update_rule_enabled(conn: sqlite3.Connection, rule_id: int, enabled: bool) -> None:
    conn.execute(
        "UPDATE rules SET enabled = ?, updated_at = ? WHERE id = ?",
        (1 if enabled else 0, utc_now(), rule_id),
    )
    conn.commit()


def delete_rule(conn: sqlite3.Connection, rule_id: int) -> None:
    conn.execute("DELETE FROM rules WHERE id = ?", (rule_id,))
    conn.commit()


def list_suggestions(conn: sqlite3.Connection, status: str | None = None) -> list[sqlite3.Row]:
    if status:
        return conn.execute(
            "SELECT * FROM rule_suggestions WHERE status = ? ORDER BY created_at DESC",
            (status,),
        ).fetchall()
    return conn.execute("SELECT * FROM rule_suggestions ORDER BY created_at DESC").fetchall()


def get_suggestion(conn: sqlite3.Connection, suggestion_id: int):
    return conn.execute("SELECT * FROM rule_suggestions WHERE id = ?", (suggestion_id,)).fetchone()


def create_suggestion(
    conn: sqlite3.Connection,
    name: str,
    proposed_rule_json: str,
    based_on_correction_count: int,
    example_message_ids_json: str,
    target_folder: str,
    confidence: float,
    status: str = "pending",
) -> int:
    now = utc_now()
    cur = conn.execute(
        """
        INSERT INTO rule_suggestions
        (name, proposed_rule_json, based_on_correction_count, example_message_ids_json, target_folder, confidence, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            name,
            proposed_rule_json,
            based_on_correction_count,
            example_message_ids_json,
            target_folder,
            confidence,
            status,
            now,
            now,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def update_suggestion_status(conn: sqlite3.Connection, suggestion_id: int, status: str) -> None:
    conn.execute(
        "UPDATE rule_suggestions SET status = ?, updated_at = ? WHERE id = ?",
        (status, utc_now(), suggestion_id),
    )
    conn.commit()


def insert_correction(
    conn: sqlite3.Connection,
    message_id: int,
    from_folder: str,
    to_folder: str,
    correction_type: str,
) -> int:
    now = utc_now()
    cur = conn.execute(
        """
        INSERT INTO corrections (message_id, from_folder, to_folder, correction_type, detected_at, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (message_id, from_folder, to_folder, correction_type, now, now),
    )
    conn.commit()
    return int(cur.lastrowid)


def count_corrections(conn: sqlite3.Connection, from_folder: str, to_folder: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS count FROM corrections WHERE from_folder = ? AND to_folder = ?",
        (from_folder, to_folder),
    ).fetchone()
    return int(row["count"] if row else 0)


def list_recent_corrections(conn: sqlite3.Connection, limit: int = 100) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT c.*, m.subject, m.sender_email
        FROM corrections c
        JOIN messages m ON m.id = c.message_id
        ORDER BY c.created_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def list_classification_events(conn: sqlite3.Connection, message_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM classification_events WHERE message_id = ? ORDER BY created_at DESC",
        (message_id,),
    ).fetchall()


def add_classification_event(conn: sqlite3.Connection, message_id: int, event_type: str, details: dict[str, Any]) -> int:
    now = utc_now()
    cur = conn.execute(
        """
        INSERT INTO classification_events (message_id, event_type, details_json, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (message_id, event_type, _json_dump(details), now),
    )
    conn.commit()
    return int(cur.lastrowid)


def load_json_text(value: Any, default: Any) -> Any:
    return _json_load(value, default)


def dump_json(value: Any) -> str:
    return _json_dump(value)

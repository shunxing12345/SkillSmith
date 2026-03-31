"""Synchronous sqlite3 fallback for session/conversation services.

This keeps the public async API stable while avoiding fragile aiosqlite
runtime behavior in restricted environments.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from middleware.config import g_config


def get_db_path() -> Path:
    return g_config.get_db_path()


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    ensure_schema(conn)
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            description TEXT,
            status TEXT NOT NULL,
            meta_info TEXT NOT NULL DEFAULT '{}',
            conversation_count INTEGER NOT NULL DEFAULT 0,
            total_tokens INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS conversations (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            role TEXT NOT NULL,
            title TEXT NOT NULL,
            content TEXT,
            content_detail TEXT,
            tool_calls TEXT,
            tool_call_id TEXT,
            meta_info TEXT NOT NULL DEFAULT '{}',
            tokens INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_session_updated
        ON sessions(updated_at DESC);

        CREATE INDEX IF NOT EXISTS idx_conversation_session_sequence
        ON conversations(session_id, sequence);
        """
    )
    conn.commit()


def dumps_json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False)


def loads_json(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


def new_id() -> str:
    return str(uuid.uuid4())

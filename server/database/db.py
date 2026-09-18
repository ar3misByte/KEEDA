"""SQLite persistence for KiCad Live.

One file, no server, no extra infrastructure - the project must keep working
on a LAN with no internet. SQLite is in the Python standard library, so there
is nothing extra to install on the five demo machines.

What is persisted: projects, users, sessions, events, comments, versions and
per-user read markers. What is deliberately NOT persisted: locks and presence.
Both describe who is holding something *right now*; restoring them after a
restart would resurrect locks owned by nobody.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
import uuid as uuidlib

log = logging.getLogger("kicadlive.db")

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS projects (
    project_id   TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    created_at   REAL NOT NULL,
    last_seen_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    user_id       TEXT NOT NULL,
    project_id    TEXT NOT NULL,
    username      TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'designer',
    first_seen_at REAL NOT NULL,
    last_seen_at  REAL NOT NULL,
    PRIMARY KEY (project_id, user_id)
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    user_id    TEXT NOT NULL,
    username   TEXT NOT NULL,
    started_at REAL NOT NULL,
    ended_at   REAL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(project_id, user_id, started_at);

CREATE TABLE IF NOT EXISTS events (
    event_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  TEXT NOT NULL,
    ts          REAL NOT NULL,
    user_id     TEXT,
    username    TEXT,
    domain      TEXT NOT NULL,
    object_type TEXT,
    object_id   TEXT,
    object_ref  TEXT,
    action      TEXT NOT NULL,
    field       TEXT,
    old_value   TEXT,
    new_value   TEXT,
    description TEXT NOT NULL,
    version     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_events_project_ts ON events(project_id, ts);
CREATE INDEX IF NOT EXISTS idx_events_user       ON events(project_id, user_id, ts);
CREATE INDEX IF NOT EXISTS idx_events_domain     ON events(project_id, domain, ts);
CREATE INDEX IF NOT EXISTS idx_events_object     ON events(project_id, object_id);

CREATE TABLE IF NOT EXISTS comments (
    comment_id  TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL,
    parent_id   TEXT,
    domain      TEXT NOT NULL,
    object_type TEXT,
    object_id   TEXT NOT NULL,
    object_ref  TEXT,
    sheet       TEXT,
    author_id   TEXT NOT NULL,
    author_name TEXT NOT NULL,
    text        TEXT NOT NULL,
    created_at  REAL NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open',
    resolved_by TEXT,
    resolved_at REAL
);
CREATE INDEX IF NOT EXISTS idx_comments_status ON comments(project_id, status);
CREATE INDEX IF NOT EXISTS idx_comments_object ON comments(project_id, domain, object_id);
CREATE INDEX IF NOT EXISTS idx_comments_parent ON comments(parent_id);

CREATE TABLE IF NOT EXISTS versions (
    version_id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    domain     TEXT NOT NULL,
    version_no INTEGER NOT NULL,
    ts         REAL NOT NULL,
    user_id    TEXT,
    username   TEXT,
    summary    TEXT
);
CREATE INDEX IF NOT EXISTS idx_versions_project ON versions(project_id, version_no);

CREATE TABLE IF NOT EXISTS read_markers (
    project_id          TEXT NOT NULL,
    user_id             TEXT NOT NULL,
    last_read_event_id  INTEGER NOT NULL DEFAULT 0,
    updated_at          REAL NOT NULL,
    PRIMARY KEY (project_id, user_id)
);
"""


def now() -> float:
    return time.time()


def new_id() -> str:
    return uuidlib.uuid4().hex[:16]


def _encode(value) -> str | None:
    """Store structured values as JSON so the change inspector can show them."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value)
    except (TypeError, ValueError):
        return str(value)


def decode(value):
    if value is None:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


class Database:
    """Thin SQLite wrapper.

    A single connection guarded by a lock. The server is asyncio and its
    queries are sub-millisecond on a hackathon-sized project, so a connection
    pool would add moving parts for no measurable gain.
    """

    def __init__(self, path: str):
        self.path = path
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()
        log.info("database ready at %s", path)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _execute(self, sql: str, params=()) -> sqlite3.Cursor:
        with self._lock:
            cursor = self._conn.execute(sql, params)
            self._conn.commit()
            return cursor

    def _query(self, sql: str, params=()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    # ------------------------------------------------------------ projects

    def ensure_project(self, project_id: str, name: str | None = None) -> None:
        current = now()
        self._execute(
            "INSERT INTO projects (project_id, name, created_at, last_seen_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(project_id) DO UPDATE SET last_seen_at = excluded.last_seen_at",
            (project_id, name or project_id, current, current))

    def projects(self) -> list[dict]:
        return [dict(r) for r in self._query(
            "SELECT * FROM projects ORDER BY last_seen_at DESC")]

    # --------------------------------------------------------------- users

    def ensure_user(self, project_id: str, user_id: str, username: str,
                    role: str = "designer") -> dict:
        current = now()
        existing = self._query(
            "SELECT * FROM users WHERE project_id = ? AND user_id = ?",
            (project_id, user_id))
        if existing:
            # Never silently downgrade a role that was set deliberately.
            self._execute(
                "UPDATE users SET username = ?, last_seen_at = ? "
                "WHERE project_id = ? AND user_id = ?",
                (username, current, project_id, user_id))
            row = dict(existing[0])
            row["username"] = username
            return row
        self._execute(
            "INSERT INTO users (user_id, project_id, username, role, first_seen_at, last_seen_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, project_id, username, role, current, current))
        return {"user_id": user_id, "project_id": project_id, "username": username,
                "role": role, "first_seen_at": current, "last_seen_at": current}

    def set_role(self, project_id: str, user_id: str, role: str) -> None:
        self._execute("UPDATE users SET role = ? WHERE project_id = ? AND user_id = ?",
                      (role, project_id, user_id))

    def users(self, project_id: str) -> list[dict]:
        return [dict(r) for r in self._query(
            "SELECT * FROM users WHERE project_id = ? ORDER BY username COLLATE NOCASE",
            (project_id,))]

    def get_user(self, project_id: str, user_id: str) -> dict | None:
        rows = self._query("SELECT * FROM users WHERE project_id = ? AND user_id = ?",
                           (project_id, user_id))
        return dict(rows[0]) if rows else None

    # ------------------------------------------------------------ sessions

    def start_session(self, project_id: str, user_id: str, username: str) -> str:
        session_id = new_id()
        self._execute(
            "INSERT INTO sessions (session_id, project_id, user_id, username, started_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (session_id, project_id, user_id, username, now()))
        return session_id

    def end_session(self, session_id: str) -> None:
        self._execute("UPDATE sessions SET ended_at = ? WHERE session_id = ? AND ended_at IS NULL",
                      (now(), session_id))

    def previous_session(self, project_id: str, user_id: str) -> dict | None:
        """The most recent ENDED session, used for the welcome-back summary."""
        rows = self._query(
            "SELECT * FROM sessions WHERE project_id = ? AND user_id = ? "
            "AND ended_at IS NOT NULL ORDER BY ended_at DESC LIMIT 1",
            (project_id, user_id))
        return dict(rows[0]) if rows else None

    # -------------------------------------------------------------- events

    def add_event(self, project_id: str, domain: str, action: str, description: str,
                  user_id: str | None = None, username: str | None = None,
                  object_type: str | None = None, object_id: str | None = None,
                  object_ref: str | None = None, field: str | None = None,
                  old_value=None, new_value=None, version: int | None = None) -> dict:
        timestamp = now()
        cursor = self._execute(
            "INSERT INTO events (project_id, ts, user_id, username, domain, object_type, "
            "object_id, object_ref, action, field, old_value, new_value, description, version) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (project_id, timestamp, user_id, username, domain, object_type, object_id,
             object_ref, action, field, _encode(old_value), _encode(new_value),
             description, version))
        return {
            "event_id": cursor.lastrowid, "project_id": project_id, "ts": timestamp,
            "user_id": user_id, "username": username, "domain": domain,
            "object_type": object_type, "object_id": object_id, "object_ref": object_ref,
            "action": action, "field": field, "old_value": old_value,
            "new_value": new_value, "description": description, "version": version,
        }

    def events(self, project_id: str, limit: int = 100, since_event_id: int | None = None,
               user_id: str | None = None, domain: str | None = None,
               action: str | None = None, since_ts: float | None = None,
               object_id: str | None = None) -> list[dict]:
        sql = "SELECT * FROM events WHERE project_id = ?"
        params: list = [project_id]
        if since_event_id is not None:
            sql += " AND event_id > ?"; params.append(since_event_id)
        if user_id:
            sql += " AND user_id = ?"; params.append(user_id)
        if domain:
            sql += " AND domain = ?"; params.append(domain)
        if action:
            sql += " AND action = ?"; params.append(action)
        if since_ts is not None:
            sql += " AND ts >= ?"; params.append(since_ts)
        if object_id:
            sql += " AND object_id = ?"; params.append(object_id)
        sql += " ORDER BY event_id DESC LIMIT ?"
        params.append(max(1, min(int(limit), 1000)))

        rows = []
        for row in self._query(sql, tuple(params)):
            item = dict(row)
            item["old_value"] = decode(item["old_value"])
            item["new_value"] = decode(item["new_value"])
            rows.append(item)
        return rows

    def event_by_id(self, project_id: str, event_id: int) -> dict | None:
        rows = self._query("SELECT * FROM events WHERE project_id = ? AND event_id = ?",
                           (project_id, event_id))
        if not rows:
            return None
        item = dict(rows[0])
        item["old_value"] = decode(item["old_value"])
        item["new_value"] = decode(item["new_value"])
        return item

    def latest_event_id(self, project_id: str) -> int:
        rows = self._query("SELECT MAX(event_id) AS m FROM events WHERE project_id = ?",
                           (project_id,))
        return int(rows[0]["m"] or 0)

    def count_events(self, project_id: str, since_event_id: int = 0,
                     domain: str | None = None) -> int:
        sql = "SELECT COUNT(*) AS c FROM events WHERE project_id = ? AND event_id > ?"
        params: list = [project_id, since_event_id]
        if domain:
            sql += " AND domain = ?"; params.append(domain)
        return int(self._query(sql, tuple(params))[0]["c"])

    # ------------------------------------------------------------ comments

    def add_comment(self, project_id: str, domain: str, object_id: str, text: str,
                    author_id: str, author_name: str, object_type: str | None = None,
                    object_ref: str | None = None, sheet: str | None = None,
                    parent_id: str | None = None) -> dict:
        comment_id = new_id()
        created = now()
        self._execute(
            "INSERT INTO comments (comment_id, project_id, parent_id, domain, object_type, "
            "object_id, object_ref, sheet, author_id, author_name, text, created_at, status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'open')",
            (comment_id, project_id, parent_id, domain, object_type, object_id,
             object_ref, sheet, author_id, author_name, text, created))
        return self.comment(comment_id)

    def comment(self, comment_id: str) -> dict | None:
        rows = self._query("SELECT * FROM comments WHERE comment_id = ?", (comment_id,))
        return dict(rows[0]) if rows else None

    def comments(self, project_id: str, status: str | None = None,
                 domain: str | None = None, object_id: str | None = None,
                 author_id: str | None = None) -> list[dict]:
        sql = "SELECT * FROM comments WHERE project_id = ?"
        params: list = [project_id]
        if status and status != "all":
            if status == "open":
                sql += " AND status IN ('open','reopened')"
            else:
                sql += " AND status = ?"; params.append(status)
        if domain:
            sql += " AND domain = ?"; params.append(domain)
        if object_id:
            sql += " AND object_id = ?"; params.append(object_id)
        if author_id:
            sql += " AND author_id = ?"; params.append(author_id)
        sql += " ORDER BY created_at ASC"
        return [dict(r) for r in self._query(sql, tuple(params))]

    def set_comment_status(self, comment_id: str, status: str,
                           user_id: str | None = None) -> dict | None:
        if status == "resolved":
            self._execute(
                "UPDATE comments SET status = ?, resolved_by = ?, resolved_at = ? "
                "WHERE comment_id = ?", (status, user_id, now(), comment_id))
        else:
            self._execute(
                "UPDATE comments SET status = ?, resolved_by = NULL, resolved_at = NULL "
                "WHERE comment_id = ?", (status, comment_id))
        return self.comment(comment_id)

    def count_open_comments(self, project_id: str) -> int:
        return int(self._query(
            "SELECT COUNT(*) AS c FROM comments WHERE project_id = ? "
            "AND status IN ('open','reopened') AND parent_id IS NULL",
            (project_id,))[0]["c"])

    # ------------------------------------------------------------ versions

    def add_version(self, project_id: str, domain: str, version_no: int,
                    user_id: str | None, username: str | None, summary: str) -> None:
        self._execute(
            "INSERT INTO versions (project_id, domain, version_no, ts, user_id, username, summary) "
            "VALUES (?,?,?,?,?,?,?)",
            (project_id, domain, version_no, now(), user_id, username, summary))

    def versions(self, project_id: str, limit: int = 100) -> list[dict]:
        return [dict(r) for r in self._query(
            "SELECT * FROM versions WHERE project_id = ? ORDER BY version_id DESC LIMIT ?",
            (project_id, limit))]

    def max_version(self, project_id: str, domain: str | None = None) -> int:
        if domain:
            rows = self._query(
                "SELECT MAX(version_no) AS m FROM versions WHERE project_id = ? AND domain = ?",
                (project_id, domain))
        else:
            rows = self._query("SELECT MAX(version_no) AS m FROM versions WHERE project_id = ?",
                               (project_id,))
        return int(rows[0]["m"] or 0)

    # -------------------------------------------------------- read markers

    def last_read_event(self, project_id: str, user_id: str) -> int:
        rows = self._query(
            "SELECT last_read_event_id FROM read_markers WHERE project_id = ? AND user_id = ?",
            (project_id, user_id))
        return int(rows[0]["last_read_event_id"]) if rows else 0

    def mark_read(self, project_id: str, user_id: str, event_id: int | None = None) -> int:
        if event_id is None:
            event_id = self.latest_event_id(project_id)
        self._execute(
            "INSERT INTO read_markers (project_id, user_id, last_read_event_id, updated_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(project_id, user_id) DO UPDATE SET "
            "last_read_event_id = excluded.last_read_event_id, updated_at = excluded.updated_at",
            (project_id, user_id, event_id, now()))
        return event_id

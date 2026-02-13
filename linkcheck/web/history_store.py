# Copyright (C) 2024 LinkChecker Authors
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
"""
SQLite-based history store for web UI check sessions.
"""

import json
import os
import sqlite3
import time
import uuid


_DB_DIR = os.path.join(os.path.expanduser("~"), ".linkchecker")
_DB_PATH = os.path.join(_DB_DIR, "web_history.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    urls        TEXT NOT NULL,
    created_at  REAL NOT NULL,
    duration    REAL DEFAULT 0,
    total       INTEGER DEFAULT 0,
    valid       INTEGER DEFAULT 0,
    errors      INTEGER DEFAULT 0,
    warnings    INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS results (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    url         TEXT NOT NULL,
    parent_url  TEXT DEFAULT '',
    result      TEXT DEFAULT '',
    valid       INTEGER DEFAULT 1,
    warnings    TEXT DEFAULT '[]',
    checktime   REAL DEFAULT 0,
    size        INTEGER DEFAULT -1,
    content_type TEXT DEFAULT '',
    level       INTEGER DEFAULT 0,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);

CREATE INDEX IF NOT EXISTS idx_results_session ON results(session_id);
CREATE INDEX IF NOT EXISTS idx_sessions_created ON sessions(created_at);
"""


class HistoryStore:
    """SQLite WAL-mode store for check session history."""

    def __init__(self, db_path=None):
        self.db_path = db_path or _DB_PATH
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_db()

    def _init_db(self):
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        return conn

    def save_session(self, urls, results, stats, duration):
        """Save a completed check session.

        Args:
            urls: List of checked URL strings.
            results: List of result dicts from GradioLogger.
            stats: LogStatistics object.
            duration: Check duration in seconds.

        Returns:
            Session ID string.
        """
        session_id = str(uuid.uuid4())
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO sessions
                   (id, urls, created_at, duration, total, valid, errors, warnings)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id,
                    json.dumps(urls),
                    time.time(),
                    duration,
                    stats.number if stats else len(results),
                    (stats.number - stats.errors) if stats else
                    sum(1 for r in results if r.get("valid")),
                    stats.errors if stats else
                    sum(1 for r in results if not r.get("valid")),
                    stats.warnings if stats else 0,
                ),
            )
            for r in results:
                warnings_json = json.dumps(r.get("warnings", []))
                conn.execute(
                    """INSERT INTO results
                       (session_id, url, parent_url, result, valid,
                        warnings, checktime, size, content_type, level)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        session_id,
                        r.get("url", ""),
                        r.get("parent_url", ""),
                        r.get("result", ""),
                        1 if r.get("valid") else 0,
                        warnings_json,
                        r.get("checktime", 0),
                        r.get("size", -1) or -1,
                        r.get("content_type", ""),
                        r.get("level", 0),
                    ),
                )
        return session_id

    def get_sessions(self, limit=50):
        """Return recent sessions as list of dicts."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM sessions ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_session_results(self, session_id):
        """Return all results for a session."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM results WHERE session_id=? ORDER BY id",
                (session_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def delete_session(self, session_id):
        """Delete a session and its results."""
        with self._connect() as conn:
            conn.execute("DELETE FROM results WHERE session_id=?", (session_id,))
            conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))

    def get_trend_data(self, url_pattern=None, days=30):
        """Return daily error counts for trend plotting.

        Args:
            url_pattern: Optional URL substring filter.
            days: Number of days to look back.

        Returns:
            List of (date_str, error_count, total_count) tuples.
        """
        cutoff = time.time() - days * 86400
        with self._connect() as conn:
            if url_pattern:
                rows = conn.execute(
                    """SELECT date(created_at, 'unixepoch', 'localtime') as day,
                              SUM(errors) as err, SUM(total) as tot
                       FROM sessions
                       WHERE created_at >= ? AND urls LIKE ?
                       GROUP BY day ORDER BY day""",
                    (cutoff, f"%{url_pattern}%"),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT date(created_at, 'unixepoch', 'localtime') as day,
                              SUM(errors) as err, SUM(total) as tot
                       FROM sessions
                       WHERE created_at >= ?
                       GROUP BY day ORDER BY day""",
                    (cutoff,),
                ).fetchall()
        return [(r["day"], r["err"], r["tot"]) for r in rows]

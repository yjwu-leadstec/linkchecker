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
SQLite persistent storage backend for LinkChecker.

Provides thread-safe database operations for URL queue persistence
and check result caching, enabling large-site scanning without OOM
and breakpoint resume capabilities.
"""

import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone

from .. import log, LOG_CACHE


class SqliteStore:
    """Thread-safe SQLite storage backend.

    Uses WAL journal mode for concurrent read access. All write operations
    are serialized through a threading lock. Thread-local connections are
    tracked in a list for proper cleanup on close().
    """

    SCHEMA_VERSION = 1

    CREATE_TABLES = """
    -- Run metadata for resume support
    CREATE TABLE IF NOT EXISTS run_metadata (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );

    -- URL queue with status tracking
    CREATE TABLE IF NOT EXISTS url_queue (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        url                 TEXT NOT NULL,
        cache_url           TEXT,
        parent_url          TEXT DEFAULT '',
        base_ref            TEXT DEFAULT '',
        recursion_level     INTEGER DEFAULT 0,
        line                INTEGER DEFAULT 0,
        column_num          INTEGER DEFAULT 0,
        page                INTEGER DEFAULT 0,
        name                TEXT DEFAULT '',
        extern              TEXT DEFAULT '',
        url_encoding        TEXT DEFAULT '',
        parent_content_type TEXT DEFAULT '',
        status              TEXT DEFAULT 'pending',
        created_at          REAL NOT NULL,
        updated_at          REAL
    );
    CREATE INDEX IF NOT EXISTS idx_queue_status ON url_queue(status);
    CREATE UNIQUE INDEX IF NOT EXISTS idx_queue_cache_url
        ON url_queue(cache_url) WHERE cache_url IS NOT NULL;

    -- Check results cache
    CREATE TABLE IF NOT EXISTS check_results (
        cache_url       TEXT PRIMARY KEY,
        url             TEXT NOT NULL,
        valid           INTEGER DEFAULT 1,
        extern          INTEGER DEFAULT 0,
        result          TEXT DEFAULT '',
        warnings        TEXT DEFAULT '[]',
        info            TEXT DEFAULT '[]',
        name            TEXT DEFAULT '',
        title           TEXT DEFAULT '',
        parent_url      TEXT DEFAULT '',
        base_ref        TEXT DEFAULT '',
        base_url        TEXT DEFAULT '',
        domain          TEXT DEFAULT '',
        content_type    TEXT DEFAULT '',
        size            INTEGER DEFAULT -1,
        modified        TEXT,
        dltime          REAL DEFAULT -1,
        checktime       REAL DEFAULT 0,
        line            INTEGER DEFAULT 0,
        column_num      INTEGER DEFAULT 0,
        page            INTEGER DEFAULT 0,
        level           INTEGER DEFAULT 0,
        checked_at      REAL NOT NULL
    );
    """

    def __init__(self, db_path):
        """Initialize SQLite store.

        @param db_path: path to SQLite database file
        """
        self.db_path = db_path
        self._write_lock = threading.Lock()
        self._local = threading.local()
        self._connections = []
        self._connections_lock = threading.Lock()
        self._closed = False
        self._init_db()

    def _get_connection(self):
        """Get or create a thread-local database connection."""
        if self._closed:
            raise RuntimeError("SqliteStore is closed")
        if not hasattr(self._local, 'conn') or self._local.conn is None:
            conn = sqlite3.connect(self.db_path, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA cache_size=-8000")  # 8MB cache
            conn.execute("PRAGMA busy_timeout=5000")
            self._local.conn = conn
            with self._connections_lock:
                self._connections.append(conn)
        return self._local.conn

    def _init_db(self):
        """Create tables if they don't exist and store schema version."""
        conn = self._get_connection()
        conn.executescript(self.CREATE_TABLES)
        conn.execute(
            "INSERT OR REPLACE INTO run_metadata (key, value) VALUES (?, ?)",
            ("schema_version", str(self.SCHEMA_VERSION)),
        )
        conn.commit()

    # ==================== Metadata Operations ====================

    def set_metadata(self, key, value):
        """Store a key-value pair in run metadata.

        @param key: metadata key
        @param value: any JSON-serializable value
        """
        with self._write_lock:
            conn = self._get_connection()
            conn.execute(
                "INSERT OR REPLACE INTO run_metadata (key, value) VALUES (?, ?)",
                (key, json.dumps(value)),
            )
            conn.commit()

    def get_metadata(self, key, default=None):
        """Retrieve a metadata value by key.

        @param key: metadata key
        @param default: default value if key not found
        @return: deserialized value or default
        """
        conn = self._get_connection()
        row = conn.execute(
            "SELECT value FROM run_metadata WHERE key = ?", (key,)
        ).fetchone()
        if row:
            return json.loads(row[0])
        return default

    # ==================== URL Queue Operations ====================

    def enqueue_url(self, url_info):
        """Add a single URL to the persistent queue.

        @param url_info: dict with url, cache_url, parent_url, etc.
        @return: True if added, False if duplicate cache_url
        """
        with self._write_lock:
            conn = self._get_connection()
            try:
                conn.execute(
                    """INSERT INTO url_queue
                    (url, cache_url, parent_url, base_ref, recursion_level,
                     line, column_num, page, name, extern, url_encoding,
                     parent_content_type, status, created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'pending',?)""",
                    (
                        url_info.get('url', ''),
                        url_info.get('cache_url'),
                        url_info.get('parent_url', ''),
                        url_info.get('base_ref', ''),
                        url_info.get('recursion_level', 0),
                        url_info.get('line', 0),
                        url_info.get('column', 0),
                        url_info.get('page', 0),
                        url_info.get('name', ''),
                        json.dumps(url_info.get('extern', '')),
                        url_info.get('url_encoding', ''),
                        url_info.get('parent_content_type', ''),
                        time.time(),
                    ),
                )
                conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def enqueue_urls_batch(self, url_infos):
        """Batch add URLs to queue within a single transaction.

        @param url_infos: list of url_info dicts
        @return: count of actually added URLs
        """
        added = 0
        with self._write_lock:
            conn = self._get_connection()
            for info in url_infos:
                try:
                    conn.execute(
                        """INSERT INTO url_queue
                        (url, cache_url, parent_url, base_ref, recursion_level,
                         line, column_num, page, name, extern, url_encoding,
                         parent_content_type, status, created_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'pending',?)""",
                        (
                            info.get('url', ''),
                            info.get('cache_url'),
                            info.get('parent_url', ''),
                            info.get('base_ref', ''),
                            info.get('recursion_level', 0),
                            info.get('line', 0),
                            info.get('column', 0),
                            info.get('page', 0),
                            info.get('name', ''),
                            json.dumps(info.get('extern', '')),
                            info.get('url_encoding', ''),
                            info.get('parent_content_type', ''),
                            time.time(),
                        ),
                    )
                    added += 1
                except sqlite3.IntegrityError:
                    pass
            conn.commit()
        return added

    def dequeue_urls(self, batch_size=100):
        """Get a batch of pending URLs and mark them as in_progress.

        @param batch_size: maximum number of URLs to dequeue
        @return: list of sqlite3.Row objects
        """
        with self._write_lock:
            conn = self._get_connection()
            rows = conn.execute(
                "SELECT * FROM url_queue WHERE status = 'pending' "
                "ORDER BY id ASC LIMIT ?",
                (batch_size,),
            ).fetchall()
            if rows:
                ids = [r['id'] for r in rows]
                placeholders = ','.join('?' * len(ids))
                conn.execute(
                    f"UPDATE url_queue SET status = 'in_progress', "
                    f"updated_at = ? WHERE id IN ({placeholders})",
                    [time.time()] + ids,
                )
                conn.commit()
            return rows

    def mark_url_done(self, queue_id):
        """Mark a URL queue entry as done.

        @param queue_id: the url_queue.id value
        """
        with self._write_lock:
            conn = self._get_connection()
            conn.execute(
                "UPDATE url_queue SET status = 'done', updated_at = ? "
                "WHERE id = ?",
                (time.time(), queue_id),
            )
            conn.commit()

    def reset_in_progress(self):
        """Reset all in_progress URLs back to pending for resume.

        Also deletes corresponding placeholder entries (valid=-1) from
        check_results to prevent them from blocking re-checking.

        @return: number of URLs reset
        """
        with self._write_lock:
            conn = self._get_connection()
            # Delete check_results placeholders for in_progress URLs
            conn.execute(
                """DELETE FROM check_results
                WHERE valid = -1 AND cache_url IN (
                    SELECT cache_url FROM url_queue
                    WHERE status = 'in_progress'
                )"""
            )
            # Reset url_queue status
            count = conn.execute(
                "UPDATE url_queue SET status = 'pending', updated_at = ? "
                "WHERE status = 'in_progress'",
                (time.time(),),
            ).rowcount
            conn.commit()
            log.debug(
                LOG_CACHE,
                "Reset %d in-progress URLs to pending, "
                "cleared corresponding result placeholders",
                count,
            )
            return count

    def has_pending_urls(self):
        """Check if there are any pending or in_progress URLs.

        @return: True if pending work exists
        """
        conn = self._get_connection()
        row = conn.execute(
            "SELECT COUNT(*) FROM url_queue "
            "WHERE status IN ('pending', 'in_progress')"
        ).fetchone()
        return row[0] > 0

    def get_queue_stats(self):
        """Get queue statistics by status.

        @return: dict with status counts
        """
        conn = self._get_connection()
        stats = {}
        for status in ('pending', 'in_progress', 'done', 'skipped'):
            row = conn.execute(
                "SELECT COUNT(*) FROM url_queue WHERE status = ?", (status,)
            ).fetchone()
            stats[status] = row[0]
        return stats

    # ==================== Result Cache Operations ====================

    @staticmethod
    def _serialize_modified(modified):
        """Serialize a datetime or None to ISO format string.

        @param modified: datetime object or None
        @return: ISO format string or None
        """
        if modified is None:
            return None
        if isinstance(modified, datetime):
            return modified.isoformat()
        return str(modified)

    @staticmethod
    def _deserialize_modified(value):
        """Deserialize an ISO format string back to datetime.

        @param value: ISO format string or None
        @return: datetime object or None
        """
        if value is None:
            return None
        try:
            return datetime.fromisoformat(value)
        except (ValueError, TypeError):
            return None

    def add_result(self, cache_url, wire_dict):
        """Add a check result to the cache.

        @param cache_url: the cache key
        @param wire_dict: dict from to_wire_dict() or None for placeholder
        """
        with self._write_lock:
            conn = self._get_connection()
            if wire_dict is None:
                # Placeholder to prevent duplicate checking
                try:
                    conn.execute(
                        "INSERT INTO check_results "
                        "(cache_url, url, checked_at, valid, result) "
                        "VALUES (?, '', ?, -1, 'pending')",
                        (cache_url, time.time()),
                    )
                    conn.commit()
                except sqlite3.IntegrityError:
                    # Already exists, skip
                    pass
                return
            try:
                conn.execute(
                    """INSERT OR REPLACE INTO check_results
                    (cache_url, url, valid, extern, result, warnings, info,
                     name, title, parent_url, base_ref, base_url, domain,
                     content_type, size, modified, dltime, checktime,
                     line, column_num, page, level, checked_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        cache_url,
                        wire_dict.get('url', ''),
                        1 if wire_dict.get('valid', True) else 0,
                        1 if wire_dict.get('extern', False) else 0,
                        wire_dict.get('result', ''),
                        json.dumps(wire_dict.get('warnings', [])),
                        json.dumps(wire_dict.get('info', [])),
                        wire_dict.get('name', ''),
                        wire_dict.get('title', ''),
                        wire_dict.get('parent_url', ''),
                        wire_dict.get('base_ref', ''),
                        wire_dict.get('base_url', ''),
                        wire_dict.get('domain', ''),
                        wire_dict.get('content_type', ''),
                        wire_dict.get('size', -1),
                        self._serialize_modified(wire_dict.get('modified')),
                        wire_dict.get('dltime', -1),
                        wire_dict.get('checktime', 0),
                        wire_dict.get('line', 0),
                        wire_dict.get('column', 0),
                        wire_dict.get('page', 0),
                        wire_dict.get('level', 0),
                        time.time(),
                    ),
                )
                conn.commit()
            except sqlite3.IntegrityError:
                pass

    def has_result(self, cache_url):
        """Check if a result exists for the given cache_url.

        @param cache_url: cache key to check
        @return: True if any entry exists (including placeholders)
        """
        conn = self._get_connection()
        row = conn.execute(
            "SELECT 1 FROM check_results WHERE cache_url = ?", (cache_url,)
        ).fetchone()
        return row is not None

    def get_result(self, cache_url):
        """Get cached result as a wire_dict.

        @param cache_url: cache key
        @return: wire_dict compatible with CompactUrlData, or None
        """
        conn = self._get_connection()
        row = conn.execute(
            "SELECT * FROM check_results WHERE cache_url = ?", (cache_url,)
        ).fetchone()
        if row is None:
            return None
        return self._row_to_wire_dict(row)

    def get_result_count(self):
        """Get count of cached results, excluding placeholders.

        @return: number of completed check results
        """
        conn = self._get_connection()
        row = conn.execute(
            "SELECT COUNT(*) FROM check_results WHERE valid != -1"
        ).fetchone()
        return row[0]

    def _row_to_wire_dict(self, row):
        """Convert a sqlite3.Row from check_results to a wire_dict.

        Returns None for placeholder rows (valid == -1).
        Properly deserializes JSON fields, datetime, and tuple warnings.

        @param row: sqlite3.Row object
        @return: dict compatible with CompactUrlData constructor, or None
        """
        if row['valid'] == -1:
            return None
        warnings_raw = json.loads(row['warnings'])
        return {
            'url': row['url'],
            'valid': bool(row['valid']),
            'extern': bool(row['extern']),
            'result': row['result'],
            'warnings': [tuple(w) for w in warnings_raw],
            'info': json.loads(row['info']),
            'name': row['name'],
            'title': row['title'],
            'parent_url': row['parent_url'],
            'base_ref': row['base_ref'],
            'base_url': row['base_url'],
            'domain': row['domain'],
            'content_type': row['content_type'],
            'size': row['size'],
            'modified': self._deserialize_modified(row['modified']),
            'dltime': row['dltime'],
            'checktime': row['checktime'],
            'line': row['line'],
            'column': row['column_num'],
            'page': row['page'],
            'cache_url': row['cache_url'],
            'level': row['level'],
        }

    # ==================== Lifecycle ====================

    def close(self):
        """Close all thread-local connections."""
        self._closed = True
        with self._connections_lock:
            for conn in self._connections:
                try:
                    conn.close()
                except Exception:
                    pass
            self._connections.clear()
        if hasattr(self._local, 'conn'):
            self._local.conn = None

    def delete_db(self):
        """Close connections and delete the database file."""
        self.close()
        for suffix in ('', '-wal', '-shm'):
            path = self.db_path + suffix
            if os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    log.debug(LOG_CACHE, "Failed to remove %s", path)

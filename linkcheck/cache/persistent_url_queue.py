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
Persistent URL queue with memory buffer and SQLite overflow.

Uses a hybrid strategy: URLs are kept in an in-memory deque up to
MEMORY_BUFFER_SIZE. When the buffer is full, new URLs overflow to
SQLite. When the memory buffer empties, URLs are batch-loaded from
SQLite.

Key design decisions (from v2 review):
- S1: do_shutdown() persists memory queue before clearing
- S2: do_shutdown() flushes overflow buffer before clearing
- P4: _load_from_sqlite() skips URLs with completed results
- I1: _overflow_to_sqlite() stores parent_content_type
"""

import collections
import threading
import time

from .. import log, LOG_CACHE


class Timeout(Exception):
    """Raised when a join() operation times out."""
    pass


class Empty(Exception):
    """Raised when a get() operation times out on an empty queue."""
    pass


# Memory buffer size (URLs kept in deque before overflowing to SQLite)
MEMORY_BUFFER_SIZE = 5000
# Batch size when loading from SQLite into memory
BATCH_LOAD_SIZE = 500
# Flush overflow buffer to SQLite every N items
OVERFLOW_CHECK_INTERVAL = 100


class PersistentUrlQueue:
    """URL queue with SQLite overflow storage.

    Provides the same public interface as UrlQueue:
    - put(item)
    - get(timeout=None)
    - task_done(url_data)
    - join(timeout=None)
    - do_shutdown()
    - status() -> (finished, in_progress, queued)
    - qsize() -> int
    - empty() -> bool
    """

    def __init__(self, sqlite_store, max_allowed_urls=None,
                 buffer_size=MEMORY_BUFFER_SIZE):
        """Initialize persistent URL queue.

        @param sqlite_store: SqliteStore instance
        @param max_allowed_urls: maximum URLs to check, or None
        @param buffer_size: max URLs in memory buffer
        """
        self.store = sqlite_store
        self.queue = collections.deque()
        self.buffer_size = buffer_size

        self.mutex = threading.Lock()
        self.not_empty = threading.Condition(self.mutex)
        self.all_tasks_done = threading.Condition(self.mutex)

        self.unfinished_tasks = 0
        self.finished_tasks = 0
        self.in_progress = 0
        self.shutdown = False

        self.max_allowed_urls = max_allowed_urls
        if max_allowed_urls is not None and max_allowed_urls <= 0:
            raise ValueError(
                "Non-positive number of allowed URLs: %d" % max_allowed_urls
            )

        self._overflow_buffer = []
        self._sqlite_pending = 0
        self._aggregate = None

    def set_aggregate(self, aggregate):
        """Set aggregate reference for URL rebuilding from SQLite.

        Must be called after Aggregate is fully initialized.

        @param aggregate: Aggregate instance
        """
        self._aggregate = aggregate

    # ==================== Queue Size & State ====================

    def qsize(self):
        """Return total number of queued URLs (memory + SQLite + overflow buffer)."""
        with self.mutex:
            return len(self.queue) + self._sqlite_pending + len(self._overflow_buffer)

    def empty(self):
        """Return True if no URLs are queued."""
        with self.mutex:
            return self._empty()

    def _empty(self):
        """Internal empty check. Caller must hold self.mutex."""
        return (not self.queue
                and self._sqlite_pending == 0
                and not self._overflow_buffer)

    # ==================== Get (Consumer) ====================

    def get(self, timeout=None):
        """Remove and return a URL from the queue.

        Blocks until a URL is available or timeout expires.

        @param timeout: seconds to wait, or None for indefinite
        @return: url_data object
        @raises Empty: if timeout expires
        """
        with self.not_empty:
            return self._get(timeout)

    def _get(self, timeout):
        """Internal get with wait logic. Caller must hold self.not_empty."""
        if timeout is None:
            while self._empty():
                self.not_empty.wait()
        else:
            if timeout < 0:
                raise ValueError("'timeout' must be a positive number")
            endtime = time.time() + timeout
            while self._empty():
                remaining = endtime - time.time()
                if remaining <= 0.0:
                    raise Empty()
                self.not_empty.wait(remaining)

        # If memory buffer is empty, flush overflow or load from SQLite
        if not self.queue:
            if self._overflow_buffer:
                self._flush_overflow()
            if self._sqlite_pending > 0:
                self._load_from_sqlite()

        self.in_progress += 1
        return self.queue.popleft()

    def _load_from_sqlite(self):
        """Load a batch of URLs from SQLite into the memory buffer.

        Skips URLs that already have completed results in the cache (P4).
        Caller must hold self.mutex.
        """
        rows = self.store.dequeue_urls(batch_size=BATCH_LOAD_SIZE)
        for row in rows:
            # P4: Skip URLs with existing complete results
            if self._aggregate is not None:
                cache = self._aggregate.result_cache
                cache_url = row['cache_url']
                if cache_url and cache.has_non_empty_result(cache_url):
                    self.store.mark_url_done(row['id'])
                    self._sqlite_pending -= 1
                    self.unfinished_tasks -= 1
                    log.debug(
                        LOG_CACHE,
                        "Skipping already-checked URL from SQLite: %s",
                        row['url'],
                    )
                    continue

            url_data = self._row_to_queue_item(row)
            if url_data is not None:
                self.queue.append(url_data)
            else:
                log.debug(
                    LOG_CACHE,
                    "Failed to rebuild URL from SQLite row: %s",
                    row['url'],
                )
            self._sqlite_pending -= 1

    def _row_to_queue_item(self, row):
        """Convert a SQLite row to a UrlData object.

        @param row: sqlite3.Row from url_queue table
        @return: UrlData instance or None if rebuild fails
        """
        if self._aggregate is None:
            return None
        try:
            from .url_rebuilder import rebuild_url_data
            return rebuild_url_data(row, self._aggregate)
        except Exception as e:
            log.debug(LOG_CACHE, "URL rebuild failed: %s", str(e))
            return None

    # ==================== Put (Producer) ====================

    def put(self, item):
        """Add a URL to the queue.

        Thread-safe. If the memory buffer is full, the URL overflows
        to SQLite storage.

        @param item: url_data object
        """
        with self.mutex:
            self._put(item)
            self.not_empty.notify()

    def _put(self, url_data):
        """Internal put logic. Caller must hold self.mutex."""
        if self.shutdown or self.max_allowed_urls == 0:
            return

        key = url_data.cache_url
        cache = url_data.aggregate.result_cache
        if cache.has_result(key):
            log.debug(
                LOG_CACHE,
                "skipping %s, %s already cached",
                url_data.url, key,
            )
            return

        log.debug(LOG_CACHE, "queueing %s", url_data.url)

        if url_data.has_result:
            # URL with existing result gets priority (prepend)
            self.queue.appendleft(url_data)
        else:
            assert key is not None, "no result for None key: %s" % url_data
            if self.max_allowed_urls is not None:
                self.max_allowed_urls -= 1

            # Decide: memory buffer or SQLite overflow
            if len(self.queue) < self.buffer_size:
                self.queue.append(url_data)
            else:
                self._overflow_to_sqlite(url_data)

        self.unfinished_tasks += 1
        cache.add_result(key, None)

    def _overflow_to_sqlite(self, url_data):
        """Serialize url_data to overflow buffer for SQLite storage.

        Includes parent_content_type (I1 fix).
        """
        url_info = {
            'url': url_data.base_url or '',
            'cache_url': url_data.cache_url,
            'parent_url': url_data.parent_url or '',
            'base_ref': url_data.base_ref or '',
            'recursion_level': url_data.recursion_level,
            'line': url_data.line or 0,
            'column': url_data.column or 0,
            'page': url_data.page,
            'name': url_data.name or '',
            'extern': list(url_data.extern) if url_data.extern else '',
            'url_encoding': getattr(url_data, 'content_encoding', '') or '',
            'parent_content_type': getattr(
                url_data, 'parent_content_type', ''
            ) or '',
        }
        self._overflow_buffer.append(url_info)

        # Batch flush when buffer is large enough
        if len(self._overflow_buffer) >= OVERFLOW_CHECK_INTERVAL:
            self._flush_overflow()

    def _flush_overflow(self):
        """Flush overflow buffer to SQLite in a batch.

        Caller must hold self.mutex (or be called during shutdown).
        """
        if self._overflow_buffer:
            added = self.store.enqueue_urls_batch(self._overflow_buffer)
            self._sqlite_pending += added
            self._overflow_buffer.clear()
            log.debug(LOG_CACHE, "Flushed %d URLs to SQLite", added)

    def _persist_memory_queue(self):
        """Persist all URLs currently in the memory queue to SQLite.

        Used during shutdown to ensure no URLs are lost (S1 fix).
        Caller must hold self.mutex.
        """
        persisted = 0
        for url_data in self.queue:
            if not url_data.has_result:
                self._overflow_to_sqlite(url_data)
                persisted += 1
        self._flush_overflow()
        if persisted:
            log.debug(
                LOG_CACHE,
                "Persisted %d in-memory URLs to SQLite on shutdown",
                persisted,
            )

    # ==================== Task Tracking ====================

    def task_done(self, url_data):
        """Signal that a queued URL has been fully processed.

        @param url_data: the url_data that was processed
        """
        with self.all_tasks_done:
            log.debug(LOG_CACHE, "task_done %s", url_data.url)
            self.finished_tasks += 1
            self.unfinished_tasks -= 1
            self.in_progress -= 1

            # Mark done in SQLite if this URL came from there
            if hasattr(url_data, '_sqlite_queue_id'):
                self.store.mark_url_done(url_data._sqlite_queue_id)

            if self.unfinished_tasks <= 0:
                if self.unfinished_tasks < 0:
                    raise ValueError('task_done() called too many times')
                self.all_tasks_done.notify_all()

    def join(self, timeout=None):
        """Block until all queued URLs have been processed.

        @param timeout: seconds to wait, or None for indefinite
        @raises Timeout: if timeout expires with unfinished tasks
        """
        with self.all_tasks_done:
            if timeout is None:
                while self.unfinished_tasks:
                    self.all_tasks_done.wait()
            else:
                if timeout < 0:
                    raise ValueError("'timeout' must be a positive number")
                endtime = time.time() + timeout
                while self.unfinished_tasks:
                    remaining = endtime - time.time()
                    if remaining <= 0.0:
                        raise Timeout()
                    self.all_tasks_done.wait(remaining)

    # ==================== Shutdown ====================

    def do_shutdown(self):
        """Shut down the queue.

        Persists all in-memory URLs to SQLite before clearing (S1/S2 fix),
        so they can be resumed later.
        """
        with self.mutex:
            # S2: Flush any pending overflow buffer
            self._flush_overflow()
            # S1: Persist memory queue URLs to SQLite
            self._persist_memory_queue()

            # After _persist_memory_queue, URLs exist in both self.queue
            # and _sqlite_pending. Clear queue first to avoid double-counting.
            self.queue.clear()

            # unfinished = tasks still being processed by checker threads
            unfinished = self.unfinished_tasks - self._sqlite_pending
            self._sqlite_pending = 0

            if unfinished <= 0:
                if unfinished < 0:
                    raise ValueError('shutdown is in error')
                self.all_tasks_done.notify_all()
            self.unfinished_tasks = unfinished
            self.shutdown = True

    # ==================== Status ====================

    def status(self):
        """Return queue status tuple.

        @return: (finished_tasks, in_progress, queued)
        """
        return (
            self.finished_tasks,
            self.in_progress,
            len(self.queue) + self._sqlite_pending + len(self._overflow_buffer),
        )

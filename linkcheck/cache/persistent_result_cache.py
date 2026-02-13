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
Persistent result cache backed by SQLite with an in-memory LRU layer.

Provides the same interface as ResultCache but uses SQLite for storage,
enabling handling of datasets larger than available memory.

Key design decisions (from v2 review):
- C1: get_result() returns CompactUrlData, not dict
- C2: add_result() extracts dict from CompactUrlData via __slots__
- P1: _known_keys set for O(1) has_result() checks
- P2: _result_count counter avoids COUNT(*) queries
"""

from collections import OrderedDict

from ..checker.urlbase import CompactUrlData, urlDataAttr
from ..decorators import synchronized
from ..lock import get_lock

cache_lock = get_lock("persistent_cache_lock")


class PersistentResultCache:
    """Thread-safe result cache with SQLite backend.

    Provides the same interface as ResultCache:
    - get_result(key) -> CompactUrlData or None
    - add_result(key, result)
    - has_result(key) -> bool
    - has_non_empty_result(key) -> CompactUrlData or None
    - __len__() -> int
    """

    def __init__(self, sqlite_store, memory_cache_size=10000):
        """Initialize persistent result cache.

        @param sqlite_store: SqliteStore instance
        @param memory_cache_size: max entries in LRU memory cache
        """
        self.store = sqlite_store
        self.memory_cache_size = memory_cache_size
        self._lru = OrderedDict()
        self._known_keys = set()
        self._result_count = self.store.get_result_count()

    @synchronized(cache_lock)
    def get_result(self, key):
        """Return cached result as CompactUrlData or None.

        Checks LRU cache first, falls back to SQLite.
        """
        if key is None:
            return None
        # Check LRU memory cache first
        if key in self._lru:
            self._lru.move_to_end(key)
            return self._lru[key]
        # Fall back to SQLite
        wire_dict = self.store.get_result(key)
        if wire_dict is not None:
            result = CompactUrlData(wire_dict)
            self._lru_put(key, result)
            return result
        return None

    @synchronized(cache_lock)
    def add_result(self, key, result):
        """Add result object to cache with given key.

        Handles three input types:
        - None: placeholder to prevent duplicate checking
        - CompactUrlData: extracted via __slots__ attributes
        - dict: used directly as wire_dict
        """
        if key is None:
            return
        self._known_keys.add(key)

        if result is None:
            # Placeholder entry
            self.store.add_result(key, None)
            self._lru_put(key, None)
        else:
            # Extract wire_dict from CompactUrlData or use dict directly
            if hasattr(result, '__slots__'):
                wire_dict = {attr: getattr(result, attr) for attr in urlDataAttr}
                compact = result
            elif isinstance(result, dict):
                wire_dict = result
                compact = CompactUrlData(result)
            else:
                wire_dict = result.to_wire_dict()
                compact = CompactUrlData(wire_dict)
            self.store.add_result(key, wire_dict)
            # Always store CompactUrlData in LRU for type consistency
            self._lru_put(key, compact)
            # TODO: If add_result is ever called multiple times with a
            # non-None result for the same key (e.g. retry/update), this
            # counter will be inflated. Current LinkChecker flow guarantees
            # each URL gets add_result(key, real_result) exactly once,
            # so this is safe for now. If that invariant changes, consider
            # checking has_non_empty_result(key) before incrementing, or
            # having store.add_result() return an is_new flag.
            self._result_count += 1

    def has_result(self, key):
        """Fast O(1) containment check using in-memory key set.

        Not thread-safe, consistent with ResultCache.has_result().
        """
        return key in self._known_keys

    def has_non_empty_result(self, key):
        """Check if key has a non-placeholder result.

        Not thread-safe, consistent with ResultCache.has_non_empty_result().

        @return: CompactUrlData if a real result exists, None otherwise
        """
        if key in self._lru:
            return self._lru[key]
        wire_dict = self.store.get_result(key)
        if wire_dict is not None:
            return CompactUrlData(wire_dict)
        return None

    def __len__(self):
        """Get number of completed results (excluding placeholders).

        Uses in-memory counter for performance.
        """
        return self._result_count

    def _lru_put(self, key, value):
        """Put value into LRU cache, evicting oldest if full."""
        if key in self._lru:
            self._lru.move_to_end(key)
        else:
            if len(self._lru) >= self.memory_cache_size:
                self._lru.popitem(last=False)
        self._lru[key] = value

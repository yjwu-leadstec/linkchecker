# Copyright (C) 2024 LinkChecker Authors
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
"""Tests for persistent result cache."""

import pytest

from linkcheck.cache.sqlite_store import SqliteStore
from linkcheck.cache.persistent_result_cache import PersistentResultCache
from linkcheck.checker.urlbase import CompactUrlData, urlDataAttr


@pytest.fixture
def store(tmp_path):
    s = SqliteStore(str(tmp_path / "test.db"))
    yield s
    s.delete_db()


@pytest.fixture
def cache(store):
    return PersistentResultCache(store, memory_cache_size=5)


def _make_wire_dict(url='http://example.com/', **kwargs):
    wire = {
        'url': url, 'valid': True, 'extern': False,
        'result': 'Valid: 200 OK',
        'warnings': [], 'info': [],
        'name': '', 'title': '',
        'parent_url': '', 'base_ref': '',
        'base_url': url, 'domain': 'example.com',
        'content_type': 'text/html', 'size': 100,
        'modified': None, 'dltime': 0.1,
        'checktime': 0.5, 'line': 0, 'column': 0,
        'page': 0, 'cache_url': url, 'level': 0,
    }
    wire.update(kwargs)
    return wire


class TestPersistentResultCache:
    """Tests for PersistentResultCache interface compatibility."""

    def test_add_and_get_placeholder(self, cache):
        cache.add_result('http://a.com/', None)
        assert cache.has_result('http://a.com/') is True
        # get_result on placeholder returns None
        assert cache.get_result('http://a.com/') is None

    def test_add_and_get_result_returns_compact_url_data(self, cache):
        """Verify C1 fix: get_result returns CompactUrlData."""
        wire = _make_wire_dict()
        compact = CompactUrlData(wire)
        cache.add_result('http://example.com/', compact)
        result = cache.get_result('http://example.com/')
        assert isinstance(result, CompactUrlData)
        assert result.valid is True
        assert result.url == 'http://example.com/'

    def test_add_compact_url_data_extracts_dict(self, cache):
        """Verify C2 fix: CompactUrlData is properly serialized."""
        wire = _make_wire_dict(url='http://test.com/')
        compact = CompactUrlData(wire)
        cache.add_result('http://test.com/', compact)
        # Read back from SQLite (bypass LRU)
        result_wire = cache.store.get_result('http://test.com/')
        assert result_wire is not None
        assert result_wire['url'] == 'http://test.com/'

    def test_add_dict_directly(self, cache):
        """add_result should also accept a plain dict."""
        wire = _make_wire_dict(url='http://dict.com/')
        cache.add_result('http://dict.com/', wire)
        result = cache.get_result('http://dict.com/')
        assert isinstance(result, CompactUrlData)
        assert result.url == 'http://dict.com/'

    def test_has_result_uses_known_keys(self, cache):
        """Verify P1 fix: has_result uses O(1) set lookup."""
        assert cache.has_result('http://new.com/') is False
        cache.add_result('http://new.com/', None)
        assert cache.has_result('http://new.com/') is True
        # Verify it's in _known_keys set
        assert 'http://new.com/' in cache._known_keys

    def test_has_result_none_key(self, cache):
        assert cache.has_result(None) is False

    def test_get_result_none_key(self, cache):
        assert cache.get_result(None) is None

    def test_len_counts_real_results_only(self, cache):
        """Verify P2 fix: __len__ uses memory counter."""
        cache.add_result('http://a.com/', None)  # placeholder
        assert len(cache) == 0
        wire = _make_wire_dict(url='http://b.com/')
        cache.add_result('http://b.com/', CompactUrlData(wire))
        assert len(cache) == 1

    def test_lru_eviction(self, cache):
        """Test LRU eviction when cache is full (size=5)."""
        for i in range(10):
            url = f'http://example.com/{i}'
            wire = _make_wire_dict(url=url)
            cache.add_result(url, CompactUrlData(wire))

        # LRU should only have last 5 entries
        assert len(cache._lru) == 5

        # But all 10 should be retrievable (from SQLite fallback)
        for i in range(10):
            url = f'http://example.com/{i}'
            result = cache.get_result(url)
            assert result is not None
            assert result.url == url

    def test_lru_hit_moves_to_end(self, cache):
        for i in range(3):
            url = f'http://example.com/{i}'
            wire = _make_wire_dict(url=url)
            cache.add_result(url, CompactUrlData(wire))

        # Access first entry to move it to end
        cache.get_result('http://example.com/0')
        keys = list(cache._lru.keys())
        assert keys[-1] == 'http://example.com/0'

    def test_has_non_empty_result(self, cache):
        cache.add_result('http://a.com/', None)
        result = cache.has_non_empty_result('http://a.com/')
        assert result is None  # placeholder

        wire = _make_wire_dict(url='http://b.com/')
        cache.add_result('http://b.com/', CompactUrlData(wire))
        result = cache.has_non_empty_result('http://b.com/')
        assert result is not None

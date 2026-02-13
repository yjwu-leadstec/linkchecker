# Copyright (C) 2024 LinkChecker Authors
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
"""Tests for SQLite persistent storage."""

import json
import os
import tempfile
import threading
import time
from datetime import datetime, timezone

import pytest

from linkcheck.cache.sqlite_store import SqliteStore


@pytest.fixture
def db_path(tmp_path):
    """Provide a temporary database path."""
    return str(tmp_path / "test.db")


@pytest.fixture
def store(db_path):
    """Provide a initialized SqliteStore, cleaned up after test."""
    s = SqliteStore(db_path)
    yield s
    s.delete_db()


class TestSqliteStoreInit:
    """Tests for database initialization."""

    def test_creates_database_file(self, db_path):
        store = SqliteStore(db_path)
        assert os.path.exists(db_path)
        store.close()

    def test_creates_tables(self, store):
        conn = store._get_connection()
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_names = {t[0] for t in tables}
        assert 'run_metadata' in table_names
        assert 'url_queue' in table_names
        assert 'check_results' in table_names

    def test_schema_version_stored(self, store):
        version = store.get_metadata('schema_version')
        # schema_version is stored as string, not via JSON metadata
        conn = store._get_connection()
        row = conn.execute(
            "SELECT value FROM run_metadata WHERE key='schema_version'"
        ).fetchone()
        assert row[0] == str(SqliteStore.SCHEMA_VERSION)

    def test_idempotent_init(self, db_path):
        """Creating SqliteStore twice on same DB should not fail."""
        store1 = SqliteStore(db_path)
        store2 = SqliteStore(db_path)
        store1.close()
        store2.close()


class TestMetadata:
    """Tests for metadata operations."""

    def test_set_and_get(self, store):
        store.set_metadata('key1', 'value1')
        assert store.get_metadata('key1') == 'value1'

    def test_get_default(self, store):
        assert store.get_metadata('nonexistent') is None
        assert store.get_metadata('nonexistent', 'default') == 'default'

    def test_set_complex_value(self, store):
        data = {'list': [1, 2, 3], 'nested': {'a': True}}
        store.set_metadata('complex', data)
        assert store.get_metadata('complex') == data

    def test_overwrite(self, store):
        store.set_metadata('key', 'old')
        store.set_metadata('key', 'new')
        assert store.get_metadata('key') == 'new'


class TestUrlQueue:
    """Tests for URL queue operations."""

    def _make_url_info(self, url, cache_url=None, **kwargs):
        info = {
            'url': url,
            'cache_url': cache_url or url,
            'parent_url': '',
            'base_ref': '',
            'recursion_level': 0,
            'line': 0,
            'column': 0,
            'page': 0,
            'name': '',
            'extern': '',
            'url_encoding': '',
            'parent_content_type': '',
        }
        info.update(kwargs)
        return info

    def test_enqueue_and_dequeue(self, store):
        info = self._make_url_info('http://example.com/')
        assert store.enqueue_url(info) is True
        rows = store.dequeue_urls(10)
        assert len(rows) == 1
        assert rows[0]['url'] == 'http://example.com/'
        # After dequeue, the URL should be marked in_progress in the DB
        stats = store.get_queue_stats()
        assert stats['in_progress'] == 1
        assert stats['pending'] == 0

    def test_enqueue_duplicate(self, store):
        info = self._make_url_info('http://example.com/')
        assert store.enqueue_url(info) is True
        assert store.enqueue_url(info) is False

    def test_enqueue_batch(self, store):
        infos = [self._make_url_info(f'http://example.com/{i}') for i in range(5)]
        added = store.enqueue_urls_batch(infos)
        assert added == 5

    def test_enqueue_batch_with_duplicates(self, store):
        infos = [
            self._make_url_info('http://example.com/1'),
            self._make_url_info('http://example.com/1'),  # duplicate
            self._make_url_info('http://example.com/2'),
        ]
        added = store.enqueue_urls_batch(infos)
        assert added == 2

    def test_dequeue_marks_in_progress(self, store):
        store.enqueue_url(self._make_url_info('http://example.com/'))
        rows = store.dequeue_urls(1)
        assert len(rows) == 1

        # Dequeue again should return empty (already in_progress)
        rows2 = store.dequeue_urls(1)
        assert len(rows2) == 0

    def test_dequeue_batch_size(self, store):
        for i in range(10):
            store.enqueue_url(self._make_url_info(f'http://example.com/{i}'))
        rows = store.dequeue_urls(batch_size=3)
        assert len(rows) == 3

    def test_mark_done(self, store):
        store.enqueue_url(self._make_url_info('http://example.com/'))
        rows = store.dequeue_urls(1)
        store.mark_url_done(rows[0]['id'])
        stats = store.get_queue_stats()
        assert stats['done'] == 1
        assert stats['pending'] == 0

    def test_has_pending_urls(self, store):
        assert store.has_pending_urls() is False
        store.enqueue_url(self._make_url_info('http://example.com/'))
        assert store.has_pending_urls() is True

    def test_get_queue_stats(self, store):
        for i in range(5):
            store.enqueue_url(self._make_url_info(f'http://example.com/{i}'))
        store.dequeue_urls(2)  # 2 in_progress
        stats = store.get_queue_stats()
        assert stats['pending'] == 3
        assert stats['in_progress'] == 2
        assert stats['done'] == 0

    def test_parent_content_type_stored(self, store):
        info = self._make_url_info(
            'http://example.com/',
            parent_content_type='text/html',
        )
        store.enqueue_url(info)
        rows = store.dequeue_urls(1)
        assert rows[0]['parent_content_type'] == 'text/html'

    def test_reset_in_progress(self, store):
        store.enqueue_url(self._make_url_info('http://example.com/1'))
        store.enqueue_url(self._make_url_info('http://example.com/2'))
        store.dequeue_urls(2)  # both in_progress
        count = store.reset_in_progress()
        assert count == 2
        stats = store.get_queue_stats()
        assert stats['pending'] == 2
        assert stats['in_progress'] == 0

    def test_reset_in_progress_clears_placeholders(self, store):
        """Verify C4 fix: reset clears check_results placeholders."""
        info = self._make_url_info('http://example.com/test')
        store.enqueue_url(info)
        store.dequeue_urls(1)  # mark in_progress
        store.add_result('http://example.com/test', None)  # add placeholder
        assert store.has_result('http://example.com/test') is True

        store.reset_in_progress()
        # Placeholder should be deleted
        assert store.has_result('http://example.com/test') is False


class TestResultCache:
    """Tests for result cache operations."""

    def _make_wire_dict(self, url='http://example.com/', **kwargs):
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

    def test_add_and_get_placeholder(self, store):
        store.add_result('http://example.com/', None)
        assert store.has_result('http://example.com/') is True
        assert store.get_result('http://example.com/') is None

    def test_add_and_get_real_result(self, store):
        wire = self._make_wire_dict()
        store.add_result('http://example.com/', wire)
        result = store.get_result('http://example.com/')
        assert result is not None
        assert result['valid'] is True
        assert result['url'] == 'http://example.com/'

    def test_placeholder_replaced_by_result(self, store):
        store.add_result('http://example.com/', None)
        wire = self._make_wire_dict()
        store.add_result('http://example.com/', wire)
        result = store.get_result('http://example.com/')
        assert result is not None
        assert result['valid'] is True

    def test_datetime_serialization(self, store):
        """Verify C3 fix: modified datetime survives round-trip."""
        dt = datetime(2024, 6, 15, 10, 30, 0, tzinfo=timezone.utc)
        wire = self._make_wire_dict(modified=dt)
        store.add_result('http://example.com/', wire)
        result = store.get_result('http://example.com/')
        assert isinstance(result['modified'], datetime)
        assert result['modified'] == dt

    def test_datetime_none(self, store):
        wire = self._make_wire_dict(modified=None)
        store.add_result('http://example.com/', wire)
        result = store.get_result('http://example.com/')
        assert result['modified'] is None

    def test_warnings_tuple_restoration(self, store):
        """Verify I2 fix: warnings tuples survive round-trip."""
        wire = self._make_wire_dict(
            warnings=[('tag1', 'message1'), ('tag2', 'message2')],
        )
        store.add_result('http://example.com/', wire)
        result = store.get_result('http://example.com/')
        assert result['warnings'] == [('tag1', 'message1'), ('tag2', 'message2')]
        # Verify they are tuples, not lists
        for w in result['warnings']:
            assert isinstance(w, tuple)

    def test_column_num_mapping(self, store):
        """Verify I4 fix: column_num maps to 'column' in wire_dict."""
        wire = self._make_wire_dict(column=42)
        store.add_result('http://example.com/', wire)
        result = store.get_result('http://example.com/')
        assert result['column'] == 42

    def test_get_result_count(self, store):
        store.add_result('http://a.com/', None)  # placeholder, not counted
        store.add_result('http://b.com/', self._make_wire_dict(url='http://b.com/'))
        store.add_result('http://c.com/', self._make_wire_dict(url='http://c.com/'))
        assert store.get_result_count() == 2

    def test_has_result_nonexistent(self, store):
        assert store.has_result('http://nonexistent.com/') is False


class TestConcurrency:
    """Tests for thread safety."""

    def test_concurrent_enqueue(self, store):
        errors = []
        def enqueue_batch(start):
            try:
                for i in range(100):
                    store.enqueue_url({
                        'url': f'http://example.com/{start}_{i}',
                        'cache_url': f'http://example.com/{start}_{i}',
                    })
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=enqueue_batch, args=(t,))
                   for t in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        stats = store.get_queue_stats()
        assert stats['pending'] == 500

    def test_concurrent_add_result(self, store):
        errors = []
        def add_batch(start):
            try:
                for i in range(50):
                    key = f'http://example.com/{start}_{i}'
                    store.add_result(key, {
                        'url': key, 'valid': True, 'extern': False,
                        'result': '', 'warnings': [], 'info': [],
                        'name': '', 'title': '', 'parent_url': '',
                        'base_ref': '', 'base_url': key,
                        'domain': 'example.com', 'content_type': 'text/html',
                        'size': 0, 'modified': None,
                        'dltime': 0, 'checktime': 0,
                        'line': 0, 'column': 0, 'page': 0,
                        'cache_url': key, 'level': 0,
                    })
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=add_batch, args=(t,))
                   for t in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert store.get_result_count() == 250


class TestLifecycle:
    """Tests for database lifecycle management."""

    def test_close(self, store):
        store.close()
        assert store._closed is True

    def test_close_all_connections(self, db_path):
        """Verify P3 fix: all thread connections are closed."""
        store = SqliteStore(db_path)
        # Simulate connections from multiple threads
        connections = []
        def create_conn():
            conn = store._get_connection()
            connections.append(conn)
        threads = [threading.Thread(target=create_conn) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(store._connections) >= 3
        store.close()
        assert len(store._connections) == 0

    def test_delete_db(self, db_path):
        store = SqliteStore(db_path)
        assert os.path.exists(db_path)
        store.delete_db()
        assert not os.path.exists(db_path)

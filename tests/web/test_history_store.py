# Copyright (C) 2024 LinkChecker Authors
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
"""Tests for HistoryStore."""

import pytest

from linkcheck.web.history_store import HistoryStore


class FakeStats:
    number = 3
    errors = 1
    warnings = 2


@pytest.fixture
def store(tmp_path):
    db_path = str(tmp_path / "test_history.db")
    return HistoryStore(db_path=db_path)


@pytest.fixture
def sample_results():
    return [
        {
            "url": "http://example.com",
            "parent_url": "",
            "result": "200 OK",
            "valid": True,
            "warnings": [],
            "checktime": 0.5,
            "size": 1024,
            "content_type": "text/html",
            "level": 0,
        },
        {
            "url": "http://example.com/broken",
            "parent_url": "http://example.com",
            "result": "404 Not Found",
            "valid": False,
            "warnings": [("http-404", "Not Found")],
            "checktime": 0.3,
            "size": 0,
            "content_type": "",
            "level": 1,
        },
        {
            "url": "http://example.com/page",
            "parent_url": "http://example.com",
            "result": "200 OK",
            "valid": True,
            "warnings": [],
            "checktime": 0.2,
            "size": 2048,
            "content_type": "text/html",
            "level": 1,
        },
    ]


class TestHistoryStore:
    def test_save_and_get_sessions(self, store, sample_results):
        session_id = store.save_session(
            urls=["http://example.com"],
            results=sample_results,
            stats=FakeStats(),
            duration=1.0,
        )
        sessions = store.get_sessions()
        assert len(sessions) == 1
        assert sessions[0]["id"] == session_id
        assert sessions[0]["total"] == 3
        assert sessions[0]["errors"] == 1

    def test_get_session_results(self, store, sample_results):
        session_id = store.save_session(
            urls=["http://example.com"],
            results=sample_results,
            stats=FakeStats(),
            duration=1.0,
        )
        results = store.get_session_results(session_id)
        assert len(results) == 3
        assert results[0]["url"] == "http://example.com"
        assert results[1]["valid"] == 0  # SQLite stores as int

    def test_delete_session(self, store, sample_results):
        session_id = store.save_session(
            urls=["http://example.com"],
            results=sample_results,
            stats=FakeStats(),
            duration=1.0,
        )
        store.delete_session(session_id)
        assert len(store.get_sessions()) == 0
        assert len(store.get_session_results(session_id)) == 0

    def test_get_trend_data(self, store, sample_results):
        store.save_session(
            urls=["http://example.com"],
            results=sample_results,
            stats=FakeStats(),
            duration=1.0,
        )
        trends = store.get_trend_data(days=1)
        assert len(trends) == 1
        date_str, errors, total = trends[0]
        assert errors == 1
        assert total == 3

    def test_get_trend_data_with_filter(self, store, sample_results):
        store.save_session(
            urls=["http://example.com"],
            results=sample_results,
            stats=FakeStats(),
            duration=1.0,
        )
        # Matching filter
        trends = store.get_trend_data(url_pattern="example.com", days=1)
        assert len(trends) == 1
        # Non-matching filter
        trends = store.get_trend_data(url_pattern="nonexistent.com", days=1)
        assert len(trends) == 0

    def test_empty_store(self, store):
        assert store.get_sessions() == []
        assert store.get_trend_data() == []

    def test_save_without_stats(self, store, sample_results):
        store.save_session(
            urls=["http://example.com"],
            results=sample_results,
            stats=None,
            duration=2.0,
        )
        sessions = store.get_sessions()
        assert sessions[0]["total"] == 3
        assert sessions[0]["errors"] == 1
        assert sessions[0]["valid"] == 2

    def test_save_without_stats_counts_warnings(self, store, sample_results):
        """Warnings count should be computed from results when stats=None."""
        store.save_session(
            urls=["http://example.com"],
            results=sample_results,
            stats=None,
            duration=1.0,
        )
        sessions = store.get_sessions()
        # sample_results has 1 result with warnings (the 404 one)
        assert sessions[0]["warnings"] == 1

    def test_save_with_duration(self, store, sample_results):
        """Duration should be stored correctly."""
        store.save_session(
            urls=["http://example.com"],
            results=sample_results,
            stats=None,
            duration=42.5,
        )
        sessions = store.get_sessions()
        assert sessions[0]["duration"] == 42.5

    def test_connection_closed_after_operations(self, store, sample_results):
        """Connections should be properly closed after each operation."""
        store.save_session(
            urls=["http://example.com"],
            results=sample_results,
            stats=FakeStats(),
            duration=1.0,
        )
        sessions = store.get_sessions()
        assert len(sessions) == 1
        # Verify we can still do operations (no locked DB)
        store.delete_session(sessions[0]["id"])
        assert len(store.get_sessions()) == 0

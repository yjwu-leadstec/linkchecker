# Copyright (C) 2024 LinkChecker Authors
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
"""Tests for CheckRunner."""

import pytest

from linkcheck.web.check_runner import CheckRunner, _default_cache_db


class TestCheckRunner:
    def test_initial_state(self):
        runner = CheckRunner()
        assert runner.is_running is False
        assert runner.aggregate is None
        assert runner.error is None

    def test_concurrent_check_raises(self):
        runner = CheckRunner()
        # Simulate running state
        runner.is_running = True
        with pytest.raises(RuntimeError, match="already running"):
            runner.run_check(
                urls=["http://example.com"],
                results_list=[],
            )

    def test_cancel_without_aggregate(self):
        runner = CheckRunner()
        # Should not raise when no aggregate exists
        runner.cancel_check()

    def test_pause_without_aggregate(self):
        runner = CheckRunner()
        result = runner.pause_check()
        assert result is None

    def test_default_cache_db(self):
        path = _default_cache_db()
        assert path.endswith("web-cache.db")
        assert ".linkchecker" in path

    def test_build_config(self):
        runner = CheckRunner()
        results = []
        config = runner._build_config(
            overrides={"threads": 5, "timeout": 30},
            results_list=results,
            persist=False,
        )
        assert config["threads"] == 5
        assert config["timeout"] == 30
        assert config["persist"] is False
        # GradioLogger should be in fileoutput
        assert len(config["fileoutput"]) >= 1
        assert config["fileoutput"][0].results_list is results

    def test_build_config_with_persist(self):
        runner = CheckRunner()
        config = runner._build_config(
            overrides={},
            results_list=[],
            persist=True,
        )
        assert config["persist"] is True
        assert config["cache_db"].endswith("web-cache.db")

    def test_build_config_with_resume(self):
        runner = CheckRunner()
        config = runner._build_config(
            overrides={},
            results_list=[],
            persist=True,
            resume=True,
            cache_db="/tmp/test-cache.db",
        )
        assert config["resume"] is True
        assert config["cache_db"] == "/tmp/test-cache.db"

# Copyright (C) 2024 LinkChecker Authors
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
"""Tests for GradioLogger."""

from linkcheck.checker.urlbase import urlDataAttr
from linkcheck.web.gradio_logger import GradioLogger


class FakeUrlData:
    """Minimal fake CompactUrlData with __slots__-like attributes."""

    def __init__(self, **kwargs):
        defaults = {attr: None for attr in urlDataAttr}
        defaults.update({
            "valid": True,
            "url": "http://example.com",
            "parent_url": "",
            "result": "200 OK",
            "warnings": [],
            "info": [],
            "checktime": 0.5,
            "size": 1024,
            "content_type": "text/html",
            "level": 0,
            "extern": 0,
        })
        defaults.update(kwargs)
        for k, v in defaults.items():
            setattr(self, k, v)


class TestGradioLogger:
    def test_logger_name(self):
        assert GradioLogger.LoggerName == "gradio"

    def test_init(self):
        results = []
        logger = GradioLogger(results)
        assert logger.results_list is results

    def test_start_output_initializes_stats(self):
        results = []
        logger = GradioLogger(results)
        logger.start_output()
        assert logger.stats is not None
        assert logger.stats.number == 0
        assert hasattr(logger, 'starttime')

    def test_log_url_appends_dict(self):
        results = []
        logger = GradioLogger(results)
        url_data = FakeUrlData(url="http://test.com", valid=True)
        logger.log_url(url_data)
        assert len(results) == 1
        assert results[0]["url"] == "http://test.com"
        assert results[0]["valid"] is True

    def test_log_url_serializes_warnings(self):
        results = []
        logger = GradioLogger(results)
        url_data = FakeUrlData(
            warnings=[("http-moved-permanent", "Redirected to https")],
        )
        logger.log_url(url_data)
        assert results[0]["warnings"] == [
            ("http-moved-permanent", "Redirected to https")
        ]

    def test_log_filter_url_always_logs(self):
        """log_filter_url should call log_url even when do_print=False."""
        results = []
        logger = GradioLogger(results)
        logger.start_output()
        url_data = FakeUrlData(url="http://valid.com", valid=True)

        # do_print=False: default _Logger would NOT call log_url
        logger.log_filter_url(url_data, do_print=False)
        assert len(results) == 1
        assert results[0]["url"] == "http://valid.com"

    def test_log_filter_url_updates_stats(self):
        results = []
        logger = GradioLogger(results)
        logger.start_output()
        url_data = FakeUrlData(valid=False, url="http://broken.com")
        logger.log_filter_url(url_data, do_print=True)
        assert logger.stats.number == 1
        assert logger.stats.errors == 1

    def test_end_output_no_error(self):
        results = []
        logger = GradioLogger(results)
        # Should not raise
        logger.end_output()

    def test_all_url_data_attrs_serialized(self):
        results = []
        logger = GradioLogger(results)
        url_data = FakeUrlData()
        logger.log_url(url_data)
        record = results[0]
        for attr in urlDataAttr:
            assert attr in record, f"Missing attribute: {attr}"

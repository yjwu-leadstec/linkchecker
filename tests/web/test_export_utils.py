# Copyright (C) 2024 LinkChecker Authors
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
"""Tests for export utilities."""

import os

import pytest

from linkcheck.web.export_utils import (
    results_to_csv,
    results_to_html,
    save_to_tempfile,
)


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
    ]


class TestResultsToCsv:
    def test_csv_header(self, sample_results):
        csv = results_to_csv(sample_results)
        lines = csv.strip().split(os.linesep)
        assert "url" in lines[0]
        assert "result" in lines[0]

    def test_csv_rows(self, sample_results):
        csv = results_to_csv(sample_results)
        lines = csv.strip().split(os.linesep)
        assert len(lines) == 3  # header + 2 data rows

    def test_csv_valid_field(self, sample_results):
        csv = results_to_csv(sample_results)
        assert "valid" in csv
        assert "error" in csv

    def test_csv_empty_results(self):
        csv = results_to_csv([])
        lines = csv.strip().split(os.linesep)
        assert len(lines) == 1  # header only


class TestResultsToHtml:
    def test_html_structure(self, sample_results):
        html = results_to_html(sample_results)
        assert "<!DOCTYPE html>" in html
        assert "<table>" in html
        assert "</table>" in html

    def test_html_contains_urls(self, sample_results):
        html = results_to_html(sample_results)
        assert "http://example.com" in html
        assert "http://example.com/broken" in html

    def test_html_status_icons(self, sample_results):
        html = results_to_html(sample_results)
        assert "\u2713" in html  # valid
        assert "\u2717" in html  # error

    def test_html_empty_results(self):
        html = results_to_html([])
        assert "<tbody>" in html
        assert "0 URLs" in html

    def test_html_no_javascript_href(self):
        """Non-HTTP URLs should not have href attributes (XSS prevention)."""
        results = [{
            "url": "javascript:alert(1)",
            "parent_url": "",
            "result": "Error",
            "valid": False,
            "warnings": [],
            "checktime": 0,
            "size": 0,
            "content_type": "",
            "level": 0,
        }]
        html = results_to_html(results)
        assert 'href="javascript:' not in html
        # URL text should still appear in the output
        assert "javascript:alert(1)" in html

    def test_html_http_urls_linked(self, sample_results):
        """HTTP URLs should have href links."""
        html = results_to_html(sample_results)
        assert 'href="http://example.com"' in html


class TestSaveToTempfile:
    def test_creates_file(self):
        path = save_to_tempfile("test content", suffix=".csv")
        try:
            assert os.path.exists(path)
            with open(path) as f:
                assert f.read() == "test content"
        finally:
            os.unlink(path)

    def test_suffix(self):
        path = save_to_tempfile("test", suffix=".html")
        try:
            assert path.endswith(".html")
        finally:
            os.unlink(path)

    def test_prefix(self):
        path = save_to_tempfile("test", suffix=".csv")
        try:
            assert "linkchecker-" in os.path.basename(path)
        finally:
            os.unlink(path)

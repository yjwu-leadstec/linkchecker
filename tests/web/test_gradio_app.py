# Copyright (C) 2024 LinkChecker Authors
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
"""Tests for gradio_app helper functions."""

import os

import pytest

gradio_app = pytest.importorskip(
    "linkcheck.web.gradio_app", reason="gradio not installed"
)
_validate_path = gradio_app._validate_path


class TestValidatePath:
    def test_normal_path(self, tmp_path):
        path = str(tmp_path / "test.ini")
        resolved, err = _validate_path(path, {".ini", ".cfg"})
        assert err is None
        assert resolved.endswith("test.ini")

    def test_rejects_dotdot_traversal(self):
        resolved, err = _validate_path("/tmp/../etc/passwd", {".ini"})
        assert resolved is None
        assert ".." in err

    def test_rejects_bad_extension(self, tmp_path):
        path = str(tmp_path / "evil.sh")
        resolved, err = _validate_path(path, {".ini", ".cfg"})
        assert resolved is None
        assert "not allowed" in err

    def test_allows_empty_extension(self, tmp_path):
        path = str(tmp_path / "linkcheckerrc")
        resolved, err = _validate_path(path, {".ini", ".cfg", ""})
        assert err is None
        assert resolved.endswith("linkcheckerrc")

    def test_no_extension_check(self, tmp_path):
        path = str(tmp_path / "anything.xyz")
        resolved, err = _validate_path(path, None)
        assert err is None

    def test_tilde_expansion(self):
        resolved, err = _validate_path("~/test.ini", {".ini"})
        assert err is None
        assert "~" not in resolved


class TestExportFunctions:
    def test_export_csv_from_raw_data(self):
        _export_csv = gradio_app._export_csv
        results = [
            {"url": "http://example.com", "parent_url": "",
             "valid": True, "result": "200 OK",
             "checktime": 0.5, "size": 1024},
        ]
        path = _export_csv(results)
        assert path is not None
        assert path.endswith(".csv")
        os.unlink(path)

    def test_export_html_from_raw_data(self):
        _export_html = gradio_app._export_html
        results = [
            {"url": "http://example.com", "parent_url": "",
             "valid": False, "result": "404 Not Found",
             "checktime": 0.3, "size": 0},
        ]
        path = _export_html(results)
        assert path is not None
        assert path.endswith(".html")
        os.unlink(path)

    def test_export_csv_empty(self):
        _export_csv = gradio_app._export_csv
        assert _export_csv([]) is None
        assert _export_csv(None) is None

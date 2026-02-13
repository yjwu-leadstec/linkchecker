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
Export utilities for check results (CSV, HTML).
"""

import csv
import html
import io
import os
import tempfile

# Columns for CSV export (matches linkcheck/logger/csvlog.py)
CSV_COLUMNS = (
    "url",
    "parent_url",
    "result",
    "valid",
    "warnings",
    "checktime",
    "size",
    "content_type",
    "level",
)


def results_to_csv(results):
    """Convert result dicts to a CSV string.

    Args:
        results: List of result dicts from GradioLogger.

    Returns:
        CSV formatted string.
    """
    output = io.StringIO()
    writer = csv.writer(output, delimiter=";", lineterminator=os.linesep)
    writer.writerow(CSV_COLUMNS)
    for r in results:
        row = []
        for col in CSV_COLUMNS:
            val = r.get(col, "")
            if col == "warnings" and isinstance(val, list):
                val = "; ".join(m for _, m in val) if val else ""
            elif col == "valid":
                val = "valid" if val else "error"
            elif val is None:
                val = ""
            row.append(val)
        writer.writerow(row)
    return output.getvalue()


def results_to_html(results):
    """Convert result dicts to an HTML table string.

    Args:
        results: List of result dicts from GradioLogger.

    Returns:
        HTML string with a styled table.
    """
    lines = [
        "<!DOCTYPE html>",
        "<html><head><meta charset='utf-8'>",
        "<title>LinkChecker Results</title>",
        "<style>",
        "body { font-family: sans-serif; margin: 20px; }",
        "table { border-collapse: collapse; width: 100%; }",
        "th, td { border: 1px solid #ddd; padding: 8px; text-align: left; }",
        "th { background-color: #4a90d9; color: white; }",
        "tr:nth-child(even) { background-color: #f2f2f2; }",
        ".valid { color: green; } .error { color: red; }",
        "</style></head><body>",
        f"<h1>LinkChecker Results ({len(results)} URLs)</h1>",
        "<table><thead><tr>",
    ]
    headers = ["URL", "Parent URL", "Status", "Result", "Time (s)", "Size"]
    for h in headers:
        lines.append(f"<th>{h}</th>")
    lines.append("</tr></thead><tbody>")

    for r in results:
        valid = r.get("valid", True)
        status_cls = "valid" if valid else "error"
        status_text = "\u2713" if valid else "\u2717"
        url = html.escape(str(r.get("url", "")))
        parent = html.escape(str(r.get("parent_url", "")))
        result_text = html.escape(str(r.get("result", "")))
        checktime = r.get("checktime", 0) or 0
        size = r.get("size", -1) or -1

        lines.append("<tr>")
        # Only link http/https URLs to prevent javascript: XSS
        raw_url = str(r.get("url", ""))
        if raw_url.lower().startswith(("http://", "https://")):
            lines.append(f'<td><a href="{url}">{url}</a></td>')
        else:
            lines.append(f"<td>{url}</td>")
        lines.append(f"<td>{parent}</td>")
        lines.append(f'<td class="{status_cls}">{status_text}</td>')
        lines.append(f"<td>{result_text}</td>")
        lines.append(f"<td>{checktime:.2f}</td>")
        lines.append(f"<td>{size if size >= 0 else '-'}</td>")
        lines.append("</tr>")

    lines.extend(["</tbody></table>", "</body></html>"])
    return "\n".join(lines)


def save_to_tempfile(content, suffix=".csv"):
    """Write content to a temporary file and return its path.

    Args:
        content: String content to write.
        suffix: File extension.

    Returns:
        Path to the temporary file.
    """
    fd = tempfile.NamedTemporaryFile(
        mode="w",
        suffix=suffix,
        prefix="linkchecker-",
        delete=False,
        encoding="utf-8",
    )
    fd.write(content)
    fd.close()
    return fd.name

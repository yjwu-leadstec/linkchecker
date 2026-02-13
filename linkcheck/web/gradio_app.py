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
Gradio web interface for LinkChecker.

Three tabs:
  1. Link Checker — run checks, view real-time results, pause/resume, export
  2. Configuration — edit linkcheckerrc INI file
  3. History — view past sessions and error trends
"""

import json
import os
import threading
import time
from datetime import datetime

import gradio as gr

from .. import configuration
from .check_runner import CheckRunner
from .export_utils import results_to_csv, results_to_html, save_to_tempfile
from .history_store import HistoryStore

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------
_runner = CheckRunner()
_history = HistoryStore()


_ALLOWED_CONFIG_EXT = {".ini", ".cfg", ".conf", ""}
_ALLOWED_EXPORT_EXT = {".csv", ".html", ".htm", ".txt", ".xml"}


def _validate_path(path, allowed_extensions=None):
    """Validate a user-supplied file path.

    Rejects paths containing '..' traversal. Optionally checks
    file extension against an allow-list.

    Returns (resolved_path, error_msg). error_msg is None if valid.
    """
    expanded = os.path.expanduser(path)
    resolved = os.path.realpath(expanded)
    if ".." in os.path.relpath(resolved, os.path.expanduser("~")):
        # Allow paths within home directory tree only
        pass
    # Block explicit ".." in the original input
    if ".." in path:
        return None, "Path must not contain '..' traversal."
    if allowed_extensions is not None:
        _, ext = os.path.splitext(resolved)
        if ext.lower() not in allowed_extensions:
            return None, (
                f"Extension '{ext}' not allowed. "
                f"Use one of: {', '.join(sorted(allowed_extensions))}"
            )
    return resolved, None


def _build_dataframe_rows(results):
    """Convert result dicts to rows for gr.Dataframe."""
    rows = []
    for r in results:
        valid = r.get("valid", True)
        warnings = r.get("warnings", [])
        if not valid:
            status = "\u2717"  # ✗ error always takes precedence
        elif warnings:
            status = "\u26a0"  # ⚠ valid with warnings
        else:
            status = "\u2713"  # ✓ valid, no warnings
        checktime = r.get("checktime", 0) or 0
        size = r.get("size", -1) or -1
        rows.append([
            r.get("url", ""),
            r.get("parent_url", ""),
            status,
            r.get("result", ""),
            f"{checktime:.2f}",
            str(size) if size >= 0 else "-",
        ])
    return rows


_DF_HEADERS = ["URL", "Parent URL", "Status", "Result", "Time (s)", "Size"]


# ---------------------------------------------------------------------------
# Tab 1: Link Checker
# ---------------------------------------------------------------------------

def _run_check_generator(
    url_text, threads, timeout, recursion, check_extern,
    save_to_file, file_path, file_format,
):
    """Generator that yields incremental updates to the Gradio UI.

    Yields: (results_df, status_text, start_btn, pause_btn, results_state)
    """
    urls = [u.strip() for u in url_text.strip().splitlines() if u.strip()]
    if not urls:
        yield (
            gr.update(value=[]),
            "Please enter at least one URL.",
            gr.update(interactive=True),   # start btn
            gr.update(interactive=False),  # pause btn
            [],                            # results_state
        )
        return

    results_list = []
    config_overrides = {
        "threads": int(threads),
        "timeout": int(timeout),
        "recursionlevel": int(recursion),
        "checkextern": bool(check_extern),
    }

    # Optional file logger
    file_loggers = []
    if save_to_file and file_path:
        file_loggers = _build_file_loggers(file_path, file_format)

    start_time = time.monotonic()

    # Start check in background thread
    check_thread = threading.Thread(
        target=_runner.run_check,
        kwargs=dict(
            urls=urls,
            config_overrides=config_overrides,
            results_list=results_list,
            persist=True,
            file_loggers=file_loggers,
        ),
        daemon=True,
    )
    check_thread.start()

    # Yield incremental updates
    last_count = 0
    while check_thread.is_alive():
        current_count = len(results_list)
        if current_count > last_count:
            last_count = current_count
            rows = _build_dataframe_rows(results_list)
            elapsed = time.monotonic() - start_time
            yield (
                gr.update(value=rows),
                f"Checking... {last_count} URLs processed ({elapsed:.1f}s)",
                gr.update(interactive=False),
                gr.update(interactive=True),
                list(results_list),
            )
        time.sleep(0.5)

    duration = time.monotonic() - start_time

    # Final update
    rows = _build_dataframe_rows(results_list)
    total = len(results_list)
    errors = sum(1 for r in results_list if not r.get("valid"))
    status_msg = f"Done! {total} URLs checked, {errors} errors found. ({duration:.1f}s)"
    if _runner.error:
        status_msg = f"Error: {_runner.error}"

    # Save to history
    if results_list and not _runner.error:
        _history.save_session(
            urls, results_list,
            stats=None, duration=duration,
        )

    yield (
        gr.update(value=rows),
        status_msg,
        gr.update(interactive=True),
        gr.update(interactive=False),
        list(results_list),
    )


def _pause_check():
    """Pause the running check."""
    cache_db = _runner.pause_check()
    if cache_db:
        return (
            f"Paused. Cache saved to: {cache_db}",
            cache_db,
            gr.update(interactive=True),   # start btn
            gr.update(interactive=False),  # pause btn
            gr.update(interactive=True),   # resume btn
        )
    return (
        "No running check to pause.",
        None,
        gr.update(interactive=True),
        gr.update(interactive=False),
        gr.update(interactive=False),
    )


def _resume_check_generator(
    cache_db_path, url_text, threads, timeout, recursion, check_extern,
):
    """Generator for resuming a paused check.

    Yields: (results_df, status_text, start_btn, pause_btn, resume_btn,
             results_state)
    """
    if not cache_db_path or not os.path.exists(cache_db_path):
        yield (
            gr.update(),
            "No cache DB found. Start a new check instead.",
            gr.update(interactive=True),
            gr.update(interactive=False),
            gr.update(interactive=False),
            [],
        )
        return

    urls = [u.strip() for u in url_text.strip().splitlines() if u.strip()]
    results_list = []
    config_overrides = {
        "threads": int(threads),
        "timeout": int(timeout),
        "recursionlevel": int(recursion),
        "checkextern": bool(check_extern),
    }

    start_time = time.monotonic()

    check_thread = threading.Thread(
        target=_runner.resume_check,
        kwargs=dict(
            cache_db_path=cache_db_path,
            urls=urls,
            config_overrides=config_overrides,
            results_list=results_list,
        ),
        daemon=True,
    )
    check_thread.start()

    last_count = 0
    while check_thread.is_alive():
        current_count = len(results_list)
        if current_count > last_count:
            last_count = current_count
            rows = _build_dataframe_rows(results_list)
            elapsed = time.monotonic() - start_time
            yield (
                gr.update(value=rows),
                f"Resuming... {last_count} URLs processed ({elapsed:.1f}s)",
                gr.update(interactive=False),
                gr.update(interactive=True),
                gr.update(interactive=False),
                list(results_list),
            )
        time.sleep(0.5)

    duration = time.monotonic() - start_time

    rows = _build_dataframe_rows(results_list)
    total = len(results_list)
    errors = sum(1 for r in results_list if not r.get("valid"))
    status_msg = f"Done! {total} URLs checked, {errors} errors found. ({duration:.1f}s)"

    if results_list:
        _history.save_session(urls, results_list, stats=None, duration=duration)

    yield (
        gr.update(value=rows),
        status_msg,
        gr.update(interactive=True),
        gr.update(interactive=False),
        gr.update(interactive=False),
        list(results_list),
    )


def _cancel_check():
    """Cancel the running check (no resume possible)."""
    _runner.cancel_check()
    return (
        "Check cancelled.",
        gr.update(interactive=True),
        gr.update(interactive=False),
    )


def _build_file_loggers(file_path, file_format):
    """Build file-output Logger instances for the chosen format.

    Returns an empty list if the format is unknown or if logger
    creation fails.
    """
    loggers = []
    fmt = file_format.lower() if file_format else "csv"
    ext_map = {"csv": ".csv", "html": ".html", "text": ".txt"}
    ext = ext_map.get(fmt)
    if ext is None:
        return loggers

    full_path = file_path + ext
    resolved, err = _validate_path(full_path, _ALLOWED_EXPORT_EXT)
    if err:
        return loggers

    try:
        if fmt == "csv":
            from ..logger.csvlog import CSVLogger
            loggers.append(
                CSVLogger(fileoutput=True, filename=resolved))
        elif fmt == "html":
            from ..logger.html import HtmlLogger
            loggers.append(
                HtmlLogger(fileoutput=True, filename=resolved))
        elif fmt == "text":
            from ..logger.text import TextLogger
            loggers.append(
                TextLogger(fileoutput=True, filename=resolved))
    except Exception:
        pass
    return loggers


def _export_csv(results_list):
    """Export current results as CSV file from raw data."""
    if not results_list:
        return None
    csv_content = results_to_csv(results_list)
    return save_to_tempfile(csv_content, suffix=".csv")


def _export_html(results_list):
    """Export current results as HTML file from raw data."""
    if not results_list:
        return None
    html_content = results_to_html(results_list)
    return save_to_tempfile(html_content, suffix=".html")


# ---------------------------------------------------------------------------
# Tab 2: Configuration
# ---------------------------------------------------------------------------

def _get_default_config_path():
    """Return the default linkcheckerrc path."""
    return configuration.get_user_config()


def _load_config(config_path):
    """Load configuration file content."""
    path = config_path.strip() if config_path else _get_default_config_path()
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(), f"Loaded: {path}"
    return "", f"File not found: {path}"


def _save_config(config_path, content):
    """Save configuration file content."""
    path = config_path.strip() if config_path else _get_default_config_path()
    if not path:
        return "No path specified."
    resolved, err = _validate_path(path, _ALLOWED_CONFIG_EXT)
    if err:
        return f"Invalid path: {err}"
    try:
        os.makedirs(os.path.dirname(resolved) or ".", exist_ok=True)
        with open(resolved, "w", encoding="utf-8") as f:
            f.write(content)
    except OSError as e:
        return f"Error saving: {e}"
    return f"Saved: {resolved}"


# ---------------------------------------------------------------------------
# Tab 3: History
# ---------------------------------------------------------------------------

def _load_history():
    """Load session history for display."""
    sessions = _history.get_sessions(limit=50)
    rows = []
    for s in sessions:
        created = datetime.fromtimestamp(s["created_at"]).strftime("%Y-%m-%d %H:%M")
        urls = s.get("urls", "[]")
        try:
            url_list = ", ".join(
                u[:50] for u in (
                    json.loads(urls) if isinstance(urls, str) else urls
                )
            )
        except Exception:
            url_list = str(urls)[:100]
        rows.append([
            s["id"][:8],
            url_list,
            created,
            f'{s.get("duration", 0):.1f}s',
            s.get("total", 0),
            s.get("errors", 0),
            s.get("warnings", 0),
        ])
    return rows


def _load_trend_plot(url_filter="", days=30):
    """Generate a matplotlib trend plot.

    Uses Figure() directly instead of plt.subplots() to avoid
    accumulating figures in pyplot's global state.
    """
    try:
        from matplotlib.figure import Figure
    except ImportError:
        return None

    pattern = url_filter.strip() if url_filter else None
    data = _history.get_trend_data(url_pattern=pattern, days=int(days))

    fig = Figure(figsize=(8, 4))
    ax = fig.add_subplot(111)
    if data:
        dates = [d[0] for d in data]
        errors = [d[1] for d in data]
        totals = [d[2] for d in data]
        ax.bar(dates, totals, label="Total", alpha=0.5, color="#4a90d9")
        ax.bar(dates, errors, label="Errors", alpha=0.8, color="#e74c3c")
        ax.set_xlabel("Date")
        ax.set_ylabel("Count")
        ax.legend()
        for tick in ax.get_xticklabels():
            tick.set_rotation(45)
            tick.set_ha("right")
    else:
        ax.text(
            0.5, 0.5, "No data available",
            ha="center", va="center", transform=ax.transAxes,
        )
    ax.set_title("Link Check Trends")
    fig.tight_layout()
    return fig


def _delete_session(session_id_prefix):
    """Delete a session by ID prefix."""
    if not session_id_prefix:
        return "No session selected.", _load_history()
    sessions = _history.get_sessions(limit=100)
    for s in sessions:
        if s["id"].startswith(session_id_prefix.strip()):
            _history.delete_session(s["id"])
            return f"Deleted session {s['id'][:8]}", _load_history()
    return "Session not found.", _load_history()


# ---------------------------------------------------------------------------
# App builder
# ---------------------------------------------------------------------------

def create_app():
    """Create and return the Gradio Blocks application."""

    with gr.Blocks(
        title="LinkChecker Web UI",
        theme=gr.themes.Soft(),
    ) as app:
        gr.Markdown("# LinkChecker Web UI")
        gr.Markdown("Check links in websites and local HTML files.")

        # Shared state for pause/resume and raw results
        cache_db_state = gr.State(value=None)
        results_state = gr.State(value=[])

        with gr.Tabs():
            # ===========================================================
            # Tab 1: Link Checker
            # ===========================================================
            with gr.TabItem("Link Checker"):
                with gr.Row():
                    with gr.Column(scale=2):
                        url_input = gr.Textbox(
                            label="URLs to check (one per line)",
                            placeholder="https://example.com\nhttps://another.com",
                            lines=4,
                        )
                    with gr.Column(scale=1):
                        threads_slider = gr.Slider(
                            1, 20, value=10, step=1, label="Threads",
                        )
                        timeout_slider = gr.Slider(
                            5, 120, value=60, step=5, label="Timeout (s)",
                        )
                        recursion_slider = gr.Slider(
                            -1, 10, value=-1, step=1,
                            label="Recursion depth (-1 = infinite)",
                        )
                        check_extern_cb = gr.Checkbox(
                            label="Check external links", value=False,
                        )

                with gr.Accordion("Save to File", open=False):
                    save_to_file_cb = gr.Checkbox(
                        label="Save results to file", value=False,
                    )
                    file_path_input = gr.Textbox(
                        label="File path",
                        value="~/linkchecker-results",
                    )
                    file_format_dd = gr.Dropdown(
                        choices=["CSV", "HTML", "Text"],
                        value="CSV",
                        label="Format",
                    )

                with gr.Row():
                    start_btn = gr.Button("Start Check", variant="primary")
                    pause_btn = gr.Button("Pause", interactive=False)
                    resume_btn = gr.Button("Resume", interactive=False)

                status_text = gr.Textbox(
                    label="Status", interactive=False, value="Ready",
                )

                results_df = gr.Dataframe(
                    headers=_DF_HEADERS,
                    label="Results",
                    interactive=False,
                    wrap=True,
                )

                with gr.Row():
                    export_csv_btn = gr.Button("Export CSV")
                    export_html_btn = gr.Button("Export HTML")
                    export_file = gr.File(label="Download", interactive=False)

                # Event bindings
                start_btn.click(
                    fn=_run_check_generator,
                    inputs=[
                        url_input, threads_slider, timeout_slider,
                        recursion_slider, check_extern_cb,
                        save_to_file_cb, file_path_input, file_format_dd,
                    ],
                    outputs=[
                        results_df, status_text, start_btn, pause_btn,
                        results_state,
                    ],
                )

                pause_btn.click(
                    fn=_pause_check,
                    inputs=[],
                    outputs=[
                        status_text, cache_db_state,
                        start_btn, pause_btn, resume_btn,
                    ],
                )

                resume_btn.click(
                    fn=_resume_check_generator,
                    inputs=[
                        cache_db_state, url_input,
                        threads_slider, timeout_slider,
                        recursion_slider, check_extern_cb,
                    ],
                    outputs=[
                        results_df, status_text,
                        start_btn, pause_btn, resume_btn,
                        results_state,
                    ],
                )

                export_csv_btn.click(
                    fn=_export_csv,
                    inputs=[results_state],
                    outputs=[export_file],
                )
                export_html_btn.click(
                    fn=_export_html,
                    inputs=[results_state],
                    outputs=[export_file],
                )

            # ===========================================================
            # Tab 2: Configuration
            # ===========================================================
            with gr.TabItem("Configuration"):
                config_path_input = gr.Textbox(
                    label="Config file path",
                    value=_get_default_config_path() or "",
                )
                config_editor = gr.Code(
                    label="Configuration (INI format)",
                    language=None,
                    lines=20,
                )
                config_status = gr.Textbox(
                    label="Status", interactive=False,
                )
                with gr.Row():
                    load_config_btn = gr.Button("Load")
                    save_config_btn = gr.Button("Save", variant="primary")

                load_config_btn.click(
                    fn=_load_config,
                    inputs=[config_path_input],
                    outputs=[config_editor, config_status],
                )
                save_config_btn.click(
                    fn=_save_config,
                    inputs=[config_path_input, config_editor],
                    outputs=[config_status],
                )

            # ===========================================================
            # Tab 3: History
            # ===========================================================
            with gr.TabItem("History"):
                with gr.Row():
                    url_filter_input = gr.Textbox(
                        label="Filter by URL", placeholder="example.com",
                    )
                    days_slider = gr.Slider(
                        7, 90, value=30, step=1, label="Days",
                    )
                    refresh_btn = gr.Button("Refresh")

                history_df = gr.Dataframe(
                    headers=[
                        "ID", "URLs", "Date", "Duration",
                        "Total", "Errors", "Warnings",
                    ],
                    label="Check History",
                    interactive=False,
                )

                trend_plot = gr.Plot(label="Error Trends")

                with gr.Row():
                    delete_session_input = gr.Textbox(
                        label="Session ID to delete",
                        placeholder="e.g. a1b2c3d4",
                    )
                    delete_btn = gr.Button("Delete Session", variant="stop")
                    delete_status = gr.Textbox(
                        label="Status", interactive=False,
                    )

                def _refresh_history(url_filter, days):
                    return _load_history(), _load_trend_plot(url_filter, days)

                refresh_btn.click(
                    fn=_refresh_history,
                    inputs=[url_filter_input, days_slider],
                    outputs=[history_df, trend_plot],
                )

                delete_btn.click(
                    fn=_delete_session,
                    inputs=[delete_session_input],
                    outputs=[delete_status, history_df],
                )

                # Load history on app start
                app.load(
                    fn=lambda: (_load_history(), _load_trend_plot()),
                    outputs=[history_df, trend_plot],
                )

    return app

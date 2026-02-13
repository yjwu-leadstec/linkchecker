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
Runner that wraps the LinkChecker director API for the Gradio web UI.
"""

import os
import threading

from .. import configuration, log, logconf
from ..cmdline import aggregate_url
from ..director import check_urls, get_aggregate
from ..director.console import StatusLogger
from .gradio_logger import GradioLogger

LOG_CHECK = logconf.LOG_CHECK

# Default cache DB directory
_CACHE_DIR = os.path.join(os.path.expanduser("~"), ".linkchecker")


def _default_cache_db():
    """Return the default path for web UI cache DB."""
    os.makedirs(_CACHE_DIR, exist_ok=True)
    return os.path.join(_CACHE_DIR, "web-cache.db")


class CheckRunner:
    """Encapsulates a single LinkChecker run for the web UI.

    Manages lifecycle: configure → run → cancel/pause → resume.
    Only one check can run at a time (guarded by is_running flag).
    """

    def __init__(self):
        self.aggregate = None
        self.is_running = False
        self.error = None
        self._lock = threading.Lock()
        self._last_cache_db = None
        self._pause_requested = False

    def run_check(
        self, urls, config_overrides=None, results_list=None,
        persist=True, file_loggers=None,
    ):
        """Execute a link check in the current thread.

        Args:
            urls: List of URL strings to check.
            config_overrides: Dict of config keys to override
                (threads, timeout, recursionlevel, checkextern).
            results_list: Shared list for GradioLogger to append results.
            persist: Enable SQLite persistence for pause/resume.
            file_loggers: Optional list of additional Logger instances
                to add to fileoutput (e.g. CSV/HTML file loggers).

        Returns:
            LogStatistics object from the GradioLogger, or None on error.
        """
        with self._lock:
            if self.is_running:
                raise RuntimeError("A check is already running")
            self.is_running = True
            self.error = None
            self._pause_requested = False

        if results_list is None:
            results_list = []
        if config_overrides is None:
            config_overrides = {}

        try:
            config = self._build_config(
                config_overrides, results_list, persist=persist,
                file_loggers=file_loggers,
            )
            self.aggregate = get_aggregate(config)
            self._last_cache_db = config.get("cache_db")

            for url in urls:
                aggregate_url(self.aggregate, url.strip())

            check_urls(self.aggregate)
            return config["fileoutput"][0].stats if config["fileoutput"] else None

        except Exception as exc:
            self.error = str(exc)
            log.warn(LOG_CHECK, "Web UI check error: %s", exc)
            return None
        finally:
            with self._lock:
                self.is_running = False
                self.aggregate = None

    def cancel_check(self):
        """Cancel the running check immediately.

        Signals the aggregate to stop. The background thread running
        check_urls will handle finish/cleanup automatically.
        Calling finish/end_log_output here would race with check_urls.
        """
        agg = self.aggregate
        if agg is not None:
            agg.cancel()

    def pause_check(self):
        """Pause the running check, preserving the cache DB for resume.

        Signals the aggregate to stop. The background thread handles
        cleanup. Note: check_urls will call _cleanup_persistence with
        interrupted=False which deletes the DB. For true pause/resume,
        the persist layer would need deeper integration.

        Returns:
            Path to the cache DB file, or None if not available.
        """
        agg = self.aggregate
        if agg is not None:
            self._pause_requested = True
            agg.cancel()
            return self._last_cache_db
        return None

    def resume_check(
        self, cache_db_path, urls, config_overrides=None,
        results_list=None, file_loggers=None,
    ):
        """Resume a previously paused check.

        Args:
            cache_db_path: Path to the SQLite cache DB from a paused check.
            urls: Original URL list (needed for aggregate_url).
            config_overrides: Same as run_check.
            results_list: Shared list for GradioLogger.
            file_loggers: Optional additional file loggers.

        Returns:
            LogStatistics or None on error.
        """
        with self._lock:
            if self.is_running:
                raise RuntimeError("A check is already running")
            self.is_running = True
            self.error = None

        if results_list is None:
            results_list = []
        if config_overrides is None:
            config_overrides = {}

        try:
            config = self._build_config(
                config_overrides, results_list, persist=True,
                resume=True, cache_db=cache_db_path,
                file_loggers=file_loggers,
            )
            self.aggregate = get_aggregate(config)
            self._last_cache_db = cache_db_path

            # For resume, aggregate_url is only needed if no pending URLs
            stats = None
            if hasattr(self.aggregate, 'sqlite_store'):
                stats = self.aggregate.sqlite_store.get_queue_stats()

            pending = 0
            if stats:
                pending = stats.get('pending', 0) + stats.get('in_progress', 0)

            if pending == 0:
                for url in urls:
                    aggregate_url(self.aggregate, url.strip())

            check_urls(self.aggregate)
            return config["fileoutput"][0].stats if config["fileoutput"] else None

        except Exception as exc:
            self.error = str(exc)
            log.warn(LOG_CHECK, "Web UI resume error: %s", exc)
            return None
        finally:
            with self._lock:
                self.is_running = False
                self.aggregate = None

    def _build_config(
        self, overrides, results_list, persist=False,
        resume=False, cache_db=None, file_loggers=None,
    ):
        """Build a Configuration object for the check."""
        config = configuration.Configuration()
        config.set_status_logger(StatusLogger())

        # Apply user overrides
        for key, value in overrides.items():
            if key in config and value is not None:
                config[key] = value

        # Persistence settings
        config["persist"] = persist
        config["resume"] = resume
        if cache_db:
            config["cache_db"] = cache_db
        elif persist:
            config["cache_db"] = _default_cache_db()

        # Create GradioLogger and add as fileoutput
        gradio_logger = GradioLogger(results_list)
        config["fileoutput"] = [gradio_logger]

        # Add any additional file loggers
        if file_loggers:
            config["fileoutput"].extend(file_loggers)

        # Sanitize creates the main console logger
        config.sanitize()

        return config

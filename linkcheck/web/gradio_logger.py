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
Custom Logger for Gradio web UI that collects results into a shared list.
"""

from ..checker.urlbase import urlDataAttr
from ..logger import _Logger


class GradioLogger(_Logger):
    """Logger that serializes URL check results into a shared list.

    Used as a fileoutput logger alongside the main console logger.
    The results_list is shared with the Gradio UI thread which polls
    it periodically to update the display.
    """

    LoggerName = "gradio"
    LoggerArgs = {}

    def __init__(self, results_list, **kwargs):
        args = self.get_args(kwargs)
        super().__init__(**args)
        self.results_list = results_list
        # No file output needed — we collect into a list
        self.fd = None

    def start_output(self):
        """Initialize stats and timing."""
        super().start_output()

    def log_filter_url(self, url_data, do_print):
        """Override to always call log_url() regardless of do_print.

        The default implementation only calls log_url() when do_print
        is True (i.e. for invalid/warning URLs). The web UI needs to
        display ALL checked URLs, so we always log.
        """
        self.stats.log_url(url_data, do_print)
        self.log_url(url_data)

    def log_url(self, url_data):
        """Serialize CompactUrlData to dict and append to results_list.

        list.append() is atomic under the GIL so this is thread-safe
        without additional locking.
        """
        record = {}
        for attr in urlDataAttr:
            value = getattr(url_data, attr, None)
            if attr == "warnings" and value:
                # warnings is a list of (tag, message) tuples
                value = [(str(t), str(m)) for t, m in value]
            elif attr == "info" and value:
                value = list(value)
            elif attr == "modified" and value is not None:
                value = str(value)
            record[attr] = value
        self.results_list.append(record)

    def end_output(self, **kwargs):
        """Finalize output. Nothing to flush for list-based logger."""
        pass

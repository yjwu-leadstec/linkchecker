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
Rebuild UrlData objects from SQLite persistent storage rows.

Used when loading URLs from the persistent queue back into the
in-memory processing pipeline.
"""

import json

from ..checker import get_url_from


def rebuild_url_data(row, aggregate):
    """Rebuild a UrlData object from a SQLite url_queue row.

    @param row: sqlite3.Row from the url_queue table
    @param aggregate: Aggregate instance for URL construction
    @return: UrlData instance with _sqlite_queue_id attached
    """
    # Deserialize extern field
    extern_raw = row['extern']
    extern = None
    if extern_raw:
        try:
            parsed = json.loads(extern_raw)
            if isinstance(parsed, list) and len(parsed) == 2:
                extern = tuple(parsed)
        except (json.JSONDecodeError, TypeError):
            pass

    url_data = get_url_from(
        row['url'],
        row['recursion_level'],
        aggregate,
        parent_url=row['parent_url'] or None,
        base_ref=row['base_ref'] or None,
        line=row['line'],
        column=row['column_num'],
        page=row['page'],
        name=row['name'] or '',
        parent_content_type=row['parent_content_type'] or None,
        extern=extern,
        url_encoding=row['url_encoding'] or None,
    )

    # Attach SQLite queue ID for tracking task completion
    url_data._sqlite_queue_id = row['id']

    return url_data

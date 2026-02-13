# Copyright (C) 2006-2014 Bastian Kleineidam
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
Management of checking a queue of links with several threads.
"""
import os
import time

from .. import log, LOG_CHECK, LinkCheckerError, LinkCheckerInterrupt, plugins
from ..cache import urlqueue, robots_txt, results
from . import aggregator, console


def check_urls(aggregate):
    """Main check function; checks all configured URLs until interrupted
    with Ctrl-C.

    @return: None
    """
    try:
        aggregate.visit_loginurl()
    except LinkCheckerError as msg:
        log.warn(LOG_CHECK, _("Problem using login URL: %(msg)s.") % dict(msg=msg))
        return
    except Exception as msg:
        log.warn(LOG_CHECK, _("Error using login URL: %(msg)s.") % dict(msg=msg))
        raise
    try:
        aggregate.logger.start_log_output()
    except Exception as msg:
        log.error(LOG_CHECK, _("Error starting log output: %(msg)s.") % dict(msg=msg))
        raise
    try:
        if not aggregate.urlqueue.empty():
            aggregate.start_threads()
        check_url(aggregate)
        aggregate.finish()
        aggregate.end_log_output()
        interrupted = getattr(aggregate, '_pause_requested', False)
        _cleanup_persistence(aggregate, interrupted=interrupted)
    except LinkCheckerInterrupt:
        raise
    except KeyboardInterrupt:
        interrupt(aggregate)
    except RuntimeError:
        log.warn(
            LOG_CHECK,
            _(
                "Could not start a new thread. Check that the current user"
                " is allowed to start new threads."
            ),
        )
        abort(aggregate)
    except Exception:
        # Catching "Exception" is intentionally done. This saves the program
        # from libraries that raise all kinds of strange exceptions.
        console.internal_error()
        aggregate.logger.log_internal_error()
        abort(aggregate)
    # Not caught exceptions at this point are SystemExit and GeneratorExit,
    # and both should be handled by the calling layer.


def check_url(aggregate):
    """Helper function waiting for URL queue."""
    while True:
        try:
            aggregate.urlqueue.join(timeout=30)
            break
        except urlqueue.Timeout:
            # Cleanup threads every 30 seconds
            aggregate.remove_stopped_threads()
            if not any(aggregate.get_check_threads()):
                break


def interrupt(aggregate):
    """Interrupt execution and shutdown, ignoring any subsequent
    interrupts."""
    while True:
        try:
            log.warn(LOG_CHECK, _("interrupt; waiting for active threads to finish"))
            log.warn(LOG_CHECK, _("another interrupt will exit immediately"))
            abort(aggregate)
            break
        except KeyboardInterrupt:
            pass


def abort(aggregate):
    """Helper function to ensure a clean shutdown."""
    while True:
        try:
            aggregate.abort()
            aggregate.finish()
            aggregate.end_log_output(interrupt=True)
            _cleanup_persistence(aggregate, interrupted=True)
            break
        except KeyboardInterrupt:
            log.warn(LOG_CHECK, _("user abort; force shutdown"))
            aggregate.end_log_output(interrupt=True)
            _cleanup_persistence(aggregate, interrupted=True)
            abort_now()


def abort_now():
    """Force exit of current process without cleanup."""
    if os.name == 'posix':
        # Unix systems can use signals
        import signal

        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(1)
        os.kill(os.getpid(), signal.SIGKILL)
    elif os.name == 'nt':
        # NT has os.abort()
        os.abort()
    else:
        # All other systems have os._exit() as best shot.
        os._exit(3)


def _cleanup_persistence(aggregate, interrupted):
    """Handle SQLite persistence lifecycle after check completion.

    If interrupted, keep the database for resume. If completed normally,
    delete the database file.
    """
    if not hasattr(aggregate, 'sqlite_store'):
        return
    try:
        if interrupted:
            log.info(
                LOG_CHECK,
                _("Check interrupted. Use --resume to continue "
                  "from where you left off."),
            )
        else:
            aggregate.sqlite_store.delete_db()
            log.info(
                LOG_CHECK,
                _("Check completed, cache database removed."),
            )
    finally:
        aggregate.sqlite_store.close()


def get_aggregate(config):
    """Get an aggregator instance with given configuration.

    When config['persist'] is True, uses SQLite-backed persistent queue
    and result cache instead of in-memory versions.
    """
    if config["persist"]:
        from ..cache.sqlite_store import SqliteStore
        from ..cache.persistent_result_cache import PersistentResultCache
        from ..cache.persistent_url_queue import PersistentUrlQueue

        db_path = config["cache_db"]

        if config["resume"]:
            sqlite_store = SqliteStore(db_path)
            # Check config consistency on resume
            saved = sqlite_store.get_metadata('config_snapshot')
            if saved:
                for key in ('recursionlevel', 'checkextern'):
                    if saved.get(key) != config.get(key):
                        log.warn(
                            LOG_CHECK,
                            _("Config '%(key)s' changed from %(old)r "
                              "to %(new)r since last run.") % dict(
                                key=key, old=saved[key], new=config[key]),
                        )
            # Reset in_progress and clear corresponding placeholders
            reset_count = sqlite_store.reset_in_progress()
            if reset_count:
                log.info(
                    LOG_CHECK,
                    _("Resumed: reset %d in-progress URLs.") % reset_count,
                )
            stats = sqlite_store.get_queue_stats()
            log.info(
                LOG_CHECK,
                _("Resume stats: %(pending)d pending, %(done)d done, "
                  "%(skipped)d skipped.") % stats,
            )
        else:
            # New scan: remove old database
            if os.path.exists(db_path):
                temp_store = SqliteStore(db_path)
                temp_store.delete_db()
            sqlite_store = SqliteStore(db_path)
            # Save config snapshot for resume consistency check
            sqlite_store.set_metadata('config_snapshot', {
                'recursionlevel': config['recursionlevel'],
                'checkextern': config['checkextern'],
                'maxnumurls': config['maxnumurls'],
            })

        _urlqueue = PersistentUrlQueue(
            sqlite_store, max_allowed_urls=config["maxnumurls"],
        )
        result_cache = PersistentResultCache(sqlite_store)
    else:
        # Original in-memory mode
        _urlqueue = urlqueue.UrlQueue(max_allowed_urls=config["maxnumurls"])
        result_cache = results.ResultCache(config["resultcachesize"])
        sqlite_store = None

    _robots_txt = robots_txt.RobotsTxt(config["useragent"])
    plugin_manager = plugins.PluginManager(config)

    aggregate = aggregator.Aggregate(
        config, _urlqueue, _robots_txt, plugin_manager, result_cache
    )

    if sqlite_store is not None:
        aggregate.sqlite_store = sqlite_store
        _urlqueue.set_aggregate(aggregate)

    return aggregate

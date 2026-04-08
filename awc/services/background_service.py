"""Background runtime loops for sync and lightweight reconciliation."""

from __future__ import annotations

from datetime import UTC, datetime
import json
import logging
import threading
import time

from ..core.config import settings
from ..core.log_events import log_block, log_exception, log_info
from ..core.logging import get_logger
from .download_service import reconcile_vanished_downloads, restore_on_startup
from .eligible_service import run_eligible_once
from .rss_service import update_rss_cache
from .sanitizer_service import sanitize_links_once, sanitizer_status
from .sync_runner_service import sync_all

logger = get_logger("runtime")

_stop_event = threading.Event()
_threads: dict[str, threading.Thread] = {}
_state_lock = threading.Lock()
_state = {
    "sync": {"running": False, "last_run_at": None, "last_error": ""},
    "rss": {"enabled": False, "running": False, "last_run_at": None, "last_error": "", "last_cached": 0},
    "links": {"enabled": False, "running": False, "last_run_at": None, "last_error": "", "last_result": None},
    "eligible": {"enabled": False, "running": False, "last_run_at": None, "last_error": "", "last_result": None},
    "scanner": {"enabled": True, "running": False, "last_run_at": None, "last_error": "", "last_result": None},
    "startup": {"restored": 0, "fixed": 0},
}


def _minutes_label(seconds: int) -> str:
    if int(seconds) < 60:
        return f"{int(seconds)}s"
    return f"{max(1, int(seconds) // 60)}m"


def runtime_state() -> dict:
    with _state_lock:
        return json.loads(json.dumps(_state))


def _set_state(section: str, **updates) -> None:
    with _state_lock:
        _state.setdefault(section, {}).update(updates)


def _run_rss_loop() -> None:
    _set_state("rss", enabled=settings.rss_enabled, running=True)
    interval = max(30, settings.rss_poll_interval)
    log_info(
        logger,
        "runtime.rss.loop_started",
        "RSS poller started",
        details={"interval_seconds": interval},
        lines=[f"interval={_minutes_label(interval)}"],
    )
    while not _stop_event.is_set():
        try:
            result = update_rss_cache(emit_cycle_logs=True)
            _set_state(
                "rss",
                enabled=bool(result.get("enabled", settings.rss_enabled)),
                running=False,
                last_run_at=datetime.now(UTC).isoformat(),
                last_error=str(result.get("error") or ""),
                last_cached=int(result.get("cached", 0) or 0),
            )
        except Exception as exc:
            log_exception(logger, "runtime.rss.loop_failed", "RSS poller failed", details={"error": str(exc)})
            _set_state(
                "rss",
                enabled=settings.rss_enabled,
                running=False,
                last_run_at=datetime.now(UTC).isoformat(),
                last_error=str(exc),
            )
        if _stop_event.wait(max(30, settings.rss_poll_interval)):
            break


def _run_sync_loop() -> None:
    _set_state("sync", running=True)
    interval = max(60, settings.sync_interval_minutes * 60)
    log_info(
        logger,
        "runtime.sync.loop_started",
        "Background sync loop started",
        details={"interval_seconds": interval},
        lines=[f"interval={_minutes_label(interval)}"],
    )
    if _stop_event.wait(60):  # brief startup grace before first sync
        return
    while not _stop_event.is_set():
        try:
            log_info(logger, "runtime.sync.started", "Background sync started")
            result = sync_all()
            log_info(
                logger,
                "runtime.sync.completed",
                "Background sync completed",
                details={"sonarr": int(result.get("sonarr", 0) or 0), "radarr": int(result.get("radarr", 0) or 0)},
                lines=[f"sonarr={int(result.get('sonarr', 0) or 0)}", f"radarr={int(result.get('radarr', 0) or 0)}"],
            )
            _set_state(
                "sync",
                running=False,
                last_run_at=datetime.now(UTC).isoformat(),
                last_error="",
                last_result=result,
            )
        except Exception as exc:
            log_exception(logger, "runtime.sync.failed", "Background sync failed", details={"error": str(exc)})
            _set_state(
                "sync",
                running=False,
                last_run_at=datetime.now(UTC).isoformat(),
                last_error=str(exc),
            )
        if _stop_event.wait(interval):
            break


def _run_link_loop() -> None:
    _set_state("links", enabled=settings.sanitizer_enabled, running=False, last_error="")
    if not settings.sanitizer_enabled:
        log_info(logger, "runtime.links.disabled", "Sanitizer loop disabled by env")
        return
    first_run = 60 * 10
    interval = 60 * 60 * 24
    log_info(
        logger,
        "runtime.links.scheduled",
        "Sanitizer loop scheduled",
        lines=[f"first_run={_minutes_label(first_run)}", f"interval={_minutes_label(interval)}"],
        details={"first_run_seconds": first_run, "interval_seconds": interval},
    )
    if _stop_event.wait(first_run):
        return
    while not _stop_event.is_set():
        try:
            _set_state("links", running=True)
            result = sanitize_links_once()
            _set_state(
                "links",
                running=False,
                last_run_at=datetime.now(UTC).isoformat(),
                last_error="",
                last_result=result,
            )
        except Exception as exc:
            log_exception(logger, "runtime.links.failed", "Link sanitizer failed", details={"error": str(exc)})
            _set_state(
                "links",
                running=False,
                last_run_at=datetime.now(UTC).isoformat(),
                last_error=str(exc),
                last_result=sanitizer_status().get("last_result"),
            )
        if _stop_event.wait(interval):
            break


def _run_eligible_loop() -> None:
    _set_state("eligible", enabled=settings.eligible_enabled, running=False, last_error="")
    if not settings.eligible_enabled:
        log_info(logger, "runtime.eligible.disabled", "Eligible loop disabled by env")
        return
    interval = max(60 * 60, int(settings.eligible_interval or 0))
    log_info(
        logger,
        "runtime.eligible.scheduled",
        "Eligible loop scheduled",
        lines=[
            f"first_run={_minutes_label(300)}",
            f"interval={_minutes_label(interval)}",
            f"lookback_days={max(0, int(settings.eligible_lookback_days or 0))}",
        ],
        details={
            "first_run_seconds": 300,
            "interval_seconds": interval,
            "lookback_days": max(0, int(settings.eligible_lookback_days or 0)),
        },
    )
    if _stop_event.wait(300):
        return
    while not _stop_event.is_set():
        try:
            _set_state("eligible", running=True)
            result = run_eligible_once()
            _set_state(
                "eligible",
                running=False,
                last_run_at=datetime.now(UTC).isoformat(),
                last_error="",
                last_result=result,
            )
        except Exception as exc:
            log_exception(logger, "runtime.eligible.failed", "Eligible loop failed", details={"error": str(exc)})
            _set_state(
                "eligible",
                running=False,
                last_run_at=datetime.now(UTC).isoformat(),
                last_error=str(exc),
                last_result=runtime_state().get("eligible", {}).get("last_result"),
            )
        if _stop_event.wait(interval):
            break


def _run_scanner_loop() -> None:
    first_run = 30
    interval = 30
    grace_seconds = 60

    _set_state("scanner", enabled=True, running=False, last_error="")
    log_info(
        logger,
        "runtime.scanner.scheduled",
        "Vanished scanner scheduled",
        lines=[
            f"first_run={_minutes_label(first_run)}",
            f"interval={_minutes_label(interval)}",
            f"grace={_minutes_label(grace_seconds)}",
        ],
        details={
            "first_run_seconds": first_run,
            "interval_seconds": interval,
            "grace_seconds": grace_seconds,
        },
    )
    if _stop_event.wait(first_run):
        return
    while not _stop_event.is_set():
        try:
            _set_state("scanner", running=True)
            result = reconcile_vanished_downloads()
            _set_state(
                "scanner",
                running=False,
                last_run_at=datetime.now(UTC).isoformat(),
                last_error="",
                last_result=result,
            )
        except Exception as exc:
            log_exception(logger, "runtime.scanner.failed", "Vanished scanner failed", details={"error": str(exc)})
            _set_state(
                "scanner",
                running=False,
                last_run_at=datetime.now(UTC).isoformat(),
                last_error=str(exc),
                last_result=runtime_state().get("scanner", {}).get("last_result"),
            )
        if _stop_event.wait(interval):
            break


def start_background_workers() -> dict:
    _stop_event.clear()
    startup = restore_on_startup()
    _set_state("startup", **startup)
    _set_state("links", enabled=settings.sanitizer_enabled, running=False, last_error="")
    _set_state("eligible", enabled=settings.eligible_enabled, running=False, last_error="")
    _set_state("scanner", enabled=True, running=False, last_error="")

    workers = {
        "sync": _run_sync_loop,
        "scanner": _run_scanner_loop,
    }
    if settings.sanitizer_enabled:
        workers["links"] = _run_link_loop
    if settings.eligible_enabled:
        workers["eligible"] = _run_eligible_loop
    if settings.rss_enabled:
        workers["rss"] = _run_rss_loop

    started: list[str] = []
    for name, target in workers.items():
        thread = _threads.get(name)
        if thread and thread.is_alive():
            continue
        thread = threading.Thread(target=target, name=f"awc-{name}", daemon=True)
        _threads[name] = thread
        thread.start()
        started.append(name)

    log_block(
        logger,
        logging.INFO,
        "Background workers started",
        [f"workers={', '.join(started) if started else 'none'}"],
        event_type="runtime.workers.started",
        details={"workers": started},
    )
    return {"started": started, "startup": startup}


def stop_background_workers() -> None:
    _stop_event.set()
    for thread in list(_threads.values()):
        thread.join(timeout=1)
    log_info(logger, "runtime.workers.stopped", "Background workers stopped")

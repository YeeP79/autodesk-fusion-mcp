"""Thread-safe callback relay for executing work on the Fusion main thread."""

import queue
import threading
import time
import traceback
from datetime import datetime

import adsk.core

from .. import settings
from ..lib import fusionAddInUtils as futil

# ── Module state ──────────────────────────────────────────────────────────

_callback_impl = None
_pending = queue.Queue()
_callback_event = None
_scheduler_active = threading.Event()
_halt = threading.Event()
_run_lock = threading.Lock()

_deferred_messages = []
_msg_lock = threading.Lock()
_registered = False

CALLBACK_EVENT_ID = "FusionBridgeCallback"
_TICK_INTERVAL = 0.075
_KEEPALIVE_INTERVAL = 2.0
_BATCH_LIMIT = 8
_READY_PING = "__startup_ping__"
_PROBE_POLL_INTERVAL = 0.05
# A probe the main thread already dequeued is answered within microseconds.
_PROBE_DEQUEUED_GRACE = 0.25


# ── Fusion application accessor ──────────────────────────────────────────


def get_app():
    return adsk.core.Application.get()


# ── Thread-safe logging ──────────────────────────────────────────────────


def format_duration(elapsed_ms):
    """Human-readable elapsed time.

    Under one second -> integer milliseconds (e.g. ``122ms``);
    one second or longer -> seconds with two decimals (e.g. ``4.52s``).
    """
    if elapsed_ms < 1000:
        return f"{elapsed_ms:.0f}ms"
    return f"{elapsed_ms / 1000:.2f}s"


def log(message: str, level=None):
    """Log immediately on the main thread, or defer for later flushing.

    The message is timestamped at call time so deferred (background-thread)
    lines reflect when they were logged, not when they were flushed.
    """
    stamped = f"{datetime.now().isoformat(timespec='milliseconds')} {message}"
    if threading.current_thread() is threading.main_thread():
        futil.log(stamped) if level is None else futil.log(stamped, level)
        return
    with _msg_lock:
        _deferred_messages.append((stamped, level))


def drain_logs():
    """Flush any log messages that were deferred from background threads."""
    with _msg_lock:
        if not _deferred_messages:
            return
        snapshot = list(_deferred_messages)
        _deferred_messages.clear()
    for text, lvl in snapshot:
        futil.log(text) if lvl is None else futil.log(text, lvl)


# ── Custom-event handler ─────────────────────────────────────────────────


class _BridgeEventHandler(adsk.core.CustomEventHandler):
    """Responds to the custom Fusion event by draining the work queue."""

    def __init__(self):
        super().__init__()

    def notify(self, args):
        del args
        _flush_pending()


# ── Recursive timer scheduler ────────────────────────────────────────────


def _schedule_tick(shutdown=None):
    """Fire a single tick, then reschedule if still active."""
    shutdown = shutdown if shutdown is not None else _halt
    if shutdown.is_set() or not _scheduler_active.is_set():
        return
    try:
        _fire_event_if_needed()
    except Exception as exc:
        log(f"Scheduler tick error: {exc}", adsk.core.LogLevels.ErrorLogLevel)
    if _scheduler_active.is_set() and not shutdown.is_set():
        t = threading.Timer(_TICK_INTERVAL, _schedule_tick, args=(shutdown,))
        t.daemon = True
        t.start()


_last_fire_time = 0.0


def _fire_event_if_needed():
    """Signal the main thread when there is queued work or a keepalive is due."""
    import time

    global _last_fire_time
    now = time.time()
    has_work = not _pending.empty()
    keepalive_due = (now - _last_fire_time) >= _KEEPALIVE_INTERVAL
    if has_work or keepalive_due:
        get_app().fireCustomEvent(CALLBACK_EVENT_ID)
        _last_fire_time = now


# ── Public dispatch API ──────────────────────────────────────────────────

# Envelope lifecycle: queued -> running -> done, or queued -> cancelled.
# Each envelope carries its own ``_lock`` so the submitting thread (on
# timeout) and the Fusion main thread (in _flush_pending) transition the
# state atomically.  Lock ordering: ``_lock`` -> queue mutex only; the
# _inflight lock is never held while taking ``_lock``.

_QUEUED_CANCEL_TEXT = (
    "Error: Request cancelled: the tool was queued but never executed "
    "(Fusion main thread did not service it). Safe to retry."
)
_RUNNING_TIMEOUT_TEXT = (
    "Error: Timeout: the tool is still executing on the Fusion main thread "
    "and may complete. Do NOT blindly retry — retrying could duplicate "
    "the operation."
)

_CANCELLED_TEXT = (
    "Error: Request cancelled; work that already started may still complete."
)

# Server/session/request key -> envelope; never key by JSON-RPC ID alone.
_inflight = {}
_inflight_lock = threading.Lock()

# (tool name, monotonic start) of the call executing on the main thread now.
# Written only by _flush_pending and replaced whole, so readers need no lock.
_running_tool = None


def _text_result(text):
    return {
        "content": [{"type": "text", "text": text}],
        "isError": True,
    }


def _new_envelope(call_data, reply):
    return {
        "payload": call_data,
        "reply": reply,
        "_lock": threading.Lock(),
        "_state": "queued",
    }


def _put_reply(reply, result):
    try:
        reply.put_nowait(result)
    except queue.Full:
        pass


def _deregister_inflight(envelope):
    request_key = envelope.get("_request_key")
    if request_key is None:
        return
    with _inflight_lock:
        if _inflight.get(request_key) is envelope:
            del _inflight[request_key]


def set_tool_handler(handler):
    global _callback_impl
    _callback_impl = handler


def cancel_request(request_key):
    """Cancel a queued request before the Fusion main thread executes it.

    Returns ``True`` when the request was still queued and has been
    cancelled; ``False`` when it is unknown, already executing, or already
    finished.  Fusion work that is underway cannot be aborted, so a
    ``False`` return means the caller must treat the operation as possibly
    completing anyway.
    """
    with _inflight_lock:
        envelope = _inflight.get(request_key)
    if envelope is None:
        return False

    with envelope["_lock"]:
        if envelope["_state"] != "queued":
            return False
        if not _try_remove(envelope):
            # Lost the race with _flush_pending: the work is executing.
            return False
        envelope["_state"] = "cancelled"

    _deregister_inflight(envelope)
    # Wake the thread blocked in dispatch_to_main_thread with the answer.
    _put_reply(envelope["reply"], _text_result(_QUEUED_CANCEL_TEXT))
    return True


def dispatch_to_main_thread(call_data):
    """Submit *call_data* for execution on the Fusion main thread and block
    until the result is available.  If already on the main thread, execute
    directly."""
    cancel_event = (
        call_data.get("_cancel_event") if isinstance(call_data, dict) else None
    )
    shutdown = _halt
    if shutdown.is_set() or (cancel_event is not None and cancel_event.is_set()):
        return _text_result(_CANCELLED_TEXT)
    if threading.current_thread() is threading.main_thread():
        if _callback_impl is None:
            raise RuntimeError("Tool implementation is not initialized")
        return _callback_impl(call_data)

    reply = queue.Queue(maxsize=1)
    envelope = _new_envelope(call_data, reply)
    envelope["_shutdown"] = shutdown

    request_key = (
        call_data.get("_request_key") if isinstance(call_data, dict) else None
    )
    if request_key is not None:
        envelope["_request_key"] = request_key
        with _inflight_lock:
            if request_key in _inflight:
                return _text_result("Error: Request ID is already in flight in this session")
            _inflight[request_key] = envelope
            # Publish the envelope before cancellation can find it.
            _pending.put(envelope)
    else:
        _pending.put(envelope)

    try:
        get_app().fireCustomEvent(CALLBACK_EVENT_ID)
    except Exception as exc:
        with envelope["_lock"]:
            if envelope["_state"] == "queued" and _try_remove(envelope):
                envelope["_state"] = "cancelled"
        _deregister_inflight(envelope)
        log(
            f"Failed to fire main-thread event: {exc}",
            adsk.core.LogLevels.ErrorLogLevel,
        )
        return _text_result(
            f"Error: RuntimeError: failed to schedule Fusion main-thread "
            f"work ({exc})"
        )

    deadline = time.monotonic() + settings.MCP_MAIN_THREAD_TIMEOUT
    while True:
        if shutdown.is_set() or (cancel_event is not None and cancel_event.is_set()):
            if cancel_event is not None:
                cancel_event.set()
            with envelope["_lock"]:
                if envelope["_state"] == "queued":
                    _try_remove(envelope)
                    envelope["_state"] = "cancelled"
            _deregister_inflight(envelope)
            return _text_result(_CANCELLED_TEXT)
        try:
            return reply.get(timeout=0.05)
        except queue.Empty:
            pass
        if time.monotonic() >= deadline:
            result, cancelled_flag = _classify_timeout(envelope)
            if cancelled_flag:
                _deregister_inflight(envelope)
            log(
                f"Timed out waiting for the Fusion main thread ({settings.MCP_MAIN_THREAD_TIMEOUT:.0f}s); "
                + ("request cancelled before execution" if cancelled_flag
                   else "request may still be executing"),
                adsk.core.LogLevels.ErrorLogLevel,
            )
            return result


def _classify_timeout(envelope):
    """Build the client-facing answer for a timed-out request.

    Distinguishes work that was still queued (cancelled for real, safe to
    retry) from work already executing on the Fusion main thread (cannot
    be aborted; the client is told it may still complete and must not
    blindly retry).
    """
    with envelope["_lock"]:
        if envelope["_state"] == "queued":
            if _try_remove(envelope):
                envelope["_state"] = "cancelled"
                return _text_result(_QUEUED_CANCEL_TEXT), True
            # Raced with _flush_pending: it already dequeued the envelope
            # and is waiting on this lock, so the work is executing.
            envelope["_state"] = "running"

        state = envelope["_state"]
        if state == "done":
            try:
                return envelope["reply"].get_nowait(), False
            except queue.Empty:
                state = "running"
        if state == "cancelled":
            # A concurrent cancel_request() already produced the answer.
            try:
                return envelope["reply"].get_nowait(), False
            except queue.Empty:
                return _text_result(_QUEUED_CANCEL_TEXT), True
        return _text_result(_RUNNING_TIMEOUT_TEXT), False


def wait_for_main_thread(poll_interval=1.0, shutdown=None):
    """Block until the Fusion main thread services a work item.

    Enqueues a lightweight ``__startup_ping__`` envelope and waits for it
    to round-trip through the Custom Event dispatcher.  Used to defer
    startup work until Fusion's event loop is actually pumping, which is
    not yet the case while an auto-loaded add-in's ``run()`` executes
    during Fusion's own cold start.

    Only call from background threads: the main thread cannot service the
    queue while blocked inside an add-in callback, so waiting here from
    ``run()`` would deadlock.  Returns ``True`` once the main thread is
    live, ``False`` if *shutdown* (a ``threading.Event``) is set before
    that happens.
    """
    if threading.current_thread() is threading.main_thread():
        return True

    while shutdown is None or not shutdown.is_set():
        reply = queue.Queue(maxsize=1)
        envelope = _new_envelope(
            {"params": {"name": _READY_PING}}, reply
        )
        envelope["_shutdown"] = shutdown
        _pending.put(envelope)

        try:
            get_app().fireCustomEvent(CALLBACK_EVENT_ID)
        except Exception:
            # The scheduler's keepalive ticks fire this event too, so a
            # single failure here is not fatal.
            pass

        try:
            result = reply.get(timeout=poll_interval)
        except queue.Empty:
            _try_remove(envelope)
            continue
        _try_remove(envelope)
        if _is_ready_reply(result):
            return True
        # A reply that is not the expected ready answer means the ping was
        # mishandled; keep waiting for a genuine main-thread round-trip.

    return False


def _is_ready_reply(result):
    return (
        isinstance(result, dict)
        and bool(result.get("content"))
        and isinstance(result["content"][0], dict)
        and result["content"][0].get("text") == "ready"
    )


def probe_main_thread(timeout_seconds, cancel_event=None):
    """Time a no-op round trip through the main-thread queue.

    The readiness ping is answered inside ``_flush_pending`` without running
    a tool, so the probe delays real work by nothing measurable, and when
    the wait ends unanswered it is pulled back out of the queue. Queued
    behind real calls it is answered after them, which is the point: the
    answer means the main thread has caught up.

    Deliberately makes no Fusion API call: this runs on the HTTP thread and
    must return while Fusion is busy, so the scheduler tick (which fires
    whenever the queue is non-empty) wakes the main thread instead of
    ``fireCustomEvent`` here. That adds up to ``_TICK_INTERVAL`` of latency.

    Returns ``{"responsive", "latency_ms", "stopped"}``; ``stopped`` is
    ``"shutdown"`` or ``"cancelled"`` when the probe was abandoned early.
    """
    if threading.current_thread() is threading.main_thread():
        return _probe_result(True, 0.0)

    shutdown = _halt
    reply = queue.Queue(maxsize=1)
    envelope = _new_envelope({"params": {"name": _READY_PING}}, reply)
    envelope["_shutdown"] = shutdown
    started = time.monotonic()
    deadline = started + timeout_seconds
    _pending.put(envelope)

    stopped = None
    while True:
        if shutdown.is_set():
            stopped = "shutdown"
            break
        if cancel_event is not None and cancel_event.is_set():
            stopped = "cancelled"
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            answer = reply.get(timeout=min(_PROBE_POLL_INTERVAL, remaining))
        except queue.Empty:
            continue
        return _probe_answer(answer, started)

    with envelope["_lock"]:
        removed = _try_remove(envelope)
        if removed:
            envelope["_state"] = "cancelled"
    if not removed:
        # Lost the race with _flush_pending or the shutdown drain: the
        # envelope has left the queue and its answer is on the way.
        try:
            return _probe_answer(reply.get(timeout=_PROBE_DEQUEUED_GRACE), started)
        except queue.Empty:
            pass
    return _probe_result(False, None, stopped)


def _probe_answer(answer, started):
    if _is_ready_reply(answer):
        return _probe_result(True, (time.monotonic() - started) * 1000)
    # Only the shutdown drain answers a probe with anything but "ready".
    return _probe_result(False, None, "shutdown")


def _probe_result(responsive, latency_ms, stopped=None):
    return {"responsive": responsive, "latency_ms": latency_ms, "stopped": stopped}


def running_tool():
    """Name and elapsed seconds of the tool on the main thread, or ``None``.

    Stays set after the client's own wait timed out, because the work is
    still running then.
    """
    current = _running_tool
    if current is None:
        return None
    name, started = current
    return {"name": name, "running_seconds": round(time.monotonic() - started, 1)}


def queued_call_count():
    """Tool calls waiting for the main thread, not counting probes."""
    with _pending.mutex:
        return sum(
            1 for envelope in _pending.queue
            if _payload_tool_name(envelope["payload"]) != _READY_PING
        )


# ── Internal helpers ─────────────────────────────────────────────────────


def _payload_tool_name(payload):
    params = payload.get("params") if isinstance(payload, dict) else None
    return params.get("name") if isinstance(params, dict) else None


def _try_remove(envelope):
    """Best-effort removal of an unprocessed envelope from the queue."""
    with _pending.mutex:
        try:
            _pending.queue.remove(envelope)
            return True
        except ValueError:
            return False


def _flush_pending():
    """Process up to *_BATCH_LIMIT* envelopes from the queue."""
    global _running_tool

    if not _run_lock.acquire(blocking=False):
        return
    try:
        drain_logs()
        processed = 0
        while processed < _BATCH_LIMIT:
            try:
                envelope = _pending.get_nowait()
            except queue.Empty:
                break

            payload = envelope["payload"]
            reply = envelope["reply"]
            shutdown = envelope.get("_shutdown")
            if shutdown is not None and shutdown.is_set():
                with envelope["_lock"]:
                    envelope["_state"] = "cancelled"
                _deregister_inflight(envelope)
                _put_reply(reply, _text_result(_QUEUED_CANCEL_TEXT))
                processed += 1
                continue

            # The readiness ping is answered here, before any tool routing,
            # so it succeeds even while the tool handler is not installed
            # (cold start, add-in not fully running).
            if _payload_tool_name(payload) == _READY_PING:
                with envelope["_lock"]:
                    envelope["_state"] = "done"
                _put_reply(
                    reply, {"content": [{"type": "text", "text": "ready"}]}
                )
                processed += 1
                continue

            # Honour the upstream v1.4.0 `_cancel_event` so a queued request
            # that was cancelled / timed out by the protocol layer is never
            # executed.
            cancel_ev = (
                payload.get("_cancel_event") if isinstance(payload, dict) else None
            )
            if cancel_ev is not None and (cancel_ev.is_set() or _halt.is_set()):
                with envelope["_lock"]:
                    envelope["_state"] = "done"
                _deregister_inflight(envelope)
                _put_reply(reply, _text_result(_CANCELLED_TEXT))
                processed += 1
                continue

            with envelope["_lock"]:
                if envelope["_state"] == "cancelled":
                    envelope["_state"] = "done"
                    cancelled = True
                else:
                    envelope["_state"] = "running"
                    cancelled = False

            if cancelled:
                _deregister_inflight(envelope)
                _put_reply(reply, _text_result(_QUEUED_CANCEL_TEXT))
                processed += 1
                continue

            _running_tool = (_payload_tool_name(payload), time.monotonic())
            try:
                if _callback_impl is None:
                    raise RuntimeError("Tool implementation is not initialized")
                result = _callback_impl(payload)
            except Exception as exc:
                tb = traceback.format_exc()
                log(
                    f"Main-thread work item failed: {exc}",
                    adsk.core.LogLevels.ErrorLogLevel,
                )
                log(tb, adsk.core.LogLevels.ErrorLogLevel)
                result = {
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                f"Error: {type(exc).__name__}: {exc}\n"
                                "Call: _flush_pending()\n"
                                f"Traceback:\n{tb}"
                            ),
                        }
                    ],
                    "isError": True,
                }
            finally:
                _running_tool = None

            with envelope["_lock"]:
                envelope["_state"] = "done"
            _deregister_inflight(envelope)
            _put_reply(reply, result)
            processed += 1

        drain_logs()
    finally:
        _run_lock.release()


# ── Lifecycle ─────────────────────────────────────────────────────────────


def init_main_thread_dispatch():
    global _callback_event, _registered, _halt

    if _registered:
        raise RuntimeError("Main-thread dispatch is already initialized")

    # A stopped generation stays stopped, even after a fast restart.
    _halt = threading.Event()
    _scheduler_active.clear()
    _callback_event = get_app().registerCustomEvent(CALLBACK_EVENT_ID)
    handler = _BridgeEventHandler()
    _callback_event.add(handler)
    # Store handler on the event object to prevent GC
    _callback_event._bridge_handler = handler
    _registered = True

    _scheduler_active.set()
    _schedule_tick()


def request_main_thread_shutdown():
    """Signal shutdown from any thread; no Fusion API calls here."""
    _halt.set()
    _scheduler_active.clear()
    while True:
        try:
            envelope = _pending.get_nowait()
        except queue.Empty:
            break
        with envelope["_lock"]:
            envelope["_state"] = "cancelled"
        _deregister_inflight(envelope)
        _put_reply(envelope["reply"], _text_result(_QUEUED_CANCEL_TEXT))


def stop_main_thread_dispatch():
    global _callback_event, _registered

    request_main_thread_shutdown()

    if _callback_event and hasattr(_callback_event, "_bridge_handler"):
        try:
            _callback_event.remove(_callback_event._bridge_handler)
        except Exception as exc:
            log(
                f"Error removing event handler: {exc}",
                adsk.core.LogLevels.WarningLogLevel,
            )

    app = None
    try:
        app = get_app()
    except Exception:
        app = None

    if app and _registered and hasattr(app, "unregisterCustomEvent"):
        try:
            app.unregisterCustomEvent(CALLBACK_EVENT_ID)
        except Exception as exc:
            log(
                f"Error unregistering custom event: {exc}",
                adsk.core.LogLevels.WarningLogLevel,
            )

    _registered = False
    _callback_event = None


def get_shutdown_flag():
    return _halt

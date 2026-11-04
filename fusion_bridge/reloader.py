"""In-process restart of the add-in after its code changed on disk.

Stopping the add-in kills the HTTP server that received the request, and
nothing outside Fusion can start an add-in again (Fusion has no CLI or API
for it; the Scripts and Add-Ins dialog is UI only). So the reload happens
inside Fusion's Python process instead:

1. ``reload_addin`` replies at once and starts a worker thread.
2. The worker waits until the server has written every pending reply, then
   fires a private custom event so the rest runs on Fusion's main thread.
3. On the main thread: the same shutdown Fusion's ``stop()`` performs, purge
   the add-in's modules from ``sys.modules``, re-import, preflight-import the
   tool registry, and the same ``startup()`` Fusion's ``run()`` performs.
4. The worker waits for the new server to bind. If the new code fails at any
   point, the previous in-memory modules are put back and started again.

The client is disconnected meanwhile, so progress and failures go to the
Fusion log and the Text Commands window.
"""

import importlib
import json
import os
import sys
import threading
import time
import traceback
import uuid

import adsk.core

from .dispatch import get_app

# Bumped for each successful reload; carried into the fresh module.
GENERATION = 1
# Outcome of the reload that produced this generation (or the last failed one).
LAST_RELOAD = None

RECONNECT_AFTER_SECONDS = 5
_IDLE_TIMEOUT = 10.0
_MAIN_THREAD_TIMEOUT = 120.0
_BIND_TIMEOUT = 60.0
_BIND_POLL = 0.25
_ENTRY_FILE = "AutodeskFusionMCP.py"
_SUFFIX = ".fusion_bridge.reloader"

# Reload events of earlier generations. An event is never unregistered from
# inside its own handler; the next shutdown of a later generation does it.
_stale_events = []
_job_lock = threading.Lock()
_active_job = None


# -- Pure helpers -------------------------------------------------------------


def package_root(module_name=__name__):
    """``<root>`` for ``<root>.fusion_bridge.reloader``; ``""`` otherwise."""
    if module_name.endswith(_SUFFIX):
        return module_name[: -len(_SUFFIX)]
    return ""


def _module_file(module):
    path = getattr(module, "__file__", None)
    return os.path.abspath(path) if path else None


def keep_names(modules, root, entry_path):
    """Modules Fusion itself holds and that must survive the purge.

    The root package (relative imports resolve through it) and the entry
    module Fusion will later call ``stop()`` on. The entry is matched by
    file, since the name Fusion gives it is not guaranteed.
    """
    entry_path = os.path.abspath(entry_path)
    kept = {root}
    prefix = root + "."
    for name, module in list(modules.items()):
        if name.startswith(prefix) and _module_file(module) == entry_path:
            kept.add(name)
    return kept


def select_purge(modules, root, keep):
    """Names of the add-in's modules to drop so they are imported afresh.

    Only names inside the add-in package are ever selected, so other
    add-ins and the standard library are untouched.
    """
    if not root:
        return []
    prefix = root + "."
    return sorted(
        name for name in list(modules)
        if name.startswith(prefix) and name not in keep
    )


def purge(modules, names):
    """Remove *names* from *modules*; return ``{name: module}`` removed.

    Also deletes the matching attribute on any surviving parent package:
    ``from . import commands`` returns the parent's attribute when it
    exists, so without this the fresh code would silently import the stale
    module.
    """
    removed = {}
    for name in names:
        if name in modules:
            removed[name] = modules.pop(name)
    for name, module in removed.items():
        parent_name, _, child = name.rpartition(".")
        parent = modules.get(parent_name)
        if parent is not None and getattr(parent, child, None) is module:
            try:
                delattr(parent, child)
            except AttributeError:
                pass
    return removed


def restore(modules, snapshot, root, keep):
    """Put the *snapshot* modules back, dropping any half-imported new ones."""
    prefix = root + "."
    for name in [n for n in list(modules) if n.startswith(prefix) and n not in keep]:
        modules.pop(name, None)
    modules.update(snapshot)
    for name, module in snapshot.items():
        parent_name, _, child = name.rpartition(".")
        parent = modules.get(parent_name)
        if parent is not None and parent_name not in snapshot:
            setattr(parent, child, module)


def _error_text(exc):
    return f"{type(exc).__name__}: {exc}"


# -- Main-thread steps --------------------------------------------------------


def reload_in_place(modules, root, keep, import_module, log):
    """Stop, purge, re-import and start. Runs on the Fusion main thread.

    Returns ``(status, snapshot, error)`` where status is ``"reloaded"``,
    ``"restored"`` (new code failed, previous runtime started again) or
    ``"failed"`` (the previous runtime could not be started either).
    """
    old_runtime = modules[root + ".addon_runtime"]
    try:
        old_runtime.shutdown()
    except Exception as exc:
        log(f"[reload] shutdown failed: {_error_text(exc)}\n{traceback.format_exc()}", True)
        return _restart_old(old_runtime, {}, _error_text(exc), log)

    snapshot = purge(modules, select_purge(modules, root, keep))
    try:
        importlib.invalidate_caches()
        new_runtime = import_module(root + ".addon_runtime")
        # operations is otherwise imported lazily on the server thread;
        # importing it here surfaces syntax and registry errors while the
        # previous runtime can still be put back.
        import_module(root + ".fusion_bridge.operations")
        if not new_runtime.startup():
            raise RuntimeError("startup() of the new code returned False")
    except Exception as exc:
        log(f"[reload] new code failed: {_error_text(exc)}\n{traceback.format_exc()}", True)
        return fall_back(modules, root, keep, snapshot, _error_text(exc), log)
    return "reloaded", snapshot, None


def fall_back(modules, root, keep, snapshot, error, log):
    """Stop whatever the new code started and start the previous runtime."""
    old_runtime = snapshot.get(root + ".addon_runtime")
    new_runtime = modules.get(root + ".addon_runtime")
    if new_runtime is not None and new_runtime is not old_runtime:
        try:
            new_runtime.shutdown()
        except Exception as exc:
            log(f"[reload] cleanup of new code failed: {_error_text(exc)}", True)
    restore(modules, snapshot, root, keep)
    if old_runtime is None:
        return "failed", snapshot, error
    return _restart_old(old_runtime, snapshot, error, log)


def _restart_old(old_runtime, snapshot, error, log):
    try:
        if old_runtime.startup():
            log("[reload] previous runtime started again", True)
            return "restored", snapshot, error
        detail = "startup() returned False"
    except Exception as exc:
        detail = _error_text(exc)
        log(traceback.format_exc(), True)
    log(f"[reload] previous runtime could not start: {detail}. "
        "Use Utilities > Add-Ins > Run.", True)
    return "failed", snapshot, f"{error}; restart of previous runtime failed: {detail}"


def await_bind(modules, root, timeout, sleep=time.sleep, clock=time.monotonic):
    """Wait for the current generation's server: ``bound``/``failed``/``timeout``."""
    deadline = clock() + timeout
    while True:
        runtime = modules.get(root + ".fusion_bridge.runtime")
        server = getattr(runtime, "_server", None)
        if server is not None and getattr(server, "is_running", False):
            return "bound"
        dispatch = modules.get(root + ".fusion_bridge.dispatch")
        try:
            halted = dispatch.get_shutdown_flag().is_set()
        except Exception:
            halted = False
        if halted:
            return "failed"
        if clock() >= deadline:
            return "timeout"
        sleep(_BIND_POLL)


# -- Orchestration ------------------------------------------------------------


class ReloadJob:
    """Reply-then-reload sequencing; collaborators are injected for tests.

    *post_to_main* must arrange for ``run_posted()`` to be called on the
    Fusion main thread (in Fusion: fire this job's custom event).
    """

    def __init__(self, *, modules, root, keep, wait_idle, post_to_main,
                 import_module=importlib.import_module, log=None,
                 bind_timeout=_BIND_TIMEOUT, sleep=time.sleep, on_finish=None,
                 home=None, generation=None):
        self.modules = modules
        self.root = root
        self.keep = keep
        self.wait_idle = wait_idle
        self.post_to_main = post_to_main
        self.import_module = import_module
        self.log = log or self._default_log
        self.bind_timeout = bind_timeout
        self.sleep = sleep
        self.on_finish = on_finish
        # The reloader module that scheduled this job (the old generation).
        self.home = home if home is not None else sys.modules.get(__name__)
        self.generation = GENERATION if generation is None else generation
        self.status = "scheduled"
        self.error = None
        self._task = None
        self._task_lock = threading.Lock()
        # Used while the purge has left no dispatcher in sys.modules.
        self._home_dispatch = modules.get(root + ".fusion_bridge.dispatch")

    def _default_log(self, message, error=False):
        # Resolve the dispatcher of whichever generation is live now: the
        # old one stops draining deferred logs once it is shut down.
        dispatch = self.modules.get(self.root + ".fusion_bridge.dispatch") or self._home_dispatch
        level = adsk.core.LogLevels.ErrorLogLevel if error else None
        try:
            dispatch.log(message, level)
        except Exception:
            print(message)

    def run_posted(self):
        """Main-thread entry: run the task the worker posted, if any."""
        with self._task_lock:
            task, self._task = self._task, None
        if task is not None:
            task()

    def _on_main(self, fn):
        done = threading.Event()
        box = {}

        def task():
            try:
                box["result"] = fn()
            except Exception as exc:
                box["error"] = exc
            finally:
                done.set()

        with self._task_lock:
            self._task = task
        self.post_to_main()
        if not done.wait(_MAIN_THREAD_TIMEOUT):
            raise TimeoutError("Fusion main thread did not run the reload")
        if "error" in box:
            raise box["error"]
        return box["result"]

    def run(self):
        """Worker-thread body: wait for the reply to flush, then reload."""
        try:
            if not self.wait_idle(_IDLE_TIMEOUT):
                self.log("[reload] requests still in flight; reloading anyway")
            self.log("[reload] reloading add-in code from disk...")
            status, snapshot, error = self._on_main(self._reload_step)
            if status == "reloaded":
                bind = await_bind(self.modules, self.root, self.bind_timeout, self.sleep)
                if bind == "failed":
                    status, snapshot, error = self._on_main(
                        lambda: fall_back(self.modules, self.root, self.keep, snapshot,
                                          "new MCP server failed to start", self.log)
                    )
                elif bind == "timeout":
                    self.log(f"[reload] new MCP server not listening after "
                             f"{self.bind_timeout:.0f}s; it keeps retrying", True)
            self.status, self.error = status, error
        except Exception as exc:
            self.status, self.error = "failed", _error_text(exc)
            self.log(f"[reload] aborted: {self.error}\n{traceback.format_exc()}", True)
        finally:
            self._record()
            if self.on_finish is not None:
                self.on_finish(self)

    def _reload_step(self):
        outcome = reload_in_place(self.modules, self.root, self.keep,
                                  self.import_module, self.log)
        if outcome[0] == "reloaded":
            # Hand bookkeeping to the fresh module before anyone can ask it.
            self._record(status="reloaded", announce=False)
        return outcome

    def _record(self, status=None, announce=True):
        """Store the outcome on whichever reloader module is live now."""
        status = status or self.status
        target = self.modules.get(self.root + _SUFFIX) or self.home
        if target is None:
            return
        target.GENERATION = self.generation + (0 if target is self.home else 1)
        target.LAST_RELOAD = {
            "status": status,
            "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "from_generation": self.generation,
            "error": self.error,
        }
        if not announce:
            return
        if status == "reloaded":
            self.log(f"[reload] OK: generation {target.GENERATION} running")
        else:
            self.log(f"[reload] {status}: {self.error}", True)


# -- Fusion wiring ------------------------------------------------------------


class _ReloadEventHandler(adsk.core.CustomEventHandler):
    def __init__(self, job):
        super().__init__()
        self.job = job

    def notify(self, args):
        del args
        self.job.run_posted()


def release_stale_events():
    """Unregister reload events left by earlier generations (main thread)."""
    app = get_app()
    while _stale_events:
        event_id, event, handler = _stale_events.pop()
        try:
            event.remove(handler)
            app.unregisterCustomEvent(event_id)
        except Exception:
            pass


def _text(payload, is_error=False):
    body = payload if isinstance(payload, str) else json.dumps(payload, indent=2)
    return {"content": [{"type": "text", "text": body}], "isError": is_error}


def _hand_over_event(job, event_id, event, handler):
    target = job.modules.get(job.root + _SUFFIX) or job.home
    if target is not None:
        target._stale_events.append((event_id, event, handler))


def reload_addin(arguments):
    """Schedule an in-process reload; replies before anything is torn down."""
    global _active_job
    del arguments
    modules = sys.modules
    root = package_root()
    if not root or root not in modules or root + ".addon_runtime" not in modules:
        return _text("Error: cannot reload: the add-in package was not identified "
                     f"(module {__name__!r}). Restart it from Utilities > Add-Ins.", True)

    with _job_lock:
        if _active_job is not None:
            return _text("Error: a reload is already in progress; wait and call bridge_info.", True)

        from . import bridge_info, python_exec, runtime

        before = bridge_info.collect()
        entry = os.path.join(python_exec.get_addin_dir(), _ENTRY_FILE)
        keep = keep_names(modules, root, entry)
        server = runtime._server
        wait_idle = server.wait_until_idle if server is not None else (lambda timeout: True)

        app = get_app()
        event_id = f"FusionBridgeReload-{uuid.uuid4().hex[:8]}"
        event = app.registerCustomEvent(event_id)

        def finished(job):
            global _active_job
            _hand_over_event(job, event_id, event, handler)
            with _job_lock:
                _active_job = None

        job = ReloadJob(modules=modules, root=root, keep=keep, wait_idle=wait_idle,
                        post_to_main=lambda: get_app().fireCustomEvent(event_id),
                        on_finish=finished)
        handler = _ReloadEventHandler(job)
        event.add(handler)
        _active_job = job
        threading.Thread(target=job.run, name="AutodeskFusionMCP-reload", daemon=True).start()

    return _text({
        "status": "reload scheduled",
        "reconnect_after_seconds": RECONNECT_AFTER_SECONDS,
        "next_step": (
            "The connection drops while the add-in restarts. Wait, then call bridge_info: "
            "running.generation should be one higher and running.last_reload.status 'reloaded'. "
            "'restored' means the new code failed and the previous code is running again; "
            "the error is in last_reload and the Fusion Text Commands window."
        ),
        "note": "Session state is reset: stored $objects, execute_python sessions and annotations.",
        "before": {
            "running": before["running"],
            "disk": before["disk"],
            "up_to_date": before["up_to_date"],
        },
    })

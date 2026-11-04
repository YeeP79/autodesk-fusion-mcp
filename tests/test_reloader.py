"""Tests for in-process add-in reload: module purge selection, the
stop/purge/re-import/start sequence with fallback, and reply-then-reload
ordering. Fusion and the real add-in modules are replaced by fakes."""

import _fusion_test_bootstrap  # noqa: F401  (installs adsk mock + parent pkg shim)

import http.client
import json
import os
import tempfile
import threading
import time
import types
import unittest

from fusion_bridge import bridge_info, reloader
from lib.mcp_server import MCPServer

ROOT = "_pkg_a1b2"
ENTRY = os.path.abspath("/addin/AutodeskFusionMCP.py")


def _mod(name, path=None):
    module = types.ModuleType(name)
    if path:
        module.__file__ = path
    return module


class _FakeRuntime(types.ModuleType):
    """Stands in for ``addon_runtime``; records calls into a shared list."""

    def __init__(self, name, calls, tag, start_ok=True, start_exc=None, stop_exc=None):
        super().__init__(name)
        self.calls, self.tag = calls, tag
        self.start_ok, self.start_exc, self.stop_exc = start_ok, start_exc, stop_exc

    def startup(self):
        self.calls.append(f"{self.tag}.startup")
        if self.start_exc:
            raise self.start_exc
        return self.start_ok

    def shutdown(self):
        self.calls.append(f"{self.tag}.shutdown")
        if self.stop_exc:
            raise self.stop_exc


def _fake_modules(calls, **old_kwargs):
    root = _mod(ROOT, "/addin/__init__.py")
    old_rt = _FakeRuntime(f"{ROOT}.addon_runtime", calls, "old", **old_kwargs)
    commands = _mod(f"{ROOT}.commands", "/addin/commands/__init__.py")
    root.addon_runtime, root.commands = old_rt, commands
    modules = {
        ROOT: root,
        f"{ROOT}.AutodeskFusionMCP": _mod(f"{ROOT}.AutodeskFusionMCP", ENTRY),
        f"{ROOT}.addon_runtime": old_rt,
        f"{ROOT}.commands": commands,
        f"{ROOT}.fusion_bridge": _mod(f"{ROOT}.fusion_bridge", "/addin/fusion_bridge/__init__.py"),
        f"{ROOT}.fusion_bridge.dispatch": _mod(f"{ROOT}.fusion_bridge.dispatch", "/addin/fusion_bridge/dispatch.py"),
        f"{ROOT}x.other": _mod(f"{ROOT}x.other", "/other/other.py"),
        "json": json,
        "fusion_bridge": _mod("fusion_bridge"),
    }
    return modules, old_rt


def _importer(modules, calls, **new_kwargs):
    """Fake import_module that builds fresh modules like a real re-import."""
    state = {}

    def import_module(name):
        calls.append(f"import {name}")
        if name in new_kwargs.get("fail_on", ()):
            modules[name] = _mod(name)  # half-imported leftovers
            raise SyntaxError(f"bad code in {name}")
        if name == f"{ROOT}.addon_runtime":
            module = _FakeRuntime(name, calls, "new", **{k: v for k, v in new_kwargs.items() if k != "fail_on"})
            state["runtime"] = module
        else:
            module = _mod(name)
        modules[name] = module
        setattr(modules[ROOT], name.rpartition(".")[2], module)
        return module

    return import_module, state


def _quiet(*args, **kwargs):
    pass


class PurgeSelectionTests(unittest.TestCase):
    def test_package_root(self):
        self.assertEqual(reloader.package_root(f"{ROOT}.fusion_bridge.reloader"), ROOT)
        self.assertEqual(reloader.package_root("fusion_bridge.reloader"), "")
        self.assertEqual(bridge_info.package_root(f"{ROOT}.fusion_bridge.bridge_info"), ROOT)
        self.assertEqual(bridge_info.package_root("bridge_info"), "")

    def test_keep_root_and_entry_by_file(self):
        modules, _ = _fake_modules([])
        self.assertEqual(reloader.keep_names(modules, ROOT, ENTRY),
                         {ROOT, f"{ROOT}.AutodeskFusionMCP"})

    def test_select_only_add_in_package_minus_kept(self):
        modules, _ = _fake_modules([])
        keep = reloader.keep_names(modules, ROOT, ENTRY)
        self.assertEqual(reloader.select_purge(modules, ROOT, keep), [
            f"{ROOT}.addon_runtime",
            f"{ROOT}.commands",
            f"{ROOT}.fusion_bridge",
            f"{ROOT}.fusion_bridge.dispatch",
        ])

    def test_empty_root_selects_nothing(self):
        modules, _ = _fake_modules([])
        self.assertEqual(reloader.select_purge(modules, "", set()), [])

    def test_purge_clears_parent_attributes(self):
        modules, old_rt = _fake_modules([])
        removed = reloader.purge(modules, [f"{ROOT}.addon_runtime", f"{ROOT}.commands"])
        self.assertIs(removed[f"{ROOT}.addon_runtime"], old_rt)
        self.assertNotIn(f"{ROOT}.addon_runtime", modules)
        # ``from . import commands`` would otherwise return the stale module.
        self.assertFalse(hasattr(modules[ROOT], "commands"))
        self.assertFalse(hasattr(modules[ROOT], "addon_runtime"))

    def test_purge_keeps_unrelated_parent_attribute(self):
        modules, _ = _fake_modules([])
        replacement = object()
        modules[ROOT].commands = replacement
        reloader.purge(modules, [f"{ROOT}.commands"])
        self.assertIs(modules[ROOT].commands, replacement)

    def test_restore_drops_new_and_reinstates_old(self):
        modules, old_rt = _fake_modules([])
        keep = reloader.keep_names(modules, ROOT, ENTRY)
        snapshot = reloader.purge(modules, reloader.select_purge(modules, ROOT, keep))
        modules[f"{ROOT}.half_new"] = _mod("half")
        reloader.restore(modules, snapshot, ROOT, keep)
        self.assertNotIn(f"{ROOT}.half_new", modules)
        self.assertIs(modules[f"{ROOT}.addon_runtime"], old_rt)
        self.assertIs(modules[ROOT].addon_runtime, old_rt)
        self.assertIn(f"{ROOT}.AutodeskFusionMCP", modules)


class ReloadInPlaceTests(unittest.TestCase):
    def _run(self, old_kwargs=None, **new_kwargs):
        calls = []
        modules, old_rt = _fake_modules(calls, **(old_kwargs or {}))
        keep = reloader.keep_names(modules, ROOT, ENTRY)
        import_module, state = _importer(modules, calls, **new_kwargs)
        status, _, error = reloader.reload_in_place(modules, ROOT, keep, import_module, _quiet)
        return status, error, calls, modules, old_rt, state

    def test_success_order(self):
        status, error, calls, modules, old_rt, state = self._run()
        self.assertEqual(status, "reloaded")
        self.assertIsNone(error)
        self.assertEqual(calls, [
            "old.shutdown",
            f"import {ROOT}.addon_runtime",
            f"import {ROOT}.fusion_bridge.operations",
            "new.startup",
        ])
        self.assertIs(modules[f"{ROOT}.addon_runtime"], state["runtime"])
        self.assertNotIn(f"{ROOT}.commands", modules)  # purged, not yet re-imported
        self.assertIn("json", modules)
        self.assertIn(f"{ROOT}x.other", modules)

    def test_import_error_restores_previous(self):
        status, error, calls, modules, old_rt, _ = self._run(
            fail_on={f"{ROOT}.fusion_bridge.operations"})
        self.assertEqual(status, "restored")
        self.assertIn("SyntaxError", error)
        self.assertEqual(calls[-2:], ["new.shutdown", "old.startup"])
        self.assertIs(modules[f"{ROOT}.addon_runtime"], old_rt)
        self.assertNotIn(f"{ROOT}.fusion_bridge.operations", modules)
        self.assertIs(modules[ROOT].addon_runtime, old_rt)

    def test_new_startup_false_restores_previous(self):
        status, error, calls, modules, old_rt, _ = self._run(start_ok=False)
        self.assertEqual(status, "restored")
        self.assertEqual(calls[-3:], ["new.startup", "new.shutdown", "old.startup"])
        self.assertIs(modules[f"{ROOT}.addon_runtime"], old_rt)

    def test_old_shutdown_error_restarts_old_without_purge(self):
        status, _, calls, modules, old_rt, _ = self._run(
            old_kwargs={"stop_exc": RuntimeError("boom")})
        self.assertEqual(status, "restored")
        self.assertEqual(calls, ["old.shutdown", "old.startup"])
        self.assertIn(f"{ROOT}.commands", modules)

    def test_both_fail_reports_failed(self):
        status, error, *_ = self._run(old_kwargs={"start_ok": False}, start_exc=ValueError("x"))
        self.assertEqual(status, "failed")
        self.assertIn("restart of previous runtime failed", error)


class AwaitBindTests(unittest.TestCase):
    def _modules(self, running=False, halted=False):
        rt = _mod(f"{ROOT}.fusion_bridge.runtime")
        rt._server = types.SimpleNamespace(is_running=running) if running is not None else None
        flag = threading.Event()
        if halted:
            flag.set()
        dispatch = _mod(f"{ROOT}.fusion_bridge.dispatch")
        dispatch.get_shutdown_flag = lambda: flag
        return {rt.__name__: rt, dispatch.__name__: dispatch}

    def test_bound(self):
        self.assertEqual(reloader.await_bind(self._modules(running=True), ROOT, 1), "bound")

    def test_failed_when_new_generation_halted(self):
        self.assertEqual(reloader.await_bind(self._modules(running=None, halted=True), ROOT, 1), "failed")

    def test_timeout(self):
        now = [0.0]

        def sleep(seconds):
            now[0] += seconds

        result = reloader.await_bind(self._modules(running=False), ROOT, 1,
                                     sleep=sleep, clock=lambda: now[0])
        self.assertEqual(result, "timeout")


class ReloadJobTests(unittest.TestCase):
    """Reply-then-reload sequencing with fake main thread and server."""

    def _job(self, calls, modules, import_module, wait_idle, bind_running=True):
        home = _mod("home_reloader")
        home.GENERATION, home.LAST_RELOAD = 3, None
        modules[f"{ROOT}.fusion_bridge.reloader"] = home

        original = import_module

        def import_and_wire(name):
            module = original(name)
            if name == f"{ROOT}.addon_runtime":
                fresh = _mod(f"{ROOT}.fusion_bridge.reloader")
                fresh.GENERATION, fresh.LAST_RELOAD = 1, None
                modules[fresh.__name__] = fresh
                rt = _mod(f"{ROOT}.fusion_bridge.runtime")
                rt._server = types.SimpleNamespace(is_running=bind_running)
                modules[rt.__name__] = rt
                flag = threading.Event()
                if not bind_running:
                    flag.set()
                dispatch = _mod(f"{ROOT}.fusion_bridge.dispatch")
                dispatch.get_shutdown_flag = lambda: flag
                modules[dispatch.__name__] = dispatch
            return module

        main_threads = []

        def post_to_main():
            calls.append("post")
            t = threading.Thread(target=job.run_posted)
            main_threads.append(t)
            t.start()

        finished = threading.Event()
        job = reloader.ReloadJob(
            modules=modules, root=ROOT, keep=reloader.keep_names(modules, ROOT, ENTRY),
            wait_idle=wait_idle, post_to_main=post_to_main, import_module=import_and_wire,
            log=_quiet, bind_timeout=0.5, sleep=lambda s: time.sleep(0.01),
            on_finish=lambda j: finished.set(), home=home, generation=3,
        )
        return job, home, finished

    def test_nothing_torn_down_until_reply_flushed(self):
        calls = []
        modules, _ = _fake_modules(calls)
        import_module, _ = _importer(modules, calls)
        reply_written = threading.Event()

        def wait_idle(timeout):
            calls.append("wait_idle")
            return reply_written.wait(timeout)

        job, home, finished = self._job(calls, modules, import_module, wait_idle)
        threading.Thread(target=job.run, daemon=True).start()
        time.sleep(0.2)
        self.assertEqual(calls, ["wait_idle"])  # reply still being written
        calls.append("reply flushed")
        reply_written.set()
        self.assertTrue(finished.wait(5))
        self.assertEqual(calls[:4], ["wait_idle", "reply flushed", "post", "old.shutdown"])
        self.assertEqual(job.status, "reloaded")
        fresh = modules[f"{ROOT}.fusion_bridge.reloader"]
        self.assertEqual(fresh.GENERATION, 4)
        self.assertEqual(fresh.LAST_RELOAD["status"], "reloaded")

    def test_bind_failure_falls_back_to_previous(self):
        calls = []
        modules, old_rt = _fake_modules(calls)
        import_module, _ = _importer(modules, calls)
        job, home, finished = self._job(calls, modules, import_module,
                                        lambda timeout: True, bind_running=False)
        job.run()
        self.assertTrue(finished.is_set())
        self.assertEqual(job.status, "restored")
        self.assertEqual(calls[-2:], ["new.shutdown", "old.startup"])
        self.assertIs(modules[f"{ROOT}.addon_runtime"], old_rt)
        self.assertIs(modules[f"{ROOT}.fusion_bridge.reloader"], home)
        self.assertEqual(home.GENERATION, 3)
        self.assertEqual(home.LAST_RELOAD["status"], "restored")

    def test_import_failure_recorded_on_old_generation(self):
        calls = []
        modules, old_rt = _fake_modules(calls)
        import_module, _ = _importer(modules, calls, fail_on={f"{ROOT}.addon_runtime"})
        job, home, _ = self._job(calls, modules, import_module, lambda timeout: True)
        job.run()
        self.assertEqual(job.status, "restored")
        self.assertIn("SyntaxError", home.LAST_RELOAD["error"])
        self.assertIs(modules[f"{ROOT}.addon_runtime"], old_rt)


class ReloadToolTests(unittest.TestCase):
    def test_refuses_without_add_in_package(self):
        result = reloader.reload_addin({})
        self.assertTrue(result["isError"])
        self.assertIn("cannot reload", result["content"][0]["text"])


class BridgeInfoTests(unittest.TestCase):
    def test_loaded_modules_relative_paths(self):
        modules, _ = _fake_modules([])
        rows = dict(bridge_info.loaded_modules(modules, ROOT, "/addin"))
        self.assertEqual(rows[f"{ROOT}.fusion_bridge.dispatch"], os.path.join("fusion_bridge", "dispatch.py"))
        self.assertNotIn("json", rows)
        self.assertNotIn(f"{ROOT}x.other", rows)
        self.assertEqual(bridge_info.loaded_modules(modules, "", "/addin"), [])

    def test_changed_sources_since_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            for rel in ("a.py", "pkg/b.py", "tests/t.py", "notes.txt"):
                path = os.path.join(tmp, rel)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w") as handle:
                    handle.write("x")
            os.utime(os.path.join(tmp, "a.py"), (100, 100))
            newest, changed = bridge_info.changed_sources(tmp, since=1000)
            self.assertEqual(changed, [os.path.join("pkg", "b.py")])
            self.assertGreater(newest, 1000)

    def test_collect_outside_git(self):
        with tempfile.TemporaryDirectory() as tmp:
            info = bridge_info.collect(modules={}, addin_dir=tmp, root="")
        self.assertIsNone(info["disk"]["commit"])
        self.assertEqual(info["disk"]["changed_since_load"], [])
        self.assertIn("generation", info["running"])
        self.assertIsNone(info["package"])

    def test_handler_returns_json(self):
        result = bridge_info.bridge_info({})
        self.assertFalse(result["isError"])
        payload = json.loads(result["content"][0]["text"])
        self.assertIn("running", payload)
        self.assertIn("modules", payload)


class ServerIdleTests(unittest.TestCase):
    def test_wait_until_idle(self):
        server = MCPServer(port=0, tools=[], tool_handlers={})
        self.assertTrue(server.wait_until_idle(0.01))
        server._track_post(1)
        self.assertFalse(server.wait_until_idle(0.05))
        threading.Timer(0.05, server._track_post, args=(-1,)).start()
        self.assertTrue(server.wait_until_idle(2))

    def test_stale_keep_alive_connection_gets_503(self):
        server = MCPServer(port=0, tools=[], tool_handlers={})
        server.start()
        self.addCleanup(server.stop)
        conn = http.client.HTTPConnection("127.0.0.1", server.httpd.server_address[1], timeout=5)
        self.addCleanup(conn.close)
        conn.request("GET", "/health")
        first = conn.getresponse()
        first.read()
        self.assertEqual(first.status, 200)
        server.stop()
        conn.request("GET", "/health")
        second = conn.getresponse()
        second.read()
        self.assertEqual(second.status, 503)
        self.assertEqual(second.getheader("Connection"), "close")


if __name__ == "__main__":
    unittest.main()

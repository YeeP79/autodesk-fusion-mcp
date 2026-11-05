"""execute_python sessions: what carries between calls and what must not."""

import gc
import json
import unittest
import weakref
from types import SimpleNamespace
from unittest.mock import patch

import _fusion_test_bootstrap  # noqa: F401
import adsk.core
from fusion_bridge import python_exec, runtime


class FakePoint3D:
    """Stands in for a SWIG class: `create` is a plain static function."""

    def __init__(self, x, y, z):
        self.xyz = (x, y, z)

    @staticmethod
    def create(x, y, z):
        return FakePoint3D(x, y, z)

    def asArray(self):
        return self.xyz


def fake_app():
    return SimpleNamespace(userInterface=SimpleNamespace())


class SessionCase(unittest.TestCase):
    def setUp(self):
        python_exec.SCRIPT_SESSIONS.clear()
        self.addCleanup(python_exec.SCRIPT_SESSIONS.clear)
        for target, attribute, value in (
            (python_exec, "get_app", fake_app),
            (adsk.core, "Point3D", FakePoint3D),
        ):
            patcher = patch.object(target, attribute, value, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)

    def call(self, code, session="s", **extra):
        arguments = {"code": code, "session_id": session, **extra}
        result = python_exec.run_python(arguments)
        return result, json.loads(result["content"][0]["text"])

    def ok(self, code, session="s", **extra):
        result, payload = self.call(code, session, **extra)
        self.assertFalse(result["isError"], payload)
        return payload

    def failed(self, code, session="s", **extra):
        result, payload = self.call(code, session, **extra)
        self.assertTrue(result["isError"], payload)
        return payload

    def value(self, expression, session="s"):
        return self.ok(f"_mcp_result = {expression}", session)["return_value"]


class SurvivalTests(SessionCase):
    def test_reported_symptom_alias_of_static_api_method(self):
        self.ok("P3 = adsk.core.Point3D.create")
        self.assertEqual(self.value("P3(1, 2, 3).asArray()"), "(1, 2, 3)")

    def test_reported_symptom_import(self):
        self.ok("import math")
        self.assertEqual(self.value("math.floor(2.5)"), "2")

    def test_from_import_and_aliased_import(self):
        self.ok("from math import floor\nimport collections.abc as cabc")
        self.assertEqual(self.value("floor(2.5)"), "2")
        self.assertEqual(self.value("cabc.Sized.__name__"), "Sized")

    def test_lambda(self):
        self.ok("double = lambda v: v * 2")
        self.assertEqual(self.value("double(4)"), "8")

    def test_function(self):
        self.ok("def helper(v):\n    return v + 1")
        self.assertEqual(self.value("helper(1)"), "2")

    def test_class_and_its_instance(self):
        self.ok("class Box:\n    size = 3\nbox = Box()")
        self.assertEqual(self.value("isinstance(box, Box) and Box.size"), "3")

    def test_bound_method_of_api_object(self):
        self.ok("as_array = adsk.core.Point3D.create(4, 5, 6).asArray")
        self.assertEqual(self.value("as_array()"), "(4, 5, 6)")

    def test_callable_instance(self):
        self.ok("import functools\nadd_one = functools.partial(lambda a, b: a + b, 1)")
        self.assertEqual(self.value("add_one(1)"), "2")

    def test_plain_values_still_persist(self):
        self.ok("count = 2\nnames = ['a']")
        self.ok("count += 1\nnames.append('b')")
        self.assertEqual(self.value("(count, names)"), "(3, ['a', 'b'])")

    def test_del_removes_a_name(self):
        self.ok("count = 2\nimport math")
        self.ok("del count, math")
        self.assertIn("name 'count' is not defined", self.failed("count")["error"])
        self.assertIn("name 'math' is not defined", self.failed("math")["error"])


class FunctionGlobalsTests(SessionCase):
    def test_helper_uses_a_name_from_an_earlier_call(self):
        self.ok("import math\nscale = 2")
        self.ok("def scaled(v):\n    return math.floor(v) * scale")
        self.assertEqual(self.value("scaled(2.5)"), "4")

    def test_helper_resolves_a_name_defined_in_a_later_call(self):
        self.ok("def outer():\n    return inner() + later_value")
        self.ok("def inner():\n    return 1")
        self.ok("later_value = 10")
        self.assertEqual(self.value("outer()"), "11")

    def test_helper_sees_a_later_rebinding(self):
        self.ok("scale = 2\ndef scaled(v):\n    return v * scale")
        self.ok("scale = 3")
        self.assertEqual(self.value("scaled(2)"), "6")

    def test_redefined_function_replaces_the_old_one_for_its_callers(self):
        self.ok("def inner():\n    return 1\ndef outer():\n    return inner()")
        self.ok("def inner():\n    return 2")
        self.assertEqual(self.value("outer()"), "2")

    def test_helper_writing_a_global_is_seen_by_later_calls(self):
        self.ok("total = 0\ndef bump():\n    global total\n    total += 1")
        self.ok("bump()")
        self.ok("bump()")
        self.assertEqual(self.value("total"), "2")

    def test_mutual_recursion_across_calls(self):
        self.ok("def even(n):\n    return n == 0 or odd(n - 1)")
        self.ok("def odd(n):\n    return n != 0 and even(n - 1)")
        self.assertEqual(self.value("even(4)"), "True")

    def test_method_resolves_session_names(self):
        self.ok("class Box:\n    def side(self):\n        return side")
        self.ok("side = 7")
        self.assertEqual(self.value("Box().side()"), "7")

    def test_lambda_alias_used_by_a_persisted_lambda(self):
        self.ok("P3 = adsk.core.Point3D.create\norigin = lambda: P3(0, 0, 0)")
        self.assertEqual(self.value("origin().asArray()"), "(0, 0, 0)")

    def test_private_helper_stays_reachable_from_a_public_one(self):
        self.ok("def _inner():\n    return 5\ndef outer():\n    return _inner()")
        self.assertEqual(self.value("outer()"), "5")

    def test_helper_sees_the_current_app_not_the_one_it_was_defined_with(self):
        first, second = fake_app(), fake_app()
        with patch.object(python_exec, "get_app", lambda: first):
            self.ok("def current():\n    return id(app)")
        with patch.object(python_exec, "get_app", lambda: second):
            self.assertEqual(self.value("current()"), str(id(second)))


class InjectedNameTests(SessionCase):
    def test_constant_names_exactly_what_each_call_injects(self):
        injected = set(python_exec._build_runtime_globals()) - {"__builtins__"}
        self.assertEqual(python_exec._INJECTED_NAMES, injected)

    def test_rebinding_an_injected_name_lasts_one_call(self):
        self.ok("json = 5\napp = 'mine'")
        self.assertEqual(self.value("json.dumps(1)"), "1")
        self.assertEqual(self.value("isinstance(app, str)"), "False")

    def test_deleting_an_injected_name_lasts_one_call(self):
        self.ok("del os")
        self.assertEqual(self.value("os.sep"), "/")


class ResultSlotTests(SessionCase):
    def test_result_does_not_repeat_on_the_next_call(self):
        self.assertEqual(self.ok("_mcp_result = 1")["return_value"], "1")
        self.assertIsNone(self.ok("pass")["return_value"])

    def test_result_from_a_failed_call_does_not_leak(self):
        self.failed("_mcp_result = 'stale'\nraise ValueError('boom')")
        self.assertIsNone(self.ok("pass")["return_value"])

    def test_persisted_function_can_set_the_result(self):
        self.ok("def answer():\n    global _mcp_result\n    _mcp_result = 42")
        self.assertEqual(self.ok("answer()")["return_value"], "42")


class ReportTests(SessionCase):
    def test_names_are_separated_by_kind_and_sorted(self):
        payload = self.ok(
            "import math\nimport cmath\n"
            "zeta = 1\nalpha = 2\n"
            "P3 = adsk.core.Point3D.create\n"
            "double = lambda v: v * 2\n"
            "def helper():\n    pass\n"
            "bound = adsk.core.Point3D.create(0, 0, 0).asArray\n"
            "class Box:\n    pass\n"
        )
        self.assertEqual(payload["session_variables"], ["alpha", "zeta"])
        self.assertEqual(payload["session_definitions"], {
            "modules": ["cmath", "math"],
            "classes": ["Box"],
            "functions": ["P3", "bound", "double", "helper"],
        })

    def test_builtins_injected_names_and_result_are_not_reported(self):
        payload = self.ok("_mcp_result = 1\nimport json\nimport os\napp = 1")
        self.assertEqual(payload["session_variables"], [])
        self.assertEqual(payload["session_definitions"],
                         {"modules": [], "classes": [], "functions": []})

    def test_private_names_are_reported_because_they_persist(self):
        payload = self.ok("_x = 1\ndef _f():\n    pass")
        self.assertEqual(payload["session_variables"], ["_x"])
        self.assertEqual(payload["session_definitions"]["functions"], ["_f"])

    def test_earlier_calls_stay_in_the_report(self):
        self.ok("a = 1\nimport math")
        payload = self.ok("b = 2")
        self.assertEqual(payload["session_variables"], ["a", "b"])
        self.assertEqual(payload["session_definitions"]["modules"], ["math"])

    def test_payload_is_json_whatever_the_session_holds(self):
        payload = self.ok("marker = object()\nraw = b'x'\nimport math")
        self.assertEqual(payload["session_variables"], ["marker", "raw"])


class FailureTests(SessionCase):
    def test_names_bound_before_a_failure_are_kept_and_reported(self):
        payload = self.failed("import math\nkept = 1\nraise ValueError('boom')\nlost = 2")
        self.assertEqual(payload["error"], "boom")
        self.assertEqual(payload["session_variables"], ["kept"])
        self.assertEqual(payload["session_definitions"]["modules"], ["math"])
        self.assertEqual(self.value("(kept, math.floor(1.5))"), "(1, 1)")

    def test_syntax_error_leaves_the_session_alone(self):
        self.ok("kept = 1")
        payload = self.failed("kept = = 2")
        self.assertEqual(payload["session_variables"], ["kept"])
        self.assertEqual(self.value("kept"), "1")

    def test_failure_in_a_one_off_call_reports_no_session(self):
        payload = self.failed("raise ValueError('boom')", persistent=False)
        self.assertNotIn("session_variables", payload)

    def test_unusable_session_id_is_an_error_result(self):
        payload = self.failed("x = 1", session=["not", "hashable"])
        self.assertFalse(payload["success"])
        self.assertNotIn("session_variables", payload)

    def test_missing_code_is_an_error_result(self):
        result = python_exec.run_python({"session_id": "s"})
        self.assertTrue(result["isError"])
        self.assertEqual(python_exec.SCRIPT_SESSIONS, {})


class OneOffCallTests(SessionCase):
    def test_result_shape_is_unchanged(self):
        payload = self.ok("x = 1\nprint('hi')\n_mcp_result = x", persistent=False)
        self.assertEqual(payload, {
            "stdout": "hi\n", "stderr": "", "return_value": "1",
            "session_variables": [], "success": True,
        })

    def test_nothing_is_stored(self):
        self.ok("x = 1\ndef f():\n    pass", persistent=False)
        self.assertEqual(python_exec.SCRIPT_SESSIONS, {})

    def test_session_is_neither_read_nor_changed(self):
        self.ok("x = 1\ndef f():\n    return 'session'")
        self.assertIn("name 'x' is not defined", self.failed("x", persistent=False)["error"])
        self.ok("x = 2\ndef f():\n    return 'one-off'", persistent=False)
        self.assertEqual(self.value("(x, f())"), "(1, 'session')")

    def test_consecutive_calls_share_nothing(self):
        self.ok("x = 1", persistent=False)
        self.assertIn("name 'x' is not defined", self.failed("x", persistent=False)["error"])

    def test_injected_names_are_available(self):
        self.assertEqual(self.ok("_mcp_result = json.dumps(1)", persistent=False)["return_value"], "1")


class IsolationTests(SessionCase):
    def test_names_do_not_cross_sessions(self):
        self.ok("import math\nx = 1\ndef f():\n    pass", "a")
        payload = self.ok("pass", "b")
        self.assertEqual(payload["session_variables"], [])
        self.assertEqual(payload["session_definitions"]["modules"], [])
        for name in ("x", "f", "math"):
            self.assertIn(f"name '{name}' is not defined", self.failed(name, "b")["error"])

    def test_same_named_helpers_resolve_in_their_own_session(self):
        self.ok("value = 'a'\ndef read():\n    return value", "a")
        self.ok("value = 'b'\ndef read():\n    return value", "b")
        self.assertEqual(self.value("read()", "a"), "a")
        self.assertEqual(self.value("read()", "b"), "b")

    def test_helper_passed_across_sessions_keeps_its_own_globals(self):
        self.ok("value = 'a'\ndef read():\n    return value", "a")
        python_exec.SCRIPT_SESSIONS["b"] = {"read": python_exec.SCRIPT_SESSIONS["a"]["read"]}
        self.assertEqual(self.value("read()", "b"), "a")

    def test_omitted_session_id_uses_the_default_session(self):
        python_exec.run_python({"code": "x = 1"})
        self.assertEqual(self.value("x", "default"), "1")


class CleanupTests(SessionCase):
    PIN = "class Held:\n    pass\nheld = Held()\ndef helper():\n    return held"

    def test_clear_sessions_forgets_every_session(self):
        self.ok("x = 1", "a")
        self.ok("y = 2", "b")
        python_exec.clear_sessions()
        self.assertEqual(python_exec.SCRIPT_SESSIONS, {})
        self.assertEqual(self.ok("pass", "a")["session_variables"], [])
        self.assertIn("name 'x' is not defined", self.failed("x", "a")["error"])

    def test_clear_sessions_on_no_sessions(self):
        python_exec.clear_sessions()
        self.assertEqual(python_exec.SCRIPT_SESSIONS, {})

    def test_held_objects_are_released_without_the_cycle_collector(self):
        self.ok(self.PIN)
        held = weakref.ref(python_exec.SCRIPT_SESSIONS["s"]["held"])
        gc.collect()
        gc.disable()
        self.addCleanup(gc.enable)
        python_exec.clear_sessions()
        self.assertIsNone(held())

    def test_stopping_the_runtime_clears_sessions(self):
        self.ok("x = 1")
        with (
            patch.object(runtime, "stop_main_thread_dispatch"),
            patch.object(runtime, "drain_logs"),
            patch.object(runtime, "log"),
            patch.object(runtime, "_server", None),
        ):
            runtime.stop()
        self.assertEqual(python_exec.SCRIPT_SESSIONS, {})


if __name__ == "__main__":
    unittest.main()

"""primitives: payload parsing, how failures surface, and the namespace in scripts."""

import json
import keyword
import unittest
from unittest import mock

import _fusion_test_bootstrap  # noqa: F401

from fusion_bridge import construction_plane, operations, primitives, python_exec, tool_surface
from fusion_bridge.common import feature_ops as fo
from fusion_bridge.common.results import error_result, ok_result, tool_result

TOOLS = {"find_entities", "sketch_profile", "extrude_profile", "edge_treatment", "pattern_features",
         "combine_bodies", "construction_plane", "apply_thread", "compare_bodies", "check_solids"}


def trace_text():
    return "Error: boom\nTraceback (most recent call last):\n  File \"x.py\", line 1\nValueError: boom\n"


class ParseResultTests(unittest.TestCase):
    def test_every_write_status_comes_back_as_it_was(self):
        for status in fo.STATUSES:
            payload = primitives.parse_result(fo.write_result(fo.envelope(status, "in")))
            self.assertEqual(payload["status"], status)
            self.assertEqual(payload["ok"], status in fo.OK_STATUSES)

    def test_refused_envelope_keeps_its_reasons(self):
        payload = primitives.parse_result(fo.write_result(fo.refused("in", ["one", "two"])))
        self.assertEqual(payload["refusals"], ["one", "two"])

    def test_answer_with_no_status_is_done(self):
        payload = primitives.parse_result(ok_result({"matched": 2}))
        self.assertEqual(payload, {"matched": 2, "status": "done", "ok": True})

    def test_a_payload_ok_of_its_own_is_kept(self):
        """check_solids answers without isError and says false when it found problems."""
        payload = primitives.parse_result(ok_result({"ok": False, "summary": {"problems": 1}}))
        self.assertIs(payload["ok"], False)
        self.assertEqual(payload["status"], "done")

    def test_text_refusal(self):
        payload = primitives.parse_result(error_result("Error: 'entity' must be 'face' or 'edge'"))
        self.assertEqual(payload, {"error": "Error: 'entity' must be 'face' or 'edge'",
                                   "status": "refused", "ok": False})

    def test_json_refusal_with_no_status(self):
        payload = primitives.parse_result(tool_result({"error": "refused", "problems": ["p"]}, True))
        self.assertEqual(payload["status"], "refused")
        self.assertIs(payload["ok"], False)
        self.assertEqual(payload["problems"], ["p"])

    def test_handler_catch_all_is_failed_not_refused(self):
        payload = primitives.parse_result(error_result(trace_text()))
        self.assertEqual(payload["status"], "failed")
        self.assertIs(payload["ok"], False)
        self.assertEqual(payload["error"], "Error: boom")
        self.assertTrue(payload["traceback"].startswith("Traceback (most recent call last):"))

    def test_error_text_that_is_json_but_not_an_object(self):
        self.assertEqual(primitives.parse_result(error_result("12"))["error"], "12")

    def test_success_text_that_is_not_json(self):
        self.assertEqual(primitives.parse_result(ok_result("fine")),
                         {"result": "fine", "status": "done", "ok": True})

    def test_missing_is_error_means_success(self):
        result = {"content": [{"type": "text", "text": "{}"}]}
        self.assertEqual(primitives.parse_result(result), {"status": "done", "ok": True})


class ArgumentTests(unittest.TestCase):
    def test_keyword_key_takes_a_trailing_underscore(self):
        self.assertEqual(primitives.tool_arguments("construction_plane", {"from_": "xy", "offset": 1}),
                         {"from": "xy", "offset": 1})

    def test_the_key_itself_still_works(self):
        self.assertEqual(primitives.tool_arguments("construction_plane", {"from": "xy"}), {"from": "xy"})

    def test_other_trailing_underscores_are_unknown(self):
        with self.assertRaisesRegex(ValueError, "offset_"):
            primitives.tool_arguments("construction_plane", {"offset_": 1})

    def test_both_spellings_refuse_in_either_order(self):
        for kwargs in ({"from": "xy", "from_": "xz"}, {"from_": "xz", "from": "xy"}):
            with self.assertRaisesRegex(ValueError, "not both"):
                primitives.tool_arguments("construction_plane", kwargs)

    def test_every_keyword_in_a_primitive_schema_is_reachable(self):
        keys = [key for tool in tool_surface.TOOL_DEFINITIONS if tool["name"] in TOOLS
                for key in tool["inputSchema"]["properties"] if keyword.iskeyword(key)]
        self.assertEqual(keys, ["from"])

    def test_readme_example_passes_validation(self):
        arguments = primitives.tool_arguments("construction_plane", {"mode": "offset", "from_": "xy", "offset": 1.0,
                                                                     "name": "Boss plane"})
        self.assertIsNone(construction_plane.validate_args(arguments)[1])


class CallTests(unittest.TestCase):
    def run_with(self, handler, **kwargs):
        with mock.patch.dict(primitives.HANDLERS, {"find_entities": handler}), \
                mock.patch.object(primitives, "log", lambda *a, **k: None):
            return primitives.Primitives().find_entities(**kwargs)

    def test_keywords_reach_the_handler_as_one_dict(self):
        handler = mock.Mock(return_value=ok_result({"matched": 0}))
        payload = self.run_with(handler, entity="edge", max_results=3)
        handler.assert_called_once_with({"entity": "edge", "max_results": 3})
        self.assertEqual(payload["matched"], 0)

    def test_no_keywords_is_an_empty_dict(self):
        handler = mock.Mock(return_value=ok_result({}))
        self.run_with(handler)
        handler.assert_called_once_with({})

    def test_exception_past_the_handler_is_failed(self):
        for error in (RuntimeError("boom"), TypeError("boom"), ValueError("boom")):
            payload = self.run_with(mock.Mock(side_effect=error))
            self.assertEqual(payload["status"], "failed", error)
            self.assertIs(payload["ok"], False)
            self.assertEqual(payload["error"], "Error: boom")
            self.assertIn("boom", payload["traceback"])

    def test_malformed_result_is_failed(self):
        self.assertEqual(self.run_with(mock.Mock(return_value={"content": []}))["status"], "failed")

    def test_system_exit_is_not_swallowed(self):
        with self.assertRaises(SystemExit):
            self.run_with(mock.Mock(side_effect=SystemExit(1)))

    def test_a_bad_key_never_reaches_the_handler(self):
        handler = mock.Mock()
        payload = self.run_with(handler, no_such_key=1)
        self.assertEqual(payload["status"], "refused")
        self.assertIn("no_such_key", payload["error"])
        handler.assert_not_called()


class SurfaceTests(unittest.TestCase):
    def test_covers_the_primitives(self):
        self.assertEqual(set(primitives.HANDLERS), TOOLS)

    def test_one_public_method_per_tool(self):
        public = {name for name in dir(primitives.Primitives) if not name.startswith("_")}
        self.assertEqual(public, TOOLS)

    def test_methods_carry_their_tool_name(self):
        self.assertEqual(primitives.Primitives.extrude_profile.__name__, "extrude_profile")

    def test_unknown_tool_is_an_attribute_error(self):
        with self.assertRaises(AttributeError):
            primitives.Primitives().delete_features

    def test_handlers_are_the_ones_the_mcp_tools_run(self):
        for name, handler in primitives.HANDLERS.items():
            wrapper = operations.TOOL_HANDLERS[name]
            self.assertIs(wrapper.__closure__[0].cell_contents, handler, name)

    def test_unknown_key_is_refused_for_every_tool(self):
        """check_solids' handler ignores one, so a typo would read as the default."""
        for name in TOOLS:
            payload = getattr(primitives.Primitives(), name)(no_such_key=1)
            self.assertEqual(payload["status"], "refused", name)
            self.assertIs(payload["ok"], False, name)

    def test_real_handler_refusal_comes_back_as_a_payload(self):
        for name in ("find_entities", "construction_plane", "apply_thread", "sketch_profile", "extrude_profile"):
            payload = getattr(primitives.Primitives(), name)()
            self.assertEqual(payload["status"], "refused", name)
            self.assertIs(payload["ok"], False, name)


class ScriptTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(python_exec, "get_app", lambda: mock.Mock())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(python_exec.clear_sessions)

    def run_code(self, code, **arguments):
        return json.loads(python_exec.run_python({"code": code, **arguments})["content"][0]["text"])

    def test_injected(self):
        self.assertIsInstance(python_exec._build_runtime_globals()["primitives"], primitives.Primitives)

    def test_script_gets_the_payload_as_a_dict(self):
        handler = mock.Mock(return_value=ok_result({"matched": 7}))
        with mock.patch.dict(primitives.HANDLERS, {"find_entities": handler}):
            report = self.run_code("_mcp_result = primitives.find_entities(entity='face')['matched']")
        self.assertEqual(report["return_value"], "7")

    def test_a_refused_tool_does_not_fail_the_script(self):
        report = self.run_code("r = primitives.find_entities()\nprint(r['status'], r['ok'])\nprint('went on')")
        self.assertTrue(report["success"])
        self.assertEqual(report["stdout"], "refused False\nwent on\n")

    def test_not_listed_as_a_session_name(self):
        report = self.run_code("x = 1", session_id="names")
        self.assertEqual(report["session_variables"], ["x"])

    def test_a_script_patching_it_does_not_outlive_the_call(self):
        self.run_code("primitives.find_entities = None", session_id="patch")
        report = self.run_code("_mcp_result = primitives.find_entities is None", session_id="patch")
        self.assertEqual(report["return_value"], "False")

    def test_a_persisted_helper_sees_it(self):
        handler = mock.Mock(return_value=ok_result({"matched": 2}))
        self.run_code("def count():\n    return primitives.find_entities(entity='edge')['matched']", session_id="fn")
        with mock.patch.dict(primitives.HANDLERS, {"find_entities": handler}):
            self.assertEqual(self.run_code("_mcp_result = count()", session_id="fn")["return_value"], "2")


if __name__ == "__main__":
    unittest.main()
